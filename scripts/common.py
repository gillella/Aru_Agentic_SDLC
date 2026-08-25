#!/usr/bin/env python3
# +60 for the #344 terminal merge lease shared by all four helpers.
# line-ceiling: 1543
"""
common.py - Shared GitHub and Git automation utilities for Aru_Agentic_SDLC scripts.
Provides robust execution of gh CLI commands, git worktree management, and API wrappers.
"""

import fnmatch
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from github_inventory import (
    board_agent_identities as rest_board_agent_identities,
    local_repo_slug,
    open_issues as rest_open_issues,
)


VERIFICATION_EVIDENCE_SCHEMA = "aru.verification.v1"
VERIFICATION_EVIDENCE_START = "<!-- aru-verification-evidence:v1 -->"
VERIFICATION_EVIDENCE_END = "<!-- /aru-verification-evidence -->"

_SENSITIVE_ARGUMENT_NAMES = {
    "api-key", "apikey", "auth", "credential", "credentials", "key",
    "password", "passwd", "secret", "sig", "signature", "token",
}

_OPAQUE_VALUE_OPTIONS = {"-c", "--command", "-Command", "-e", "--eval"}


def _looks_sensitive(name: str) -> bool:
    normalized = name.lstrip("-").replace("_", "-").lower()
    return any(part in _SENSITIVE_ARGUMENT_NAMES for part in normalized.split("-"))


def _redact_local_path(value: str) -> str:
    """Removes absolute filesystem locations while keeping a useful basename."""
    if not value:
        return value
    sanitized_url = _sanitize_url(value)
    if sanitized_url is not None:
        return sanitized_url
    for separator in ("=", ":"):
        prefix, found, suffix = value.partition(separator)
        if found:
            sanitized_url = _sanitize_url(suffix)
            if sanitized_url is not None:
                return f"{prefix}{separator}{sanitized_url}"
    if Path(value).is_absolute():
        return f"<local-path>/{Path(value).name}" if Path(value).name else "<local-path>"
    if value.startswith("@") and Path(value[1:]).is_absolute():
        name = Path(value[1:]).name
        return f"@<local-path>/{name}" if name else "@<local-path>"
    for separator in ("=", ":"):
        prefix, found, suffix = value.partition(separator)
        if found and Path(suffix).is_absolute():
            name = Path(suffix).name
            replacement = f"<local-path>/{name}" if name else "<local-path>"
            return f"{prefix}{separator}{replacement}"
    if value.startswith("-I/"):
        return f"-I<local-path>/{Path(value[2:]).name}"
    absolute_path = re.compile(
        r"(?P<prefix>^|[\s@=:,(\[{\"'])(?P<path>/(?:[^/\s\"']+/)*[^/\s\"']+)"
    )

    def replace_path(match: re.Match) -> str:
        path = match.group("path")
        name = Path(path).name
        replacement = f"<local-path>/{name}" if name else "<local-path>"
        return f"{match.group('prefix')}{replacement}"

    return absolute_path.sub(replace_path, value)


def _redact_embedded_secrets(value: str) -> str:
    """Redacts common credentials embedded in otherwise opaque argv values."""
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+", r"\1 <redacted>", value)
    value = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]+\b", "<redacted>", value)
    pattern = re.compile(
        r"(?i)(api[-_]?key|auth(?:orization)?|credential|password|passwd|secret|signature|token)"
        r"(?P<separator>[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\s,;\"'}]+)"
    )
    return pattern.sub(lambda match: f"{match.group(1)}{match.group('separator')}<redacted>", value)


def _sanitize_url(value: str) -> Optional[str]:
    """Redacts URL credentials, sensitive query values, and local file paths."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if not parsed.scheme or (not parsed.netloc and parsed.scheme != "file"):
        return None
    if parsed.scheme == "file":
        basename = Path(parsed.path).name
        suffix = f"/{basename}" if basename else ""
        return f"file://<local-path>{suffix}"

    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        host = f"{hostname}:{port}" if port is not None else hostname
        netloc = f"<redacted>@{host}"

    query = urlencode([
        (name, "<redacted>" if _looks_sensitive(name) else val)
        for name, val in parse_qsl(parsed.query, keep_blank_values=True)
    ], doseq=True)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def sanitize_command(cmd: List[str]) -> List[str]:
    """Redacts common secret arguments and absolute local paths from evidence."""
    sanitized = []
    redact_next = False
    redact_header_next = False
    redact_opaque_next = False
    for raw_arg in cmd:
        arg = str(raw_arg)
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if redact_opaque_next:
            sanitized.append("<redacted>")
            redact_opaque_next = False
            continue
        if redact_header_next:
            name, separator, _value = arg.partition(":")
            sanitized.append(f"{name}: <redacted>" if separator else "<redacted>")
            redact_header_next = False
            continue
        if arg in {"-H", "--header"}:
            sanitized.append(arg)
            redact_header_next = True
            continue
        if arg.startswith("--header="):
            name, separator, _value = arg[len("--header="):].partition(":")
            sanitized.append(f"--header={name}: <redacted>" if separator else "--header=<redacted>")
            continue
        if arg.startswith("-H") and len(arg) > 2:
            name, separator, _value = arg[2:].partition(":")
            sanitized.append(f"-H{name}: <redacted>" if separator else "-H<redacted>")
            continue
        if arg in _OPAQUE_VALUE_OPTIONS:
            sanitized.append(arg)
            redact_opaque_next = True
            continue
        attached_opaque = next(
            (option for option in _OPAQUE_VALUE_OPTIONS if arg.startswith(option) and len(arg) > len(option)),
            None,
        )
        if attached_opaque:
            separator = "=" if arg[len(attached_opaque):].startswith("=") else ""
            sanitized.append(f"{attached_opaque}{separator}<redacted>")
            continue
        name, separator, _value = arg.partition("=")
        if separator and _looks_sensitive(name):
            sanitized.append(f"{name}=<redacted>")
            continue
        if arg.startswith("-") and _looks_sensitive(arg):
            sanitized.append(arg)
            redact_next = True
            continue
        sanitized.append(_redact_embedded_secrets(_redact_local_path(arg)))
    return sanitized


def run_cmd(
    cmd: List[str],
    check: bool = True,
    cwd: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, str, str]:
    """Runs a command and optionally appends sanitized verification evidence."""
    started = time.monotonic()
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
        if check and res.returncode != 0:
            print(f"[ERROR] Command failed ({' '.join(cmd)}):\n{res.stderr.strip()}", file=sys.stderr)
        code, stdout, stderr = res.returncode, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        if check:
            print(f"[EXCEPT] Exception running command ({' '.join(cmd)}): {e}", file=sys.stderr)
        code, stdout, stderr = 1, "", str(e)
    if evidence is not None:
        evidence.append({
            "command": sanitize_command(cmd),
            "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
            "exit_code": code,
            "status": "passed" if code == 0 else "failed",
        })
    return code, stdout, stderr


def get_current_commit() -> str:
    """Returns the checked-out commit SHA, or an empty string on failure."""
    code, stdout, _ = run_cmd(["git", "rev-parse", "HEAD"], check=False)
    return stdout.strip() if code == 0 else ""


def get_agent_id() -> Optional[str]:
    """Returns the active agent ID from environment variables, or None if unset."""
    for var in ("ARU_AGENT_ID", "AGENT_ID", "ARU_AGENT", "AGENT"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


def _parse_terminal_trailers(message: str) -> Tuple[str, List[str]]:
    """Splits a commit message into the main content (subject/body) and terminal trailer lines.

    According to Git trailer conventions:
    - Trailers appear in a contiguous block at the end of the message.
    - Each trailer line matches `<Token>: <value>`.
    - The first line (subject) is never a trailer.
    - If the terminal paragraph contains any non-trailer lines, the entire paragraph is body prose.
    """
    raw_lines = message.rstrip().splitlines()
    if not raw_lines:
        return "", []

    # Find the last paragraph (separated by blank lines)
    idx = len(raw_lines) - 1
    while idx >= 0 and not raw_lines[idx].strip():
        idx -= 1

    if idx <= 0:
        # Only 1 line (subject) or empty
        return "\n".join(raw_lines).rstrip(), []

    paragraph_end = idx
    while idx >= 0 and raw_lines[idx].strip():
        idx -= 1
    paragraph_start = idx + 1

    # If paragraph_start == 0, the entire message is one paragraph (subject + body or subject only).
    # The first line is the subject, so it cannot be a trailer block unless separated by a blank line.
    if paragraph_start == 0:
        return "\n".join(raw_lines).rstrip(), []

    candidate_lines = raw_lines[paragraph_start:paragraph_end + 1]
    trailer_regex = re.compile(r"^[A-Za-z0-9_-]+:\s*.+$")

    # Every line in the terminal paragraph must match trailer_regex
    if not all(trailer_regex.match(line.strip()) for line in candidate_lines):
        return "\n".join(raw_lines).rstrip(), []

    body = "\n".join(raw_lines[:paragraph_start]).rstrip()
    trailers = [line.strip() for line in candidate_lines]
    return body, trailers


def format_commit_message(message: str, agent: Optional[str] = None) -> str:
    """Formats a git commit message with standard trailers.

    If an agent ID is provided or resolved from the environment, attaches an
    'Agent: <id>' trailer if not already present in the terminal trailer block.
    """
    msg = message.strip()
    if not msg:
        return msg

    agent_id = agent.strip() if agent else (get_agent_id() or "")
    if not agent_id:
        return msg

    trailer = f"Agent: {agent_id}"
    body, trailers = _parse_terminal_trailers(msg)

    # Check if Agent trailer is already present in the terminal trailer block
    if any(re.match(r"^agent\s*:", t, re.IGNORECASE) for t in trailers):
        return msg

    if trailers:
        trailers.append(trailer)
        return body + "\n\n" + "\n".join(trailers)

    if body:
        return body + "\n\n" + trailer

    return msg + "\n\n" + trailer


def run_gh_json(cmd: List[str]) -> Optional[Any]:
    """Runs a gh CLI command and parses JSON output."""
    code, stdout, stderr = run_cmd(cmd, check=False)
    if code != 0 or not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        print(f"[WARN] Failed to parse JSON from gh CLI: {stdout}", file=sys.stderr)
        return None


def get_current_branch() -> str:
    """Returns the current git branch name."""
    _, stdout, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return stdout or "main"


WORKTREE_ROOT = ".worktrees"
# Separator between the branch slug and the owning agent in a worktree path.
# Doubled so it cannot occur inside a sanitised branch slug or agent id.
WORKTREE_AGENT_SEP = "__"
# Path length matters on macOS and Linux, and branch slugs are already long.
WORKTREE_AGENT_MAXLEN = 24


def worktree_agent_component(agent: str) -> str:
    """Filesystem-safe, short form of an agent id for use in a path.

    Dots are not preserved: an agent id is untrusted enough that leaving '..'
    intact in a path component invites a traversal for no benefit.

    Sanitising and truncating are both lossy, so two distinct ids can reduce to
    one component -- `claude.1` and `claude-1`, or any pair sharing a long
    prefix. That would put two agents back in one directory, which is precisely
    the data-integrity defect this scoping exists to prevent, so a lossy
    reduction carries a short digest of the id as given.
    """
    raw = str(agent)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if slug == raw and len(slug) <= WORKTREE_AGENT_MAXLEN:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    head = slug[:WORKTREE_AGENT_MAXLEN - len(digest) - 1].strip("-")
    return f"{head}-{digest}" if head else digest


def worktree_path_for(branch_name: str, agent: str = "") -> str:
    """The directory this agent uses for this branch.

    The owning agent is encoded in the path rather than recorded inside the
    worktree, so ownership cannot be read wrong and no marker file can be swept
    into somebody's commit. Two agents in one clone therefore never derive the
    same directory (#305). Calls that name no agent keep the historical
    unscoped path, so worktrees created before this change still resolve.
    """
    base = os.path.join(WORKTREE_ROOT, branch_name.replace("/", "-"))
    component = worktree_agent_component(agent) if agent else ""
    return f"{base}{WORKTREE_AGENT_SEP}{component}" if component else base


def worktree_agent_of(path: str) -> str:
    """The agent encoded in a worktree path, or '' for an unscoped one."""
    tail = os.path.basename(os.path.normpath(path))
    _, sep, component = tail.rpartition(WORKTREE_AGENT_SEP)
    return component if sep else ""


def worktree_holding_branch(branch_name: str) -> Optional[str]:
    """Path of the worktree that currently has ``branch_name`` checked out.

    Returns None when no worktree holds it, or when the listing cannot be read
    -- callers treat an unreadable listing as "no known holder" and let git
    itself refuse, rather than blocking on a transient failure.
    """
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False)
    if code != 0:
        return None
    current = None
    for line in (out or "").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref in (f"refs/heads/{branch_name}", branch_name):
                return current
    return None


def _describe_worktree_holder(holder: str) -> str:
    agent = worktree_agent_of(holder)
    return f"'{holder}' (agent '{agent}')" if agent else f"'{holder}' (no agent recorded)"


def create_worktree(branch_name: str, path: str = None, attempts: int = 5,
                    agent: str = "") -> Optional[str]:
    """Creates a git worktree for isolated feature development/review.

    Returns the worktree path, or None if one could not be established. The old
    contract returned the path unconditionally, so a caller that lost the race
    for a shared directory was handed another agent's checkout and committed
    out of it -- the #305 data-integrity defect, where one agent's commit
    carried another's uncommitted files.

    Retries with backoff: concurrent agents in one clone contend on
    .git/index.lock, and `git worktree add` fails transiently rather than
    waiting.
    """
    if not path:
        path = worktree_path_for(branch_name, agent)

    # Never adopt a directory we did not resolve for ourselves. git allows only
    # one worktree per branch, so a holder at any other path is somebody else's
    # working directory -- refuse and name it instead of silently sharing it.
    holder = worktree_holding_branch(branch_name)
    if holder and os.path.realpath(holder) != os.path.realpath(path):
        print(f"[ERROR] Branch '{branch_name}' is already checked out at "
              f"{_describe_worktree_holder(holder)}, not at '{path}'. Refusing to "
              "share another agent's worktree.", file=sys.stderr)
        return None
    if holder:
        print(f"✅ Reattached to existing worktree at: '{path}'")
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)

    stderr = ""
    for attempt in range(attempts):
        code, _, stderr = run_cmd(["git", "worktree", "add", "-b", branch_name, path], check=False)
        if code == 0:
            print(f"✅ Git worktree initialized at: '{path}'")
            return path

        # Branch already exists (resume) - attach the worktree to it instead.
        code2, _, _ = run_cmd(["git", "worktree", "add", path, branch_name], check=False)
        if code2 == 0:
            print(f"✅ Git worktree attached to existing branch at: '{path}'")
            return path

        if "lock" not in stderr.lower() or attempt == attempts - 1:
            break
        sleep_s = 0.5 * (2 ** attempt) + random.random() * 0.3
        print(f"[INFO] git lock contention; retrying in {sleep_s:.1f}s "
              f"({attempt + 1}/{attempts})", file=sys.stderr)
        time.sleep(sleep_s)

    print(f"[ERROR] Could not create worktree at '{path}': {stderr}", file=sys.stderr)
    return None


def board_agent_identities() -> Tuple[Optional[Dict[str, List[str]]], str]:
    """Agent ids GitHub currently shows in use, mapped to where they are held.

    GitHub is the system of record for liveness; the presence registry is a
    local cache of intent with a 300-second heartbeat TTL. A session that missed
    a heartbeat -- or never registered at all -- looked free to the registry
    while the board still showed it holding an issue claim and authoring an open
    PR, so its id was handed to a second session and every downstream identity
    guarantee degraded (#304).

    Two REST endpoints are used because GitHub exposes issues and pull requests
    separately.  Keeping this lightweight read off GraphQL prevents identity
    resolution from consuming the Projects/review query budget.

    Returns (holders, error). ``holders`` is None when the board could not be
    read, so callers fail closed rather than assign a possibly-held id.
    """
    slug = get_repo_slug()
    if not slug:
        return None, "could not resolve the repository from the local origin"
    return rest_board_agent_identities(run_cmd, slug)


def query_open_issues() -> Optional[List[Dict[str, Any]]]:
    """Fetch open issues through paginated REST, preserving failure as ``None``.

    ``gh issue list`` uses GraphQL and historically spent quota on a payload the
    REST Issues endpoint already provides.  Filtering pull requests in jq and
    paginating explicitly keeps this inventory complete without consuming the
    GraphQL budget needed for Projects and review threads.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    return rest_open_issues(run_cmd, slug)


def list_open_issues() -> List[Dict[str, Any]]:
    """Compatibility wrapper for issue pickers that historically consume a list.

    Authoritative callers that must distinguish an empty repository from an
    infrastructure failure use :func:`query_open_issues` directly.
    """
    return query_open_issues() or []


# --- Concurrency primitives -----------------------------------------------
# GitHub offers no compare-and-swap on issue assignment, so claiming is
# optimistic: write, read back, and resolve any race with a deterministic
# tie-break on agent id. See claim_issue.py for the protocol.

AGENT_LABEL_PREFIX = "agent:"
ACTIVE_STATUS_LABELS = {"status:in-progress", "status:in-review"}


def label_names(issue: Dict[str, Any]) -> List[str]:
    return [label.get("name", "") for label in issue.get("labels", [])]


def agent_labels(issue: Dict[str, Any]) -> List[str]:
    """Returns every agent:* label on an issue. More than one means a race."""
    return sorted(n for n in label_names(issue) if n.startswith(AGENT_LABEL_PREFIX))


def claimed_by(issue: Dict[str, Any]) -> Optional[str]:
    """Returns the winning agent id for an issue, or None if unclaimed.

    When two agents raced, the lowest-sorting label wins. Both agents compute
    the same winner from the same data, so no coordinator is required.

    An issue claim (agent:<id>) represents active In Progress implementation
    only. In Review ignores the label as a claim while retaining it as a legacy
    authorship backstop; Done removes it during close-out.
    """
    names = set(label_names(issue))
    if {"status:in-review", "status:done"} & names:
        return None
    labels = agent_labels(issue)
    if labels:
        return labels[0][len(AGENT_LABEL_PREFIX):]
    if "status:in-progress" in names:
        return "unknown"  # in flight but pre-dates agent labelling
    return None


def ensure_label(name: str, color: str = "5319e7", description: str = "") -> bool:
    """Creates a label if absent. gh issue edit --add-label fails on unknown
    labels, and agent:* labels are created on demand."""
    code, _, _ = run_cmd(
        ["gh", "label", "create", name, "--color", color, "--description", description, "--force"],
        check=False,
    )
    return code == 0


# Issue metadata is data, not a shell. Globs (`scripts/*`) are valid touches;
# command operators and traversal are not. `*` `?` `[` stay allowed for globs.
_METADATA_COMMAND_RE = re.compile(r"""[;&|`$()<>\n\r!\\]|&&|\|\|""")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

_FENCE_RE = re.compile(r"(`{3,}|~{3,})")


def strip_code_blocks(body: str) -> str:
    """Blanks fenced and indented code blocks, preserving line positions.

    Issue metadata parsers anchor to the start of a line so a code *span* cannot
    hijack them, but a fenced code *block* also begins at column 0, and an
    indented (4-space or tab) block is indistinguishable from an ordinary
    declaration to a ``^[ \\t]*`` anchor. An issue that quotes the issue
    template as an example would otherwise have the example's ``touches:`` and
    ``depends-on:`` parsed as its own declaration: reserving paths it will
    never edit while leaving its real paths unreserved (a two-agent collision)
    and masking real prerequisites with ``depends-on: none`` (blocked work
    that looks claimable).

    An unterminated fence blanks the remainder of the body. That is the safe
    direction: a missing declaration makes an issue non-claimable, whereas a
    wrong one causes collisions.
    """
    if not body:
        return body
    lines = []
    open_fence = None
    for line in body.splitlines():
        indented = line.startswith(("    ", "\t"))
        match = _FENCE_RE.match(line.lstrip())
        # Normalise to the fence character: a closing fence must use the same
        # character as the one that opened the block.
        token = match.group(1)[0] if match else None
        if open_fence is None:
            if token is not None:
                open_fence = token
                lines.append("")
                continue
            if indented:
                lines.append("")
                continue
            lines.append(line)
        else:
            if token == open_fence:
                open_fence = None
            lines.append("")
    return "\n".join(lines)


def _is_plausible_declared_path(path: str) -> bool:
    """True when a ``touches:`` token is a real tree reference, not prose.

    The line-anchored parse can still grab a prose tail ("declaration - every
    issue about the touches system naturally discusses") or a quoted wrapper,
    so an entry carrying embedded whitespace, a quote, or an em dash cannot be
    a path and must not reserve anything. Legit references - `src/a.py`,
    `tests/*`, `docs/guide.md`, `**/*.py` - carry none of those.
    """
    if any(ch.isspace() for ch in path):
        return False
    if '"' in path or "'" in path or "—" in path or "–" in path:
        return False
    return True


def metadata_line_is_command_like(text: str) -> bool:
    """True when a metadata line contains shell operators, not path/issue tokens."""
    return bool(_METADATA_COMMAND_RE.search(text or ""))


def declared_path_is_safe(path: str) -> bool:
    """True if a `touches:` token is a relative repo path, not a command."""
    value = (path or "").strip().strip("`")
    if not value or value.startswith("/") or value.startswith("~"):
        return False
    if _WINDOWS_ABS_RE.match(value):
        return False
    if _METADATA_COMMAND_RE.search(value):
        return False
    parts = re.split(r"[\\/]", value)
    if any(part in {".", ".."} for part in parts):
        return False
    return True


TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TRUSTED_REWRITE_LABEL = "trusted-rewrite"


def _actor_login(record: Any) -> Optional[str]:
    if isinstance(record, dict):
        login = record.get("login")
        return login if isinstance(login, str) and login else None
    if isinstance(record, str) and record:
        return record
    return None


def author_login(issue: Dict[str, Any]) -> Optional[str]:
    """Returns the GitHub login that authored an issue list record, if present."""
    if not isinstance(issue, dict):
        return None
    return _actor_login(issue.get("author"))


def editor_login(issue: Dict[str, Any]) -> Optional[str]:
    """Returns the last body editor login when the payload includes one."""
    if not isinstance(issue, dict):
        return None
    return _actor_login(issue.get("editor"))


def repository_owner_login(slug: Optional[str] = None) -> Optional[str]:
    """Owner half of `owner/repo`, or None when identity cannot be resolved."""
    resolved = slug if slug is not None else get_repo_slug()
    if not resolved or "/" not in resolved:
        return None
    owner = resolved.split("/", 1)[0].strip()
    return owner or None


def repository_trusted_logins(slug: Optional[str] = None) -> Optional[Set[str]]:
    """Owner plus collaborator logins, or None when identity cannot be resolved."""
    resolved = slug if slug is not None else get_repo_slug()
    owner = repository_owner_login(resolved)
    if not owner or not resolved:
        return None
    code, stdout, _ = run_cmd(
        [
            "gh", "api", "--paginate",
            f"repos/{resolved}/collaborators",
            "--jq", ".[].login",
        ],
        check=False,
    )
    if code != 0:
        return None
    logins = {owner.lower()}
    logins.update(line.strip().lower() for line in stdout.splitlines() if line.strip())
    return logins


def _association_value(issue: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = issue.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _login_is_authorized(
    login: Optional[str],
    *,
    owner: Optional[str] = None,
    trusted_logins: Optional[Iterable[str]] = None,
    association: Optional[str] = None,
) -> bool:
    if not login:
        return False
    if association and association.upper() in TRUSTED_AUTHOR_ASSOCIATIONS:
        return True
    names = {name.lower() for name in (trusted_logins or []) if name}
    if owner:
        names.add(owner.lower())
    return login.lower() in names


def has_trusted_rewrite_label(issue: Dict[str, Any]) -> bool:
    """True when a write-access actor attested the current issue body."""
    return TRUSTED_REWRITE_LABEL in {name.lower() for name in label_names(issue)}


def is_trusted_metadata_author(
    issue: Dict[str, Any],
    owner: Optional[str] = None,
    trusted_logins: Optional[Iterable[str]] = None,
) -> bool:
    """True when issue metadata may be honoured as `touches:` / `depends-on:`.

    Fail closed when author identity is missing. Org-owned repositories trust
    collaborators and GitHub associations (OWNER / MEMBER / COLLABORATOR), not
    equality with the organization login. An outsider issue stays untrusted
    until a trusted rewrite is bound to the current body: a last editor who is
    an authorized actor, optionally attested by a `trusted-rewrite` label.
    The label alone is not enough when the last editor is an outsider, or when
    a claim-path identity lookup failed (`trustIdentityResolved` is False).
    """
    if not isinstance(issue, dict):
        return False
    login = author_login(issue)
    if _login_is_authorized(
        login,
        owner=owner,
        trusted_logins=trusted_logins,
        association=_association_value(issue, "authorAssociation", "author_association"),
    ):
        return True
    editor = editor_login(issue)
    editor_is_trusted = _login_is_authorized(
        editor,
        owner=owner,
        trusted_logins=trusted_logins,
        association=_association_value(issue, "editorAssociation", "editor_association"),
    )
    if editor_is_trusted:
        return True
    if has_trusted_rewrite_label(issue):
        # A write-access label attests a rewrite only when the last editor is
        # unknown (list payloads) or is itself an authorized actor.
        # A failed GraphQL lookup omits editor the same way a list payload
        # does; that is not "unknown" and must not honour the label.
        if issue.get("trustIdentityResolved") is False:
            return False
        return editor is None
    return False


def parse_touches(body: str) -> List[str]:
    """Parses 'touches: src/a/*, docs/b.md' from an issue body.

    Declares which paths an issue will modify so the picker can refuse to hand
    two agents work that collides on the same files. `parallel-eligible` only
    means 'no unresolved depends-on'; it says nothing about file conflicts.
    Untrusted input is data: traversal, absolute paths, and command operators
    are dropped rather than executed.
    """
    if not body:
        return []
    # Treat this as issue metadata, not prose.  An unanchored search would
    # parse the first sentence containing ``touches:`` (including Markdown
    # code spans) and silently ignore the real declaration later in the body.
    #
    # [^\n]* rather than a lazy match with a trailing \s*: \s matches newlines,
    # so an *empty* declaration used to run past the line ending and adopt the
    # next line. "touches:\nparallel-eligible: true" reported
    # ['parallel-eligible: true'] as a declared path, which made an issue with
    # no path budget look claimable to build_candidates() while the enforcement
    # hook's stricter parser saw nothing and failed open - so two agents could
    # be handed overlapping files.
    # Ignore code blocks before the line-anchored search: a fenced or indented
    # example quotes the template and would supply its own declaration in place
    # of the issue's, reserving the wrong paths and masking real depends-on
    # edges (issue #294).
    match = re.search(
        r"^[ \t]*[*_`]{0,2}touches[*_`]{0,2}[ \t]*:[ \t]*([^\n]*)",
        strip_code_blocks(body), re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return []
    raw = _unwrap_declared_touches_value(match.group(1))
    # "(github settings only)" and similar prose mean the issue changes nothing
    # in the tree - not that it declared a directory called "(github".
    if raw.startswith("("):
        return []
    if metadata_line_is_command_like(raw):
        return []
    return [
        p.strip().strip("`")
        for p in raw.split(",")
        if p.strip()
        and declared_path_is_safe(p.strip().strip("`"))
        and _is_plausible_declared_path(p.strip().strip("`"))
    ]


def _unwrap_declared_touches_value(raw: str) -> str:
    """Strip wrapping markdown without destroying a repo-wide ``**`` glob."""
    value = (raw or "").strip()
    if value in {"*", "**"}:
        return value
    if value.startswith("**/"):
        return value
    if len(value) >= 2 and value[0] == "`" and value[-1] == "`" and "`" not in value[1:-1]:
        inner = value[1:-1].strip()
        return inner if inner else value
    if value.startswith("**") and value.endswith("**") and len(value) > 4:
        return value[2:-2].strip()
    if value.startswith("**"):
        return value[2:].strip()
    return value


def _norm_path(p: str) -> str:
    return p.strip().strip("/")


def paths_overlap(a: str, b: str) -> bool:
    """True if two path patterns could touch the same file.

    Deliberately errs toward declaring a conflict: a false positive costs
    serialisation, a false negative costs a merge conflict.
    """
    a, b = _norm_path(a), _norm_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    # Directory containment: "docs/contracts/*" vs "docs/contracts/X.md"
    ap = a.split("*")[0].rstrip("/")
    bp = b.split("*")[0].rstrip("/")
    if ap and bp and (ap == bp or ap.startswith(bp + "/") or bp.startswith(ap + "/")):
        return True
    return False


def touches_conflict(a_paths: List[str], b_paths: List[str]) -> Optional[Tuple[str, str]]:
    """Returns the first conflicting pair, or None.

    Empty declarations have no pair to compare; the issue picker rejects them
    before calling this primitive.
    """
    for a in a_paths:
        for b in b_paths:
            if paths_overlap(a, b):
                return (a, b)
    return None


def get_issue(issue_id: int) -> Optional[Dict[str, Any]]:
    """Fetches single issue details via gh CLI, plus GraphQL trust identity."""
    cmd = [
        "gh", "issue", "view", str(issue_id),
        "--json", "number,title,labels,assignees,body,state,author,updatedAt",
    ]
    res = run_gh_json(cmd)
    if not isinstance(res, dict):
        return None
    trust = _issue_trust_identity(issue_id)
    res["trustIdentityResolved"] = trust is not None
    if trust:
        if "editor" in trust:
            res["editor"] = trust["editor"]
        if trust.get("authorAssociation"):
            res["authorAssociation"] = trust["authorAssociation"]
    return res


_ISSUE_TRUST_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      editor { login }
      authorAssociation
    }
  }
}
"""


def _issue_trust_identity(issue_id: int) -> Optional[Dict[str, Any]]:
    """Editor and association fields that `gh issue view --json` cannot return."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={_ISSUE_TRUST_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
        "-F", f"number={issue_id}",
    ]
    payload = run_gh_json(cmd)
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    try:
        node = payload["data"]["repository"]["issue"]
    except (KeyError, TypeError):
        return None
    if not isinstance(node, dict):
        return None
    trust: Dict[str, Any] = {}
    if "editor" in node:
        trust["editor"] = node.get("editor")
    association = node.get("authorAssociation")
    if isinstance(association, str) and association:
        trust["authorAssociation"] = association
    return trust


def fetch_pr_comments(pr_id: int) -> List[Dict[str, Any]]:
    """Fetches inline review comments for a Pull Request."""
    cmd = ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_id}/comments"]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else []


def fetch_issue_comments(issue_id: int) -> List[Dict[str, Any]]:
    """Fetches all comments for an Issue, retrieving all pages."""
    cmd = ["gh", "api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{issue_id}/comments"]
    code, stdout, _ = run_cmd(cmd, check=False)
    if code != 0 or not stdout:
        return []
    comments: List[Dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(stdout):
        while pos < len(stdout) and stdout[pos].isspace():
            pos += 1
        if pos >= len(stdout):
            break
        try:
            doc, end = decoder.raw_decode(stdout, idx=pos)
            if isinstance(doc, list):
                comments.extend(doc)
            elif isinstance(doc, dict):
                comments.append(doc)
            pos = end
        except json.JSONDecodeError:
            break
    return comments


# --- GitHub Project v2 board helpers --------------------------------------
# The board is the monitoring surface; the status:* labels are what the CLI
# reads. Both must move together or they drift. These helpers exist so
# claim_issue.py and update_issue_status.py can move the board item too.


def get_repo_slug() -> Optional[str]:
    """Return ``owner/repo`` from the local origin without spending API quota."""
    return local_repo_slug(run_cmd)


def query_issue_project_items(
    issue_number: int,
) -> Optional[List[Dict[str, Any]]]:
    """Returns project items while preserving GraphQL failures as ``None``."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)

    query = """
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        issue(number:$number) {
          id
          url
          projectItems(first:10) {
            nodes {
              id
              status: fieldValueByName(name:"Status") {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  optionId
                  name
                }
              }
              project {
                id
                number
                title
                repositories(first:100) {
                  nodes { nameWithOwner }
                }
                field(name:"Status") {
                  ... on ProjectV2SingleSelectField {
                    id
                    options { id name }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
        "-F", f"number={issue_number}",
    ]
    res = run_gh_json(cmd)
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        return res["data"]["repository"]["issue"]["projectItems"]["nodes"]
    except (KeyError, TypeError):
        return None


def get_issue_project_items(issue_number: int) -> List[Dict[str, Any]]:
    """Compatibility wrapper for board mutation helpers expecting a list."""
    return query_issue_project_items(issue_number) or []


def get_repo_projects(repo_slug: str) -> Optional[List[Dict[str, Any]]]:
    """Returns Project v2 boards linked to ``owner/repo``.

    Issue creation cannot discover its destination from project items because
    a newly-created issue has none yet.  Resolve from the repository's linked
    projects instead, using the same title/linkage contract as status moves.
    """
    if not repo_slug or "/" not in repo_slug:
        return None
    owner, repo = repo_slug.split("/", 1)
    query = """
    query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        projectsV2(first:100) {
          nodes {
            id
            number
            title
            owner {
              ... on User { login }
              ... on Organization { login }
            }
            repositories(first:100) {
              nodes { nameWithOwner }
            }
          }
        }
      }
    }
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
    ]
    res = run_gh_json(cmd)
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        return res["data"]["repository"]["projectsV2"]["nodes"]
    except (KeyError, TypeError):
        return None


def select_governed_project_items(
    items: List[Dict[str, Any]],
    repo_slug: str,
) -> List[Dict[str, Any]]:
    """Selects only the repository's Aru_Agentic_SDLC project item.

    Issues can appear on several roadmaps.  A status transition must not move
    all of them just because they expose an identically named Status option.
    Prefer the board name created by ``init_project.py``; fall back only when
    exactly one project is linked to this repository.  Ambiguity fails closed.
    """
    repo_name = repo_slug.split("/", 1)[-1]
    expected_title = f"{repo_name} Board".lower()
    linked = []
    for item in items:
        project = item.get("project") or {}
        repositories = (project.get("repositories") or {}).get("nodes") or []
        linked_slugs = {repo.get("nameWithOwner") for repo in repositories}
        if repo_slug in linked_slugs:
            linked.append(item)

    named = [
        item
        for item in linked
        if (item.get("project") or {}).get("title", "").lower() == expected_title
    ]
    if len(named) == 1:
        return named
    if not named and len(linked) == 1:
        return linked
    return []


def select_governed_projects(
    projects: List[Dict[str, Any]],
    repo_slug: str,
) -> List[Dict[str, Any]]:
    """Applies the governed-board selector before an issue has project items."""
    wrapped = [{"project": project} for project in projects]
    return [
        item["project"]
        for item in select_governed_project_items(wrapped, repo_slug)
    ]


def resolve_governed_project(repo_slug: str) -> Optional[Dict[str, Any]]:
    """Resolves the exact ``<repo> Board`` or sole linked project."""
    available = get_repo_projects(repo_slug)
    if available is None:
        print(
            f"[WARN] Could not query project boards for '{repo_slug}'.",
            file=sys.stderr,
        )
        return None
    projects = select_governed_projects(available, repo_slug)
    if len(projects) == 1:
        return projects[0]
    print(
        f"[WARN] Could not identify one governed project board for '{repo_slug}'.",
        file=sys.stderr,
    )
    return None


def attach_issue_to_governed_project(issue_number: int) -> bool:
    """Idempotently attaches an issue to its repository's governed board."""
    slug = get_repo_slug()
    if not slug:
        print(
            f"[WARN] Could not resolve the repository for issue #{issue_number}.",
            file=sys.stderr,
        )
        print(
            "[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            "--owner <OWNER> --url <ISSUE_URL>",
            file=sys.stderr,
        )
        return False

    project = resolve_governed_project(slug)
    if not project:
        print(
            f"[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            f"--owner <OWNER> --url https://github.com/{slug}/issues/{issue_number}",
            file=sys.stderr,
        )
        return False

    project_id = project.get("id")
    existing = get_issue_project_items(issue_number)
    if project_id and any(
        (item.get("project") or {}).get("id") == project_id
        for item in existing
    ):
        return True

    project_number = project.get("number")
    owner = (project.get("owner") or {}).get("login")
    if project_number is None or not owner:
        print(
            f"[WARN] Governed project metadata is incomplete for '{slug}'.",
            file=sys.stderr,
        )
        print(
            f"[WARN] Manual remedy: gh project item-add <PROJECT_NUMBER> "
            f"--owner <OWNER> --url https://github.com/{slug}/issues/{issue_number}",
            file=sys.stderr,
        )
        return False
    return add_issue_to_project(issue_number, int(project_number), owner)


def set_board_status(issue_number: int, status: str) -> bool:
    """Moves an issue's board item(s) to the named Status option.

    Returns True only if at least one board item actually moved, so callers can
    tell the difference between 'moved' and 'issue is not on any board'.
    """
    slug = get_repo_slug()
    if not slug:
        return False
    items = get_issue_project_items(issue_number)
    items = select_governed_project_items(items, slug)
    if not items:
        if not attach_issue_to_governed_project(issue_number):
            return False
        items = select_governed_project_items(
            get_issue_project_items(issue_number), slug
        )
    if not items:
        print(
            f"[WARN] Could not identify one governed project board for '{slug}'.",
            file=sys.stderr,
        )
        return False

    moved = False
    for item in items:
        project = item.get("project") or {}
        field = project.get("field") or {}
        field_id = field.get("id")
        if not field_id:
            continue
        option = next(
            (o for o in field.get("options", []) if o.get("name", "").lower() == status.lower()),
            None,
        )
        if not option:
            print(
                f"[WARN] Project '{project.get('title')}' has no Status option "
                f"'{status}'. Available: {[o['name'] for o in field.get('options', [])]}",
                file=sys.stderr,
            )
            continue

        mutation = """
        mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
          updateProjectV2ItemFieldValue(input:{
            projectId:$project, itemId:$item, fieldId:$field,
            value:{ singleSelectOptionId:$option }
          }) { projectV2Item { id } }
        }
        """
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-F", f"project={project['id']}",
            "-F", f"item={item['id']}",
            "-F", f"field={field_id}",
            "-F", f"option={option['id']}",
        ]
        code, _, err = run_cmd(cmd, check=False)
        if code == 0:
            moved = True
        else:
            print(f"[WARN] Board move failed for project '{project.get('title')}': {err}", file=sys.stderr)
    return moved


def get_issue_priority_field(issue_number: int) -> Optional[str]:
    """Returns the governed Project 'Priority' single-select value (P0..P3).

    Returns ``None`` on any GraphQL failure, missing field, or value, so the
    caller can fail closed instead of guessing. The read is scoped to the
    governed board item — mirroring ``set_issue_priority_field`` and the
    Status helpers — because an issue can appear on several boards and only
    the governed item is the synchronization mirror for ``priority:pN``.
    """
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    query = """
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        issue(number:$number) {
          projectItems(first:10) {
            nodes {
              priority: fieldValueByName(name:"Priority") {
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
              project {
                id
                title
                repositories(first:100) {
                  nodes { nameWithOwner }
                }
              }
            }
          }
        }
      }
    }
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
        "-F", f"number={issue_number}",
    ]
    res = run_gh_json(cmd)
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        nodes = res["data"]["repository"]["issue"]["projectItems"]["nodes"]
    except (KeyError, TypeError):
        return None
    items = select_governed_project_items(nodes or [], slug)
    for item in items:
        value = (item or {}).get("priority") or {}
        name = value.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def set_issue_priority_field(issue_number: int, value: str) -> bool:
    """Sets the governed Project 'Priority' field to a P0..P3 value.

    ``value`` must already be an exact option (e.g. 'P2'). Returns True only
    when at least one board item updated, mirroring ``set_board_status``.
    """
    slug = get_repo_slug()
    if not slug or "/" not in slug or "P" not in value:
        return False
    items = get_issue_project_items(issue_number)
    items = select_governed_project_items(items, slug)
    if not items:
        if not attach_issue_to_governed_project(issue_number):
            return False
        items = select_governed_project_items(
            get_issue_project_items(issue_number), slug
        )
    if not items:
        return False

    updated = False
    for item in items:
        project = item.get("project") or {}
        field = next(
            (f for f in project.get("fields", []) if f.get("name") == "Priority"),
            None,
        )
        if not field:
            continue
        option = next(
            (o for o in field.get("options", []) if o.get("name") == value),
            None,
        )
        if not option:
            continue
        mutation = """
        mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
          updateProjectV2ItemFieldValue(input:{
            projectId:$project, itemId:$item, fieldId:$field,
            value:{ singleSelectOptionId:$option }
          }) { projectV2Item { id } }
        }
        """
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={mutation}",
            "-F", f"project={project['id']}",
            "-F", f"item={item['id']}",
            "-F", f"field={field['id']}",
            "-F", f"option={option['id']}",
        ]
        code, _, err = run_cmd(cmd, check=False)
        if code == 0:
            updated = True
        else:
            print(
                f"[WARN] Priority field update failed for project "
                f"'{project.get('title')}': {err}",
                file=sys.stderr,
            )
    return updated


def add_issue_to_project(issue_number: int, project_number: int, owner: str = "@me") -> bool:
    """Adds an issue to a project board. Idempotent - re-adding is a no-op."""
    slug = get_repo_slug()
    if not slug:
        return False
    url = f"https://github.com/{slug}/issues/{issue_number}"
    code, out, err = run_cmd(
        ["gh", "project", "item-add", str(project_number), "--owner", owner, "--url", url],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not add issue #{issue_number} to project #{project_number}: {err or out}", file=sys.stderr)
        print(
            f"[WARN] Manual remedy: gh project item-add {project_number} "
            f"--owner {owner} --url {url}",
            file=sys.stderr,
        )
        return False
    return True


def parse_semver_major(version_str: Optional[str]) -> Optional[int]:
    """Extracts the MAJOR version number from a SemVer string (e.g., 'v1.2.3' -> 1, 'v0.1.0' -> 0)."""
    if not version_str:
        return None
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", str(version_str).strip())
    if match:
        return int(match.group(1))
    return None


def get_framework_root() -> str:
    """Returns the absolute path to the Aru_Agentic_SDLC framework repository root."""
    env_home = os.environ.get("ARU_SDLC_HOME")
    if env_home and os.path.isdir(env_home):
        return os.path.abspath(env_home)
    # Fallback to the repository root containing this module (parent directory of scripts/)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_current_framework_version(repo_root: Optional[str] = None) -> str:
    """Returns the current framework version from git tags or fallback."""
    cwd = repo_root or get_framework_root()
    code, stdout, _ = run_cmd(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"], check=False, cwd=cwd)
    if code == 0 and stdout:
        return stdout.strip()
    return "v0.1.0"


def check_version_compatibility(
    expected_ref: Optional[str] = None,
    current_version: Optional[str] = None,
) -> bool:
    """Warns (never hard-fails) on MAJOR SemVer mismatch between expected ref and current version.

    Always returns True (never raises or exits non-zero).
    """
    ref = expected_ref or os.environ.get("ARU_SDLC_REF")
    if not ref:
        return True

    expected_major = parse_semver_major(ref)
    if expected_major is None:
        return True

    curr_ver = current_version or get_current_framework_version()
    current_major = parse_semver_major(curr_ver)
    if current_major is None:
        return True

    if expected_major != current_major:
        print(
            f"[WARN] Framework version mismatch: ARU_SDLC_REF specifies '{ref}' "
            f"(MAJOR {expected_major}), but current repository ref is '{curr_ver}' "
            f"(MAJOR {current_major}). Continuing execution.",
            file=sys.stderr,
        )
    return True


TERMINAL_MERGE_LEASE_LABEL = "lease:merged"


def terminal_merge_lease(branch: str) -> Optional[Dict[str, Any]]:
    """Resolve a head branch to its terminal merged lease, or None.

    After a governed merge accepts an exact head, that branch name is spent:
    any later push to it is a stale worker producing an ungoverned orphan
    commit, not new work (#344, observed on hermes PR #89). The lease is the
    predicate that lets every helper tell those apart.

    Derived from merged-PR state rather than a separate store, so it cannot
    disagree with GitHub about whether a merge happened, and so it applies to
    PRs merged before this landed with no backfill. Returns None when the
    branch was never merged. Fails closed -- returning a lease -- when the
    lookup is unreadable or matches several merged PRs, because "cannot tell"
    must block continuation rather than permit it.
    """
    if not branch or not isinstance(branch, str):
        return None
    rows = run_gh_json([
        "gh", "pr", "list", "--state", "merged", "--head", branch, "--limit", "20",
        "--json", "number,headRefName,headRefOid,mergeCommit,mergedAt,author",
    ])
    if rows is None:
        return {"branch": branch, "unreadable": True, "pr": None,
                "gated_sha": None, "merged_sha": None, "holder": None}
    exact = [r for r in rows if isinstance(r, dict) and r.get("headRefName") == branch]
    if not exact:
        return None
    if len(exact) > 1:
        return {"branch": branch, "ambiguous": sorted(r.get("number") for r in exact),
                "pr": None, "gated_sha": None, "merged_sha": None, "holder": None}
    row = exact[0]
    return {
        "branch": branch,
        "pr": row.get("number"),
        "gated_sha": row.get("headRefOid"),
        "merged_sha": (row.get("mergeCommit") or {}).get("oid"),
        "holder": (row.get("author") or {}).get("login"),
        "merged_at": row.get("mergedAt"),
    }


def terminal_lease_refusal(lease: Dict[str, Any], attempted: str) -> str:
    """One refusal message shared by every door a stale writer can knock on."""
    if lease.get("unreadable"):
        return (f"Cannot determine whether branch {lease['branch']!r} was already merged; "
                f"refusing to {attempted} rather than risk continuing merged work.")
    if lease.get("ambiguous"):
        return (f"Branch {lease['branch']!r} matches several merged PRs "
                f"({lease['ambiguous']}); refusing to {attempted}.")
    return (
        f"Branch {lease['branch']!r} was terminally merged by PR #{lease['pr']} "
        f"at gated head {lease['gated_sha']} (merge {lease['merged_sha']}). "
        f"Refusing to {attempted}: merged work cannot be continued. "
        "File a new issue and create a new branch for follow-up work."
    )


if os.environ.get("ARU_SDLC_REF"):
    check_version_compatibility()


if __name__ == "__main__":
    print("Aru_Agentic_SDLC Common Utilities Loaded Cleanly.")
