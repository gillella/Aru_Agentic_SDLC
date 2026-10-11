#!/usr/bin/env python3
"""One installed mechanical merge request; no scheduler or generic App endpoint."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import pwd
import re
import signal
import resource
import stat
import sys
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

ROOT = Path("/private/var/db/aru-trusted-merge")
KERNEL_FILES = (
    "merge_pr.py", "merge_authority.py", "merge_state.py", "common.py",
    "check_ci.py", "fetch_pr_feedback.py", "touches.py",
)
PRINCIPAL = "aru-merge"
LOCKED_SHELL = "/usr/bin/false"


LIMITS = {"as": None, "core": 0, "cpu": None, "data": None, "fsize": None,
          "memlock": None, "nofile": 1024, "nproc": 1024, "rss": None, "stack": 8_388_608}


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



def verify_resource_limits() -> None:
    """Sudo must replace every caller-supplied soft and hard resource limit."""
    try:
        for name, value in LIMITS.items():
            expected = resource.RLIM_INFINITY if value is None else value
            if resource.getrlimit(getattr(resource, "RLIMIT_" + name.upper())) != (expected, expected):
                raise Refusal("unapproved-resource-limits")
    except (AttributeError, OSError, ValueError):
        raise Refusal("unapproved-resource-limits") from None

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


def verify_enablement(config_bytes: bytes) -> None:
    """A human's protected host switch, bound to this exact installed config.

    It supplies no issue, review or merge evidence. The canonical helper still
    rereads every live gate. Installation never creates this marker.
    """
    try:
        marker = json.loads(protected_bytes(ROOT / "enabled.json", {0}))
        if (not isinstance(marker, dict) or set(marker) != {"version", "config_sha256"}
                or type(marker["version"]) is not int or marker["version"] != 1
                or marker["config_sha256"] != hashlib.sha256(config_bytes).hexdigest()):
            raise Refusal("submissions-disabled")
    except (OSError, ValueError, Refusal):
        raise Refusal("submissions-disabled") from None


def execution_lock(uid: int) -> int:
    """Reject overlapping submissions immediately; no queue or lifecycle store."""
    fd = os.open(ROOT / "runtime" / "request.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077
                or info.st_mode & 0o600 != 0o600 or info.st_nlink != 1):
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


def bounded_nodes(connection) -> list:
    if (not isinstance(connection, dict) or type(connection.get("totalCount")) is not int
            or not isinstance(connection.get("nodes"), list)
            or not 0 <= connection["totalCount"] <= 100
            or len(connection["nodes"]) != connection["totalCount"]
            or not isinstance(connection.get("pageInfo"), dict)
            or connection["pageInfo"].get("hasNextPage") is not False):
        raise Refusal("closing-effect-evidence")
    return connection["nodes"]


def read_closing_effects(kernel, record: dict, pr: int) -> dict:
    owner, name = kernel.repo_slug().split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){pullRequest(number:$number){
        number title body state headRefOid baseRefOid headRefName
        mergeCommit{oid message}
        repository{nameWithOwner} headRepository{nameWithOwner}
        closingIssuesReferences(first:100,excludeUserLinked:false){
          totalCount pageInfo{hasNextPage} nodes{number repository{nameWithOwner}}
        }
        commits(first:100){totalCount pageInfo{hasNextPage} nodes{commit{oid message}}}
      }}
    }"""
    try:
        result = kernel.run([
            "gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}",
            "-F", f"name={name}", "-F", f"number={pr}",
        ], auth="repository", timeout=30)
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or ("errors" in data and data["errors"] != []):
            raise ValueError
        return data["data"]["repository"]["pullRequest"]
    except Exception:
        raise Refusal("closing-effect-evidence") from None


def restricted_closing_text(text: str, issue: int) -> None:
    """Admit simple whole-line directives only; do not emulate GitHub's parser.

    Remove exact same-issue local directives and Markdown heading markers, then
    deny any remaining reference when closing vocabulary occurs in the text.
    This rejects prose/code examples and ambiguous syntax as well as closures;
    removing a directive must never hide a possible continuation target.
    Conventional 'fix:' messages without references remain usable. Substring
    matching is conservative even around punctuation, Markdown and Unicode.
    """
    keywords = r"close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved"
    directive = re.compile(rf"(?i:{keywords})[ \t]+#{issue}")
    residual = "\n".join(re.sub(r"^[ \t]*#{1,6}[ \t]+", "", line)
                         for line in text.splitlines()
                         if not directive.fullmatch(line.strip(" \t\r")))
    if re.search(keywords, text, re.I) and (
        "#" in residual or "github.com" in residual.lower()
    ):
        raise Refusal("authorized-closing-scope")


def validate_closing_effects(kernel, record: dict, config: dict, pr: int, issue: int) -> None:
    live = read_closing_effects(kernel, record, pr)
    if (not isinstance(live, dict)
            or any(key not in record or live.get(key) != record[key] for key in
                   ("number", "title", "body", "state", "headRefOid", "baseRefOid", "headRefName"))
            or live.get("repository") != {"nameWithOwner": config["repository"]}
            or live.get("headRepository") != {"nameWithOwner": config["repository"]}):
        raise Refusal("closing-effect-evidence")
    for field in ("title", "body", "headRefName"):
        if not isinstance(live[field], str):
            raise Refusal("closing-effect-evidence")
        restricted_closing_text(live[field], issue)
    targets = bounded_nodes(live.get("closingIssuesReferences"))
    if (len(targets) != 1 or not isinstance(targets[0], dict)
            or type(targets[0].get("number")) is not int
            or targets != [{"number": issue, "repository": {"nameWithOwner": config["repository"]}}]):
        raise Refusal("authorized-closing-scope")
    commits = bounded_nodes(live.get("commits"))
    oids = []
    for node in commits:
        commit = node.get("commit") if isinstance(node, dict) else None
        if (not isinstance(commit, dict) or not isinstance(commit.get("oid"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", commit["oid"])
                or not isinstance(commit.get("message"), str)):
            raise Refusal("closing-effect-evidence")
        oids.append(commit["oid"])
        restricted_closing_text(commit["message"], issue)
    if not oids or len(set(oids)) != len(oids) or record["headRefOid"] not in oids:
        raise Refusal("closing-effect-evidence")
    if record["state"] == "MERGED":
        validate_merge_message(live, record, config, pr, issue)


def validate_merge_message(live, record, config, pr, issue):
    commit = live.get("mergeCommit")
    expected = record.get("mergeCommit")
    if (not isinstance(commit, dict) or not isinstance(expected, dict)
            or commit.get("oid") != expected.get("oid")
            or not isinstance(commit.get("oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", commit["oid"])
            or not isinstance(commit.get("message"), str)):
        raise Refusal("closing-effect-evidence")
    # This fixed mechanical header references the PR, not a closing target.
    # Its branch and the remaining generated title/body are checked separately.
    header = f"Merge pull request #{pr} from {config['repository'].split('/')[0]}/{record['headRefName']}\n\n"
    message = commit["message"]
    if message.startswith(header):
        message = message[len(header):]
    restricted_closing_text(message, issue)


def merged_body_unchanged(kernel, record: dict, pr: int, head: str) -> None:
    """Use GitHub's body-edit evidence, not a local receipt, for merged retries."""
    owner, name = kernel.repo_slug().split("/", 1)
    query = """query($owner:String!,$name:String!,$number:Int!){
      repository(owner:$owner,name:$name){pullRequest(number:$number){
        number state headRefOid body mergedAt lastEditedAt
      }}
    }"""
    result = kernel.run([
        "gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}",
        "-F", f"name={name}", "-F", f"number={pr}",
    ], auth="repository")
    data = json.loads(result.stdout)
    repository = (data.get("data") or {}).get("repository") if isinstance(data, dict) else None
    live = repository.get("pullRequest") if isinstance(repository, dict) else None
    required = {"number", "state", "headRefOid", "body", "mergedAt", "lastEditedAt"}
    if (not isinstance(live, dict) or set(live) != required or data.get("errors")
            or live["number"] != pr or live["state"] != "MERGED" or live["headRefOid"] != head
            or live["body"] != record.get("body") or live["mergedAt"] != record.get("mergedAt")):
        raise Refusal("merged-body-evidence")
    try:
        merged = datetime.strptime(live["mergedAt"], "%Y-%m-%dT%H:%M:%SZ")
        edited = live["lastEditedAt"]
        if edited is not None and datetime.strptime(edited, "%Y-%m-%dT%H:%M:%SZ") >= merged:
            raise Refusal("merged-body-edited")
    except (ValueError, TypeError):
        raise Refusal("merged-body-evidence") from None


def admitted_issue(kernel, record: dict, config: dict, pr: int, head: str) -> int:
    issue = validate_live_record(kernel, record, config, pr, head)
    if record.get("state") == "MERGED":
        merged_body_unchanged(kernel, record, pr, head)
    validate_closing_effects(kernel, record, config, pr, issue)
    return issue


def scope_refusal(kernel) -> type:
    """Scope guards raise an error that the sealed helper's handlers also catch.

    Canonical merge() supersedes a posted authorization only for KernelError, so
    a refusal inside its rereads must be one. The class derives from the imported,
    already sealed helper's KernelError; nothing is imported here.
    """
    base = getattr(kernel, "KernelError", None)
    if not isinstance(base, type) or not issubclass(base, Exception) or issubclass(base, Refusal):
        raise Refusal("kernel-error-contract")
    return type("ScopeRefusal", (Refusal, base), {"__module__": __name__})




def catchable_signals() -> set:
    """Handle every catchable default-terminating/stopping signal on this OS."""
    ignored = {signal.SIGKILL, signal.SIGSTOP, signal.SIGCHLD, signal.SIGCONT,
               signal.SIGWINCH, signal.SIGURG}
    if hasattr(signal, "SIGINFO"):
        ignored.add(signal.SIGINFO)
    return signal.valid_signals() - ignored


def authority_post(original_post, head, conclusion, summary, scope_error):
    try:
        return original_post(head, conclusion, summary)
    except Refusal:
        raise
    except Exception:
        raise scope_error("authority-unavailable") from None


def public_failure(exc: Exception, kernel=None) -> str:
    if isinstance(exc, Refusal):
        return str(exc)
    if getattr(exc, "gate", None):
        return exc.gate
    if kernel is not None and isinstance(exc, kernel.KernelError):
        return "kernel-refused"
    return "authority-unavailable"

def cancel_pending(original_post, head, scope_error):
    # cancellation_guard blocks the complete signal set before reaching here.
    try:
        original_post(head, "failure", "Restricted request failed; re-run merge_pr.py")
    except Exception:
        raise scope_error("authorization-revocation-unavailable") from None


def confirmed_merge(record: dict, pr: int, head: str) -> bool:
    return (record.get("number") == pr and record.get("state") == "MERGED"
            and record.get("headRefOid") == head and bool(record.get("mergedAt")))


@contextmanager
def cancellation_guard(kernel, head: str, scope_error, before_success=None):
    """Cancel attempted authorization on catchable interruption or failure."""
    original_post = kernel.merge_authority.post
    authorization_pending = False
    previous_signals = {}
    signals = catchable_signals()
    interrupted_once = cleaning = False

    def interrupted(_number, _frame):
        nonlocal interrupted_once
        # Another Python handler may already be queued when SIG_BLOCK takes
        # effect. It must return, even if dispatched reentrantly inside that call.
        if interrupted_once or cleaning:
            return
        interrupted_once = True
        signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        raise scope_error("request-interrupted")

    def scoped_post(post_head, conclusion, summary):
        nonlocal authorization_pending
        if post_head != head:
            raise scope_error("current-head")
        if conclusion == "success":
            if before_success is not None:
                before_success()
            # Mark before the call: interruption may occur after GitHub records
            # success but before the adapter or canonical helper returns it.
            authorization_pending = True
        result = authority_post(original_post, post_head, conclusion, summary, scope_error)
        if conclusion == "failure":
            authorization_pending = False
        return result

    def merge_confirmed():
        nonlocal authorization_pending
        authorization_pending = False

    # Install and restore handlers under one mask, never a sequential SIG_IGN
    # window. Save the caller's mask once; the first interruption only adds blocks.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        try:
            kernel.merge_authority.post = scoped_post
            for number in sorted(signals):
                previous_signals[number] = signal.signal(number, interrupted)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            yield merge_confirmed
        finally:
            # If the FIRST signal interrupts this assignment itself, its handler
            # has set interrupted_once and blocked delivery. The outer finally
            # still runs, so compensation cannot be skipped at cleanup entry.
            cleaning = True
    finally:
        # Also cover ordinary exceptions: a signal dispatched while entering
        # cleanup cannot raise out of the compensation path. After this flag,
        # queued Python handlers return; OS delivery is blocked as a single set.
        signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            if authorization_pending:
                cancel_pending(original_post, head, scope_error)
        finally:
            kernel.merge_authority.post = original_post
            try:
                for number, handler in previous_signals.items():
                    signal.signal(number, handler)
            finally:
                # Pending signals now use the originals, after compensation.
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)



def checked_admission(kernel, record, config, pr, head, scope_error):
    try:
        return admitted_issue(kernel, record, config, pr, head)
    except Refusal as exc:
        # Scope changes inside the canonical submission block reach revocation.
        raise scope_error(str(exc)) from None


def scoped_issue_action(original, admitted, scope_error, before=None):
    def action(numbers, *args, **kwargs):
        if admitted() is None or numbers != [admitted()]:
            raise scope_error("authorized-issue-changed")
        if before is not None:
            before()
        return original(numbers, *args, **kwargs)
    return action


def execute_request(kernel, config: dict, pr: int, head: str, check: bool) -> dict:
    """Bind every canonical reread to this request's admitted issue and head."""
    scope_error = scope_refusal(kernel)
    original_pull = kernel.pull_request
    original_gate = kernel.issue_gate
    original_close = kernel.close_out
    original_run = kernel.run
    admitted = None
    latest_record = None

    def recheck_effects():
        issue = checked_admission(kernel, latest_record, config, pr, head, scope_error)
        if issue != admitted:
            raise scope_error("authorized-issue-changed")

    def scoped_run(argv, *args, **kwargs):
        if list(argv)[:3] == ["gh", "pr", "merge"]:
            recheck_effects()
        return original_run(argv, *args, **kwargs)

    def scoped_pull(number):
        nonlocal admitted, latest_record
        if number != pr:
            raise scope_error("current-head")
        record = original_pull(number)
        if confirmed_merge(record, pr, head):
            confirm_merge()
        issue = checked_admission(kernel, record, config, pr, head, scope_error)
        if admitted is None:
            admitted = issue
        elif issue != admitted:
            raise scope_error("authorized-issue-changed")
        latest_record = dict(record)
        return record

    # The process lock admits one request. Temporary wrappers add the external
    # operator scope to each read and close-out without replacing a canonical gate.
    kernel.pull_request = scoped_pull
    kernel.issue_gate = scoped_issue_action(original_gate, lambda: admitted, scope_error)
    kernel.close_out = scoped_issue_action(original_close, lambda: admitted, scope_error, recheck_effects)
    kernel.run = scoped_run
    try:
        with cancellation_guard(kernel, head, scope_error, recheck_effects) as confirm_merge:
            record = kernel.pull_request(pr)
            if record.get("state") == "MERGED":
                if check:
                    raise Refusal("already-merged")
                # GitHub body-edit evidence is rechecked on each merged reread.
                return kernel.finalize_queued(pr, head)
            return kernel.merge(pr, head, dry_run=check)
    finally:
        kernel.pull_request = original_pull
        kernel.issue_gate = original_gate
        kernel.close_out = original_close
        kernel.run = original_run


def main(argv: list[str]) -> int:
    lock, kernel = None, None
    previous_umask = os.umask(0o077)
    try:
        pr, head, check = parse_request(argv)
        if sys.platform != "darwin" or Path(__file__).absolute() != ROOT / "request.py":
            raise Refusal("not-installed")
        verify_resource_limits()
        config_bytes = protected_bytes(ROOT / "config.json", {0})
        config = validate_config(json.loads(config_bytes))
        verify_installation(config)
        if not check:
            verify_enablement(config_bytes)
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
        gate = public_failure(exc, kernel)
        print(json.dumps({"refused": True, "gate": gate}))
        return 2
    finally:
        if lock is not None:
            os.close(lock)
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
