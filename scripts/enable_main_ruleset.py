#!/usr/bin/env python3
"""Create or update the Aru ruleset that blocks direct pushes to main.

Required approving review and required linear history are deliberately
absent. The former deadlocks a same-account fleet (#123 / S0.7a). The
latter forbids merge commits and fights #89 / S1.1.

Default enforcement is ``evaluate`` so a misnamed status check cannot
lock the factory out. ``--enforcement active`` is #133. ``--disable`` and
``--delete`` are the inverse of ``--apply``.

A 403 from GitHub because the plan does not include rulesets is exit 3,
not a silent success.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import get_repo_slug, run_cmd

RULESET_NAME = "aru-protect-main"
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
EXIT_BLOCKED = 3
FORBIDDEN_RULE_TYPES = frozenset({"required_linear_history"})
ListResult = Tuple[str, Optional[int], str]


def ci_context_from_workflow(path: Path = WORKFLOW) -> str:
    """The required-check name must be the CI job name, not a copy of it."""
    in_job = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("  test-and-lint:"):
            in_job = True
            continue
        if in_job and line.startswith("    name:"):
            return line.split(":", 1)[1].strip()
        if in_job and line.startswith("  ") and not line.startswith("    "):
            break
    raise ValueError(f"{path} has no test-and-lint job name")


def ruleset_payload(enforcement: str = "evaluate") -> Dict[str, Any]:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": enforcement,
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["refs/heads/main", "refs/heads/master"],
                "exclude": [],
            }
        },
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": ci_context_from_workflow()}
                    ],
                },
            },
        ],
    }


def assert_safe_payload(payload: Dict[str, Any]) -> None:
    """Refuse to ship the two rules this issue explicitly excludes."""
    for rule in payload.get("rules") or []:
        rtype = rule.get("type")
        if rtype in FORBIDDEN_RULE_TYPES:
            raise ValueError(f"forbidden ruleset rule: {rtype}")
        params = rule.get("parameters") or {}
        if int(params.get("required_approving_review_count") or 0) != 0:
            raise ValueError("required approving review would deadlock the fleet")


def _gh_api(method: str, path: str, body: Optional[Dict[str, Any]] = None):
    cmd = ["gh", "api", "--method", method, path]
    if body is not None:
        cmd += ["--input", "-"]
    payload = json.dumps(body) if body is not None else None
    try:
        proc = subprocess.run(
            cmd, input=payload, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def is_plan_blocked(stderr: str, stdout: str) -> bool:
    blob = f"{stderr}\n{stdout}".lower()
    return "upgrade to github pro" in blob or (
        "403" in blob and "ruleset" in blob
    )


def _parse_ruleset_pages(raw: str) -> List[Dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        items: List[Dict[str, Any]] = []
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(raw):
            while idx < len(raw) and raw[idx].isspace():
                idx += 1
            if idx >= len(raw):
                break
            obj, end = decoder.raw_decode(raw, idx)
            if not isinstance(obj, list):
                raise ValueError("ruleset list page was not a JSON array")
            items.extend(obj)
            idx = end
        return items
    if not isinstance(data, list):
        raise ValueError("ruleset list was not a JSON array")
    return data


def find_existing_id(slug: str) -> ListResult:
    """Return (ok, id|None, ''), (blocked, None, detail), or (error, None, detail).

    A failed list must not be treated as 'does not exist' — that POSTs a
    duplicate. Plan 403 is blocked, not missing.
    """
    code, out, err = run_cmd(
        ["gh", "api", "--paginate", f"repos/{slug}/rulesets?per_page=100"],
        check=False,
    )
    if code != 0:
        detail = (err or out).strip()
        if is_plan_blocked(err, out):
            return "blocked", None, detail
        return "error", None, detail or "listing rulesets failed"
    try:
        items = _parse_ruleset_pages(out)
    except (json.JSONDecodeError, ValueError) as exc:
        return "error", None, str(exc)
    for item in items:
        if item.get("name") == RULESET_NAME:
            return "ok", item.get("id"), ""
    return "ok", None, ""


def _blocked_message(detail: str) -> int:
    print(
        "[BLOCKED] GitHub refused rulesets on this repository (HTTP 403). "
        "Private repos need GitHub Pro, or the repo must be public. "
        "The local pre-push hook remains skippable until then. See #133.",
        file=sys.stderr,
    )
    if detail:
        print(detail, file=sys.stderr)
    return EXIT_BLOCKED


def apply_ruleset(slug: str, enforcement: str = "evaluate") -> int:
    payload = ruleset_payload(enforcement)
    assert_safe_payload(payload)
    status, existing, detail = find_existing_id(slug)
    if status == "blocked":
        return _blocked_message(detail)
    if status == "error":
        print(f"[ERROR] cannot list rulesets; refusing to POST a duplicate: {detail}",
              file=sys.stderr)
        return 1
    if existing is None:
        method, path = "POST", f"repos/{slug}/rulesets"
    else:
        method, path = "PUT", f"repos/{slug}/rulesets/{existing}"
    code, out, err = _gh_api(method, path, payload)
    if code == 0:
        action = "updated" if existing else "created"
        print(f"✅ Ruleset '{RULESET_NAME}' {action} on {slug} "
              f"(enforcement={enforcement}).")
        return 0
    if is_plan_blocked(err, out):
        return _blocked_message((err or out).strip())
    print(f"[ERROR] ruleset {method} {path} failed:\n{(err or out).strip()}",
          file=sys.stderr)
    return 1


def disable_ruleset(slug: str) -> int:
    status, existing, detail = find_existing_id(slug)
    if status == "blocked":
        return _blocked_message(detail)
    if status == "error":
        print(f"[ERROR] cannot list rulesets: {detail}", file=sys.stderr)
        return 1
    if existing is None:
        print(f"[ERROR] no ruleset named '{RULESET_NAME}' to disable.",
              file=sys.stderr)
        return 1
    return apply_ruleset(slug, enforcement="disabled")


def delete_ruleset(slug: str) -> int:
    status, existing, detail = find_existing_id(slug)
    if status == "blocked":
        return _blocked_message(detail)
    if status == "error":
        print(f"[ERROR] cannot list rulesets: {detail}", file=sys.stderr)
        return 1
    if existing is None:
        print(f"[ERROR] no ruleset named '{RULESET_NAME}' to delete.",
              file=sys.stderr)
        return 1
    code, out, err = _gh_api("DELETE", f"repos/{slug}/rulesets/{existing}")
    if code == 0:
        print(f"✅ Ruleset '{RULESET_NAME}' deleted on {slug}.")
        return 0
    if is_plan_blocked(err, out):
        return _blocked_message((err or out).strip())
    print(f"[ERROR] delete failed:\n{(err or out).strip()}", file=sys.stderr)
    return 1


def _require_slug() -> Optional[str]:
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        print("[ERROR] Could not resolve owner/repo via gh.", file=sys.stderr)
        return None
    return slug


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the Aru main-branch ruleset (no required reviews)."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument(
        "--enforcement",
        choices=("evaluate", "active", "disabled"),
        default="evaluate",
        help="evaluate is the default so a bad check name cannot lock main.",
    )
    args = parser.parse_args(argv)
    payload = ruleset_payload(args.enforcement)
    assert_safe_payload(payload)
    if args.dry_run or not (args.apply or args.disable or args.delete):
        print(json.dumps(payload, indent=2))
        if not (args.apply or args.disable or args.delete):
            print("Pass --apply, --disable, or --delete.", file=sys.stderr)
        return 0
    slug = _require_slug()
    if slug is None:
        return 1
    if args.delete:
        return delete_ruleset(slug)
    if args.disable:
        return disable_ruleset(slug)
    return apply_ruleset(slug, enforcement=args.enforcement)


if __name__ == "__main__":
    sys.exit(main())
