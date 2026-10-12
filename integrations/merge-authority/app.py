#!/usr/bin/env python3
"""Private check-only App adapter. Never expose this program through sudoers."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/private/var/db/aru-trusted-merge")
# Same-head failure attempts after an unconfirmed authorization POST.
COMPENSATION_ATTEMPTS = 2


class Refusal(RuntimeError):
    """An adapter refusal; no credential-bearing exception is exposed."""


def operation(argv: list[str], repository: str, head: str, payload: object) -> str:
    prefix = ["--repo", repository, "--", "gh", "api"]
    if argv == [*prefix, f"repos/{repository}"] and payload is None:
        return "repository"
    if argv != [*prefix, "--method", "POST", f"repos/{repository}/check-runs", "--input", "-"]:
        raise Refusal("app-operation")
    if not isinstance(payload, dict) or set(payload) != {"name", "head_sha", "status", "conclusion", "output"}:
        raise Refusal("app-payload")
    if payload["name"] != "aru-merge-authorized" or payload["head_sha"] != head:
        raise Refusal("app-head")
    if payload["status"] != "completed" or payload["conclusion"] not in ("success", "failure"):
        raise Refusal("app-verdict")
    output = payload["output"]
    if not isinstance(output, dict) or set(output) != {"title", "summary"} or any(
        not isinstance(text, str) or not 1 <= len(text) <= 200 for text in output.values()
    ):
        raise Refusal("app-output")
    return "check"


def api(method: str, endpoint: str, token: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("https://api.github.com/" + endpoint, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    # No proxy or redirect may forward the App credential elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise Refusal("app-redirect")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise Refusal("app-response")
    return json.loads(raw) if raw else None


def jwt(config: dict) -> str:
    key = ROOT / "credentials/merge-app.pem"
    # Validate metadata without ever returning or reading PEM bytes in Python.
    # OpenSSL alone consumes the private file under the separate execution user.
    info = key.lstat()
    import stat
    if not stat.S_ISREG(info.st_mode) or info.st_uid != config["principal_uid"] or info.st_mode & 0o077 or info.st_nlink != 1:
        raise Refusal("app-private-key")
    # main() validates this system executable's protected ancestor chain.
    now = int(time.time())
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    unsigned = encoded({"alg": "RS256", "typ": "JWT"}) + b"." + encoded(
        {"iat": now - 60, "exp": now + 300, "iss": config["authority_app_id"]},
    )
    result = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign", str(key)],
                            input=unsigned, capture_output=True, check=False,
                            env={"PATH": "/usr/bin:/bin"}, timeout=15)
    if result.returncode or not result.stdout:
        raise Refusal("app-signing")
    return (unsigned + b"." + base64.urlsafe_b64encode(result.stdout).rstrip(b"=")).decode()


def verified_check(result: object, config: dict, payload: dict) -> dict:
    """Return only a check GitHub confirmably recorded exactly as requested."""
    app = result.get("app") if isinstance(result, dict) else None
    if (
        not isinstance(app, dict) or type(result.get("id")) is not int or result["id"] <= 0
        or type(app.get("id")) is not int or app["id"] != config["authority_app_id"]
        or result.get("head_sha") != payload["head_sha"] or result.get("name") != payload["name"]
        or result.get("conclusion") != payload["conclusion"]
    ):
        raise Refusal("app-check-result")
    return {"id": result["id"], "app": {"id": app["id"]}, "head_sha": result["head_sha"],
            "name": result["name"], "conclusion": result["conclusion"]}


def compensate(config: dict, token: str, payload: dict) -> bool:
    """After an unconfirmed POST, post a bounded same-head failure; never a success."""
    failure = {**payload, "conclusion": "failure", "output": {
        "title": "Merge authorization unconfirmed",
        "summary": "Authorization response was uncertain; re-run merge_pr.py",
    }}
    for _attempt in range(COMPENSATION_ATTEMPTS):
        try:
            verified_check(api("POST", f"repos/{config['repository']}/check-runs", token, failure),
                           config, failure)
            return True
        except Exception:
            continue
    # An outage can outlast this bound; the README records that honest limit.
    return False


def revoke(token: str) -> None:
    """Best-effort token cleanup; GitHub's bounded expiry otherwise applies."""
    try:
        api("DELETE", "installation/token", token)
    except Exception:
        # Never mask a confirmed result or the primary refusal; expose nothing.
        pass


def perform(config: dict, kind: str, payload):
    bearer = jwt(config)
    repository = config["repository"]
    installation = config["authority_installation_id"]
    app = api("GET", "app", bearer)
    installed = api("GET", f"app/installations/{installation}", bearer)
    if app.get("id") != config["authority_app_id"] or installed.get("app_id") != config["authority_app_id"]:
        raise Refusal("app-identity")
    if (installed.get("account") or {}).get("login", "").lower() != repository.split("/")[0].lower():
        raise Refusal("app-installation")
    permissions = {"checks": "write", "metadata": "read"}
    issued = api("POST", f"app/installations/{installation}/access_tokens", bearer,
                 {"repositories": [repository.split("/")[1]], "permissions": permissions})
    token = issued.get("token")
    if not isinstance(token, str) or not token:
        raise Refusal("app-token")
    try:
        repositories = issued.get("repositories")
        if issued.get("permissions") != permissions or not isinstance(repositories, list) or len(repositories) != 1 or repositories[0].get("full_name", "").lower() != repository.lower():
            raise Refusal("app-token-scope")
        if kind == "repository":
            result = api("GET", "repos/" + repository, token)
            return {"id": result["id"], "full_name": result["full_name"]}
        try:
            return verified_check(api("POST", "repos/" + repository + "/check-runs", token, payload),
                                  config, payload)
        except Exception:
            # GitHub may have recorded it, but canonical post() sees only a refusal
            # and cannot revoke. Supersede at this head, then refuse.
            compensate(config, token, payload)
            raise Refusal("app-check-unconfirmed") from None
    finally:
        revoke(token)


def main(argv: list[str]) -> int:
    try:
        if sys.platform != "darwin" or Path(__file__).absolute() != ROOT / "app.py":
            raise Refusal("not-installed")
        # -I excludes the script directory; use only the fixed protected root.
        sys.path.insert(0, str(ROOT))
        from request import protected_bytes, validate_config, verify_installation

        config = validate_config(json.loads(protected_bytes(ROOT / "config.json", {0})))
        verify_installation(config)
        protected_bytes(Path("/usr/bin/openssl"), {0})
        # The only sudo entry point constructs this head from its strict argv.
        head = os.environ.get("ARU_TRUSTED_HEAD", "")
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise Refusal("app-head")
        raw = sys.stdin.buffer.read(16_385) if argv[-2:] == ["--input", "-"] else b""
        if len(raw) > 16_384:
            raise Refusal("app-payload")
        payload = json.loads(raw) if raw else None
        kind = operation(argv, config["repository"], head, payload)
        print(json.dumps(perform(config, kind, payload)))
        return 0
    except Exception:
        # No signing/authentication response or command stderr reaches callers.
        print(json.dumps({"refused": True, "gate": "app-authority-unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
