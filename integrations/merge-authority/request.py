#!/usr/bin/env python3
"""One installed mechanical merge request; no scheduler or generic App endpoint."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path

ROOT = Path("/private/var/db/aru-trusted-merge")
KERNEL_FILES = (
    "merge_pr.py", "merge_authority.py", "merge_state.py", "common.py",
    "check_ci.py", "fetch_pr_feedback.py", "touches.py",
)
PRINCIPAL = "aru-merge"
LOCKED_SHELL = "/usr/bin/false"


class Refusal(RuntimeError):
    """A bounded refusal whose public diagnostic contains no credential data."""


def parse_request(argv: list[str]) -> tuple[int, str, bool]:
    if len(argv) not in (4, 5) or argv[::2][:2] != ["--pr", "--expected-head"]:
        raise Refusal("request-shape")
    if len(argv) == 5 and argv[4] != "--check":
        raise Refusal("request-shape")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", argv[1]):
        raise Refusal("request-pr")
    head = argv[3]
    if not re.fullmatch(r"[0-9a-f]{40}", head) or head == "0" * 40:
        raise Refusal("request-head")
    return int(argv[1]), head, len(argv) == 5


def protected_bytes(path: Path, owners: set[int], *, secret: bool = False) -> bytes:
    """Refuse links, mutable ancestor chains and races before reading a file."""
    if not path.is_absolute():
        raise Refusal("unsafe-installation-path")
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
            raise Refusal("mutable-installation-chain")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
            raise Refusal("mutable-installation-file")
        if secret and info.st_mode & 0o077:
            raise Refusal("credential-directory-access")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def system_interpreter() -> Path:
    """Apple's root-owned alias is allowed only to a protected real binary.

    This exception applies solely to the already running OS interpreter, not
    configuration, installed code, the CLI, credentials or caller paths.
    """
    alias = Path(sys.executable)
    for path in [*reversed(alias.parents), alias]:
        info = path.lstat()
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise Refusal("mutable-interpreter-alias")
    resolved = alias.resolve(strict=True)
    protected_bytes(resolved, {0})
    return resolved


def validate_file_inventory(config: dict) -> None:
    required = {"request.py", "app.py", "bin/authority", "bin/gh", "checkout/.git/config", "checkout/.git/HEAD"}
    required.update("lib/" + name for name in KERNEL_FILES)
    files = config["files"]
    if not isinstance(files, dict) or set(files) != required:
        raise Refusal("installation-file-inventory")
    authority = config["authority_files"]
    if not isinstance(authority, dict) or not authority or len(authority) > 30:
        raise Refusal("authority-file-inventory")
    if config["authority_runner"] not in authority:
        raise Refusal("authority-file-inventory")
    for path, digest in [*files.items(), *authority.items()]:
        if not isinstance(path, str) or ".." in Path(path).parts or not re.fullmatch(
            r"[0-9a-f]{64}", str(digest),
        ):
            raise Refusal("installation-file-inventory")
    if any(not path.startswith("/") for path in authority):
        raise Refusal("authority-file-inventory")


def validate_config(config: object) -> dict:
    expected = {
        "version", "repository", "issues", "principal_uid", "principal_gid",
        "principal_home", "helper_commit", "files", "authority_files",
        "authority_runner", "authority_app_id", "authority_installation_id", "python", "python_sha256",
    }
    if not isinstance(config, dict) or set(config) != expected or type(config["version"]) is not int or config["version"] != 1:
        raise Refusal("installation-config")
    if not isinstance(config["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
        config["repository"],
    ) or ".." in config["repository"]:
        raise Refusal("installation-repository")
    issues = config["issues"]
    if not isinstance(issues, list) or not issues or len(issues) > 100 or any(
        type(n) is not int or n <= 0 for n in issues
    ) or len(set(issues)) != len(issues):
        raise Refusal("installation-scope")
    for name in ("principal_uid", "principal_gid", "authority_app_id", "authority_installation_id"):
        if type(config[name]) is not int or config[name] <= 0:
            raise Refusal("installation-principal")
    for name in ("principal_home", "authority_runner", "python"):
        value = config[name]
        if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
            raise Refusal("installation-path")
    if not re.fullmatch(r"[0-9a-f]{40}", str(config["helper_commit"])):
        raise Refusal("installation-revision")
    validate_file_inventory(config)
    if config["authority_runner"] != str(ROOT / "bin/authority"):
        raise Refusal("installation-authority")
    if config["authority_files"] != {
        str(ROOT / name): config["files"][name]
        for name in ("app.py", "bin/authority")
    }:
        raise Refusal("authority-file-inventory")
    if not re.fullmatch(r"[0-9a-f]{64}", str(config["python_sha256"])):
        raise Refusal("installation-interpreter")
    return config


def verify_locked_principal(config: dict) -> None:
    """Recheck live account metadata; the shell is never caller or config input.

    A non-login shell narrows one access route. It does not prove the absence of
    passwords, SSH, remote access or other sudo paths; host proof covers those.
    """
    try:
        account = pwd.getpwuid(os.geteuid())
    except KeyError:
        raise Refusal("wrong-execution-principal") from None
    if (account.pw_name, account.pw_uid, account.pw_gid, account.pw_dir) != (
        PRINCIPAL, config["principal_uid"], config["principal_gid"], config["principal_home"],
    ):
        raise Refusal("wrong-execution-principal")
    if account.pw_shell != LOCKED_SHELL:
        raise Refusal("login-capable-principal")


def verify_installation(config: dict) -> None:
    if not sys.flags.isolated or os.geteuid() != config["principal_uid"]:
        raise Refusal("wrong-execution-principal")
    if os.getegid() != config["principal_gid"] or os.geteuid() == 0:
        raise Refusal("wrong-execution-principal")
    verify_locked_principal(config)
    for relative, digest in config["files"].items():
        if hashlib.sha256(protected_bytes(ROOT / relative, {0})).hexdigest() != digest:
            raise Refusal("installation-content-changed")
    for filename, digest in config["authority_files"].items():
        if hashlib.sha256(protected_bytes(Path(filename), {0})).hexdigest() != digest:
            raise Refusal("authority-content-changed")
    interpreter = system_interpreter()
    if str(interpreter) != config["python"] or hashlib.sha256(
        protected_bytes(interpreter, {0})
    ).hexdigest() != config["python_sha256"]:
        raise Refusal("interpreter-changed")
    # Only the separate merge identity can read the operator's CLI login. Do
    # not read or copy its contents here; gh consumes it in the clean context.
    for directory in (ROOT / "credentials", ROOT / "runtime"):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != config["principal_uid"] or info.st_mode & 0o077:
            raise Refusal("private-directory-access")


def execution_lock(uid: int) -> int:
    """Reject overlapping submissions immediately; no queue or lifecycle store."""
    fd = os.open(ROOT / "runtime" / "request.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Refusal("unsafe-execution-lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("merge-context-busy") from None
        return fd
    except Exception:
        os.close(fd)
        raise


def authority_environment(config: dict) -> dict[str, str]:
    """Construct rather than filter: no caller token, PATH, hook or import input."""
    return {
        "HOME": config["principal_home"], "PATH": f"{ROOT}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8", "GH_CONFIG_DIR": str(ROOT / "credentials"),
        "GH_REPO": config["repository"], "GH_PROMPT_DISABLED": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(ROOT / "checkout"),
        "ARU_MERGE_APP_ID": str(config["authority_app_id"]),
        "ARU_MERGE_APP_RUNNER": config["authority_runner"],
    }


def validate_live_record(kernel, record: dict, config: dict, pr: int, head: str) -> int:
    if record.get("headRefOid") != head or record.get("number") != pr:
        raise Refusal("current-head")
    numbers = kernel.linked_issues(str(record.get("body") or ""))
    if len(numbers) != 1 or numbers[0] not in config["issues"]:
        raise Refusal("authorized-issue-scope")
    if record.get("state") != "MERGED":
        if record.get("state") != "OPEN" or record.get("isDraft") is not False:
            raise Refusal("open-ready-pr")
        if record.get("reviewDecision") != "APPROVED":
            raise Refusal("github-counted-approval")
    return numbers[0]


def execute_request(kernel, config: dict, pr: int, head: str, check: bool) -> dict:
    """Bind every canonical reread to this request's admitted issue and head."""
    original_pull = kernel.pull_request
    original_gate = kernel.issue_gate
    original_close = kernel.close_out
    admitted = None

    def scoped_pull(number):
        nonlocal admitted
        if number != pr:
            raise Refusal("current-head")
        record = original_pull(number)
        issue = validate_live_record(kernel, record, config, pr, head)
        if admitted is None:
            admitted = issue
        elif issue != admitted:
            raise Refusal("authorized-issue-changed")
        return record

    def scoped_gate(numbers, *args, **kwargs):
        if admitted is None or numbers != [admitted]:
            raise Refusal("authorized-issue-changed")
        return original_gate(numbers, *args, **kwargs)

    def scoped_close(numbers, *args, **kwargs):
        # The pinned helper passes the admitted issue; refuse any other set
        # before canonical close-out reads or changes issue/Project status.
        if admitted is None or numbers != [admitted]:
            raise Refusal("authorized-issue-changed")
        return original_close(numbers, *args, **kwargs)

    # The process lock admits one request. Temporary wrappers add the external
    # operator scope to each read and close-out without replacing a canonical gate.
    kernel.pull_request = scoped_pull
    kernel.issue_gate = scoped_gate
    kernel.close_out = scoped_close
    try:
        record = kernel.pull_request(pr)
        if record.get("state") == "MERGED":
            if check:
                raise Refusal("already-merged")
            # A retried bounded request repairs close-out, never resubmits a merge.
            return kernel.finalize_queued(pr, head)
        return kernel.merge(pr, head, dry_run=check)
    finally:
        kernel.pull_request = original_pull
        kernel.issue_gate = original_gate
        kernel.close_out = original_close


def main(argv: list[str]) -> int:
    lock = None
    try:
        pr, head, check = parse_request(argv)
        if sys.platform != "darwin" or Path(__file__).absolute() != ROOT / "request.py":
            raise Refusal("not-installed")
        config = validate_config(json.loads(protected_bytes(ROOT / "config.json", {0})))
        verify_installation(config)
        lock = execution_lock(config["principal_uid"])
        os.environ.clear()
        os.environ.update(authority_environment(config))
        os.environ["ARU_TRUSTED_HEAD"] = head
        os.chdir(ROOT / "checkout")
        sys.path.insert(0, str(ROOT / "lib"))
        import merge_pr as kernel

        if kernel.repo_slug() != config["repository"]:
            raise Refusal("current-repository")
        result = execute_request(kernel, config, pr, head, check)
        # No raw command output, token, configuration or exception detail is
        # returned to the caller. The caller refreshes GitHub for full evidence.
        print(json.dumps({"pr": pr, "head": head, "merged": result.get("merged") is True,
                          "checked": check, "finalized": result.get("finalized") is True,
                          "merge_commit": result.get("merge_commit")}))
        return 0
    except Exception as exc:
        gate = str(exc) if isinstance(exc, Refusal) else getattr(exc, "gate", "authority-unavailable")
        print(json.dumps({"refused": True, "gate": gate}))
        return 2
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
