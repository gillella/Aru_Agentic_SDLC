#!/usr/bin/env python3
"""
fetch_next_issue.py - Selects the next actionable issue for one agent.

Multi-agent safe. Three things make it so:

  * Issues already held by another agent are excluded, so two agents are never
    handed the same work. (The previous version filtered only on --state open,
    so a claimed, in-progress issue was still returned as "next".)
  * Candidates whose `touches:` paths overlap pre-PR work already in flight are
    excluded. In Progress reserves the issue's declared `touches:`. In Review
    releases that implementation reservation because the open PR and merge
    gates now arbitrate conflicts. `parallel-eligible` only means "no unresolved
    depends-on"; it says nothing about two agents editing the same file.
  * --claim walks the candidate list and takes the first issue it can claim,
    so a lost race costs one retry rather than duplicated work.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from common import (
    agent_labels,
    claimed_by,
    get_current_branch,
    get_issue,
    get_issue_priority_field,
    get_repo_slug,
    is_trusted_metadata_author,
    list_open_issues,
    metadata_line_is_command_like,
    parse_touches,
    repository_owner_login,
    set_issue_priority_field,
    strip_code_blocks,
    repository_trusted_logins,
    run_cmd,
    run_gh_json,
    touches_conflict,
)
from delivery_increments import DeliveryIncrementStore
from update_issue_status import update_status

# Canonical Priority ranking: `priority:p0` is the highest. The governance
# label is the canonical source; the Project Board Priority field is a
# synchronized mirror (see common.get_issue_priority_field).
PRIORITY_RANK = {
    "priority:p0": 0,
    "priority:p1": 1,
    "priority:p2": 2,
    "priority:p3": 3,
}


def priority_rank(labels: List[Dict[str, Any]]) -> "tuple[Optional[int], Optional[str]]":
    """Returns (rank, None) when exactly one priority:pN label is present.

    Fails closed with (None, reason) on missing, duplicate, or contradictory
    priority metadata so the picker reports the issue for grooming instead of
    guessing.
    """
    found = [
        (label.get("name") or "").lower()
        for label in labels
        if (label.get("name") or "").lower() in PRIORITY_RANK
    ]
    if not found:
        return None, "missing priority:pN label"
    unique = sorted(set(found))
    if len(unique) > 1:
        return None, f"contradictory priority labels: {', '.join(unique)}"
    if len(found) > 1:
        return None, f"duplicate priority label: {found[0]}"
    return PRIORITY_RANK[unique[0]], None


def active_increment_scope(project_id: Optional[str] = None) -> Optional[set]:
    """Resolves the active operator-authorized increment's issue scope.

    Returns the set of in-scope issue numbers, or ``None`` when no active
    increment exists or its state cannot be resolved unambiguously (callers
    fail closed on ``IncrementError``). The store is the durable record
    authorized by the operator through the #205 flow.
    """
    try:
        store = DeliveryIncrementStore()
        if project_id is None:
            project_id = repo_project_id()
        increment = store.active(project_id) if project_id else None
    except Exception:
        return None
    if not increment:
        return None
    scope = increment.get("issue_scope") or []
    return {int(num) for num in scope}


def repo_project_id() -> Optional[str]:
    """Deterministic project id for this repository, or None when unknown."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    return f"proj_{owner}_{repo}".replace("-", "_")


def canonical_priority_display(labels: List[Dict[str, Any]]) -> Optional[str]:
    """The canonical board-field spelling ('P0'..'P3') of an issue's label."""
    rank, _ = priority_rank(labels)
    if rank is None:
        return None
    name = next(
        (label.get("name") for label in labels
         if (label.get("name") or "").lower() in PRIORITY_RANK),
        None,
    )
    if not name:
        return None
    return name[len("priority:"):].upper()


def sync_board_priority(issue: Dict[str, Any]) -> Optional[str]:
    """Enforces the Project Priority field mirror for one issue.

    The ``priority:pN`` label is the canonical source. The board Priority
    field must agree; when it disagrees the field is deterministically synced
    from the label. Returns the canonical display value ('P0'..'P3') on
    agreement or successful sync, else ``None`` (fail closed - never guess).
    """
    labels = issue.get("labels", [])
    canonical = canonical_priority_display(labels)
    if canonical is None:
        return None
    field_value = get_issue_priority_field(issue.get("number"))
    if field_value == canonical:
        return canonical
    if field_value is None:
        # Field unreadable is not a disagreement we can prove or fix; fail
        # closed rather than guess.
        return None
    if set_issue_priority_field(issue.get("number"), canonical):
        return canonical
    return None


ISSUE_IN_BRANCH = re.compile(r"issue-(\d+)", re.IGNORECASE)
CLOSES_ISSUE = re.compile(r"\bcloses\s+#(\d+)\b", re.IGNORECASE)
RENAME_CHANGE_TYPES = frozenset({"RENAMED"})
REST_STATUS_TO_CHANGE = {
    "renamed": "RENAMED",
    "added": "ADDED",
    "removed": "DELETED",
    "modified": "MODIFIED",
    "copied": "COPIED",
    "changed": "CHANGED",
}
OPEN_PR_FILES_QUERY = """
query($owner:String!, $repo:String!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        body
        headRefName
        changedFiles
        files(first: 100) {
          nodes { path changeType }
        }
      }
    }
  }
}
"""


class InvalidPrFiles(ValueError):
    """PR file snapshot cannot be used as an authoritative lock."""


def linked_issue_numbers_from_pr(pr: Dict[str, Any]) -> List[int]:
    """Issue numbers this PR closes, from body then branch name."""
    seen: set[int] = set()
    out: List[int] = []
    for match in CLOSES_ISSUE.finditer(pr.get("body") or ""):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            out.append(num)
    if out:
        return out
    match = ISSUE_IN_BRANCH.search(pr.get("headRefName") or "")
    if match:
        return [int(match.group(1))]
    return []


def _append_unique(paths: List[str], path: str) -> None:
    if path not in paths:
        paths.append(path)


def reserved_paths_from_pr(pr: Dict[str, Any]) -> List[str]:
    """Return this PR's lock paths, or raise if the snapshot is unusable."""
    if not isinstance(pr, dict) or not isinstance(pr.get("files"), list):
        raise InvalidPrFiles("invalid PR file response")
    files = pr["files"]
    changed = pr.get("changedFiles")
    if isinstance(changed, int) and changed > len(files):
        raise InvalidPrFiles("truncated PR file snapshot")
    if not files:
        raise InvalidPrFiles("empty PR file list")
    paths: List[str] = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path:
            raise InvalidPrFiles("invalid PR file entry")
        previous = entry.get("previousFileName") or entry.get("previous_filename")
        renamed = str(entry.get("changeType") or "").upper() in RENAME_CHANGE_TYPES
        if renamed or previous:
            if not isinstance(previous, str) or not previous:
                raise InvalidPrFiles("rename without previous path")
            _append_unique(paths, path)
            _append_unique(paths, previous)
            continue
        _append_unique(paths, path)
    return paths


def pr_files_by_issue_from_prs(prs: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    """Map each linked issue to the union of usable PR file paths.

    An issue is omitted when any linked PR snapshot is empty, truncated,
    renamed without a source path, or malformed, so reservation_paths falls
    back to declared touches. A non-dict record poisons the whole map.
    """
    if not isinstance(prs, list):
        return {}
    mapping: Dict[int, List[str]] = {}
    invalid: set[int] = set()
    for pr in prs:
        if not isinstance(pr, dict):
            return {}
        issue_nums = linked_issue_numbers_from_pr(pr)
        try:
            paths = reserved_paths_from_pr(pr)
        except InvalidPrFiles:
            invalid.update(issue_nums)
            continue
        for num in issue_nums:
            if num in invalid:
                continue
            current = mapping.setdefault(num, [])
            for path in paths:
                _append_unique(current, path)
    for num in invalid:
        mapping.pop(num, None)
    return mapping


def _normalize_pr_file_record(node: Dict[str, Any]) -> Dict[str, Any]:
    files_conn = node.get("files")
    if isinstance(files_conn, dict):
        nodes = files_conn.get("nodes")
        files = nodes if isinstance(nodes, list) else []
    elif isinstance(files_conn, list):
        files = files_conn
    else:
        files = []
    return {
        "number": node.get("number"),
        "body": node.get("body") or "",
        "headRefName": node.get("headRefName") or "",
        "changedFiles": node.get("changedFiles"),
        "files": files,
    }


def _pr_files_page(owner: str, repo: str, cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={OPEN_PR_FILES_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
    ]
    if cursor:
        cmd.extend(["-F", f"cursor={cursor}"])
    res = run_gh_json(cmd)
    if not isinstance(res, dict) or res.get("errors"):
        return None
    try:
        return res["data"]["repository"]["pullRequests"]
    except (KeyError, TypeError):
        return None


def _rest_pr_files(owner: str, repo: str, number: int) -> Optional[List[Dict[str, Any]]]:
    """REST file list with previous_filename; GraphQL has no rename source path."""
    if not isinstance(number, int):
        return None
    data = run_gh_json([
        "gh", "api",
        f"repos/{owner}/{repo}/pulls/{number}/files",
        "--paginate",
    ])
    if not isinstance(data, list):
        return None
    files: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            return None
        path = entry.get("filename")
        if not isinstance(path, str) or not path:
            return None
        record: Dict[str, Any] = {"path": path}
        status = str(entry.get("status") or "").lower()
        change = REST_STATUS_TO_CHANGE.get(status)
        if change:
            record["changeType"] = change
        previous = entry.get("previous_filename")
        if isinstance(previous, str) and previous:
            record["previous_filename"] = previous
        files.append(record)
    return files


def load_open_pr_file_records() -> Optional[List[Dict[str, Any]]]:
    """Open-PR file snapshots, or None when the list cannot be trusted."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    records: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    while True:
        page = _pr_files_page(owner, repo, cursor)
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            return None
        for node in page["nodes"]:
            if not isinstance(node, dict):
                return None
            records.append(_normalize_pr_file_record(node))
        info = page.get("pageInfo")
        if not isinstance(info, dict) or not info:
            return None
        has_next = info.get("hasNextPage")
        if not isinstance(has_next, bool):
            return None
        if not has_next:
            break
        cursor = info.get("endCursor")
        if not cursor or cursor in seen:
            return None
        seen.add(cursor)
    for record in records:
        files = _rest_pr_files(owner, repo, record["number"])
        record["files"] = [] if files is None else files
    return records


def attach_open_pr_file_snapshots(prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Overlay REST file paths and GraphQL changedFiles onto an existing PR list."""
    records = load_open_pr_file_records()
    if records is None:
        return prs
    by_number = {
        record["number"]: record
        for record in records
        if isinstance(record.get("number"), int)
    }
    for pr in prs:
        extra = by_number.get(pr.get("number"))
        if extra is None:
            continue
        pr["files"] = extra["files"]
        pr["changedFiles"] = extra["changedFiles"]
    return prs


def list_open_pr_files_by_issue() -> Dict[int, List[str]]:
    """Live map of issue → open-PR files. Empty on lookup failure."""
    records = load_open_pr_file_records()
    if records is None:
        return {}
    try:
        return pr_files_by_issue_from_prs(records)
    except (AttributeError, TypeError, ValueError):
        return {}


def reservation_paths(
    issue: Dict[str, Any],
    pr_files_by_issue: Optional[Dict[int, List[str]]] = None,
    repo_owner: Optional[str] = None,
    trusted_logins: Optional[set] = None,
) -> List[str]:
    """Paths this in-flight issue currently locks.

    In Progress uses declared touches. In Review releases the implementation
    lock because the branch has an open PR and merge-time conflict gates are
    authoritative. ``pr_files_by_issue`` remains accepted for caller
    compatibility but cannot extend the pre-PR reservation window.
    Untrusted authors contribute no reservation; they cannot lock the board.
    """
    owner = repo_owner if repo_owner is not None else repository_owner_login()
    logins = trusted_logins
    if logins is None and repo_owner is None:
        logins = repository_trusted_logins()
    if not is_trusted_metadata_author(issue, owner, trusted_logins=logins):
        return []
    names = {label.get("name", "").lower() for label in issue.get("labels", [])}
    if "status:in-review" in names:
        return []
    return parse_touches(issue.get("body") or "")


def parse_dependencies(body: str) -> List[int]:
    """Parses 'depends-on: #12, #14' pattern from issue body."""
    if not body:
        return []
    # A fenced or indented template example would otherwise supply its example
    # 'depends-on: none', masking this issue's real prerequisites (issue #294).
    match = re.search(
        r"^\s*depends-on\s*:\s*(.*?)\s*$",
        strip_code_blocks(body),
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return []
    raw = match.group(1)
    if metadata_line_is_command_like(raw):
        return []
    return [int(d) for d in re.findall(r"#(\d+)", raw)]


def is_epic(labels: List[Dict[str, Any]]) -> bool:
    """Epics are phase-level containers, never directly implementable.

    They carry no depends-on, so without this filter they are always
    'unblocked' and always the lowest-numbered candidate - meaning the picker
    would hand an agent an epic every single time.
    """
    return "type:epic" in [label.get("name", "").lower() for label in labels]


def needs_human(labels: List[Dict[str, Any]]) -> bool:
    """Operator-only issues are visible board work, never factory work."""
    return "needs-human" in [label.get("name", "").lower() for label in labels]


def is_parallel_eligible(body: str, labels: List[Dict[str, Any]]) -> bool:
    """Checks if issue is flagged as parallel-eligible."""
    names = [label.get("name", "").lower() for label in labels]
    if "parallel-eligible" in names or "independent" in names:
        return True
    return bool(body) and "parallel-eligible: true" in body.lower()


def reap_stale_claims(issues: List[Dict[str, Any]], hours: int) -> List[int]:  # noqa: C901, PLR0912, PLR0915
    """Releases claims that have gone quiet.

    An agent that crashes mid-issue leaves it In Progress forever, and once
    claimed issues are excluded from selection nothing would ever pick it up
    again. An issue is stale when it has been untouched for `hours`, has no
    open PR, and has no remote branch carrying commits.
    """
    if hours <= 0:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pr_code, prs, pr_err = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200",
         "--json", "number,body,headRefName"],
        check=False,
    )
    if pr_code != 0:
        print(f"[WARN] Could not verify open PRs; no claims were reaped: {pr_err}", file=sys.stderr)
        return []
    try:
        open_prs = json.loads(prs) if prs else []
    except json.JSONDecodeError:
        print("[WARN] Could not parse open PRs; no claims were reaped.", file=sys.stderr)
        return []

    branch_code, remote_branches, branch_err = run_cmd(
        ["git", "ls-remote", "--heads", "origin"],
        check=False,
    )
    if branch_code != 0:
        print(
            f"[WARN] Could not verify remote branches; no claims were reaped: {branch_err}",
            file=sys.stderr,
        )
        return []

    def has_open_pr(num: int) -> bool:
        pat = re.compile(rf"closes\s+#{num}\b", re.IGNORECASE)
        return any(pat.search(p.get("body") or "") or f"issue-{num}-" in p.get("headRefName", "")
                   for p in open_prs)

    def has_remote_branch(num: int) -> bool:
        return f"issue-{num}-" in remote_branches

    released = []
    for issue in issues:
        num = issue["number"]
        original_holders = agent_labels(issue)
        if not original_holders:
            continue
        updated = issue.get("updatedAt")
        if not updated:
            continue
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts > cutoff or has_open_pr(num) or has_remote_branch(num):
            continue

        current = get_issue(num)
        if not current:
            print(f"[WARN] Could not revalidate stale claim on #{num}; claim retained.",
                  file=sys.stderr)
            continue
        current_holders = agent_labels(current)
        if current_holders != original_holders:
            print(f"[INFO] Claim holders changed on #{num}; stale snapshot ignored.",
                  file=sys.stderr)
            continue
        current_updated = current.get("updatedAt")
        if current_updated:
            try:
                current_ts = datetime.fromisoformat(current_updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if current_ts > cutoff:
                continue

        target_status = "Backlog" if needs_human(current.get("labels", [])) else "Ready"
        if not update_status(num, target_status, require_board=True):
            print(
                f"[WARN] Could not synchronize #{num} to {target_status}; claim retained.",
                file=sys.stderr,
            )
            continue

        after_status = get_issue(num)
        if not after_status:
            update_status(num, "In Progress", require_board=True)
            print(f"[WARN] Could not verify stale-claim status on #{num}; status restored.",
                  file=sys.stderr)
            continue
        if needs_human(after_status.get("labels", [])) and target_status != "Backlog":
            if not update_status(num, "Backlog", require_board=True):
                update_status(num, "In Progress", require_board=True)
                print(f"[WARN] Could not return operator-only #{num} to Backlog; claim retained.",
                      file=sys.stderr)
                continue
            target_status = "Backlog"

        cmd = ["gh", "issue", "edit", str(num), "--remove-assignee", "@me"]
        for lbl in original_holders:
            cmd += ["--remove-label", lbl]
        if run_cmd(cmd, check=False)[0] == 0:
            released.append(num)
            print(f"♻️  Released stale claim on #{num} (idle > {hours}h, no PR, no branch).",
                  file=sys.stderr)
        else:
            update_status(num, "In Progress", require_board=True)
            print(
                f"[WARN] Could not remove the claim markers on #{num}; status restored.",
                file=sys.stderr,
            )
    return released


def build_candidates(  # noqa: C901, PLR0912, PLR0915
    issues: List[Dict[str, Any]],
    agent: Optional[str],
    pr_files_by_issue: Optional[Dict[int, List[str]]] = None,
    repo_owner: Optional[str] = None,
    trusted_logins: Optional[set] = None,
    increment_scope: Optional[set] = None,
) -> Dict[str, Any]:
    """Partitions open issues into in-flight, blocked, and claimable.

    ``increment_scope`` (set of issue numbers from the active authorized
    Delivery Increment) restricts claimable candidates to the active sprint:
    Ready, dependency-unblocked, path-safe issues outside the scope are
    returned as ``future_inventory`` and cannot be claimed. Issues with
    missing, duplicate, or contradictory ``priority:pN`` metadata fail closed
    and are returned as ``integrity_issues`` for grooming. Claimable
    candidates sort by priority (P0 > P1 > P2 > P3) then ascending issue
    number as the deterministic tie-break."""
    open_numbers = {i["number"] for i in issues}

    in_flight_paths: List[str] = []
    my_in_flight_issues: List[Dict[str, Any]] = []
    if trusted_logins is None and repo_owner is None:
        trusted_logins = repository_trusted_logins()
    if repo_owner is None:
        repo_owner = repository_owner_login()

    for issue in issues:
        labels = issue.get("labels", [])
        names = {label.get("name", "").lower() for label in labels}
        holder = claimed_by(issue)
        if holder or "status:in-progress" in names or "status:in-review" in names:
            in_flight_paths.extend(
                reservation_paths(
                    issue, pr_files_by_issue,
                    repo_owner=repo_owner, trusted_logins=trusted_logins,
                )
            )
        if needs_human(labels):
            continue
        if holder:
            if agent and holder == agent:
                my_in_flight_issues.append(issue)

    candidates, blocked, conflicted, not_ready, missing_touches = [], [], [], [], []
    operator_only = []
    future_inventory = []
    integrity_issues = []

    for issue in issues:
        num = issue["number"]
        body = issue.get("body") or ""
        labels = issue.get("labels", [])
        names = {label.get("name", "").lower() for label in labels}

        if needs_human(labels):
            operator_only.append(num)
            continue
        if is_epic(labels):
            continue
        if claimed_by(issue) or "status:in-progress" in names or "status:in-review" in names:
            continue  # held or in flight; not claimable as new implementation
        if "status:ready" not in names:
            not_ready.append(num)
            continue

        if not is_trusted_metadata_author(
            issue, repo_owner, trusted_logins=trusted_logins,
        ):
            missing_touches.append(num)
            continue

        unresolved = [d for d in parse_dependencies(body) if d in open_numbers]
        if unresolved:
            blocked.append({"number": num, "blocked_by": unresolved})
            continue

        my_paths = parse_touches(body)
        if not my_paths:
            missing_touches.append(num)
            continue
        clash = touches_conflict(my_paths, in_flight_paths) if my_paths else None
        if clash:
            conflicted.append({"number": num, "conflict": list(clash)})
            continue

        # Priority integrity gate: missing/duplicate/contradictory priority
        # fails closed for this issue and is reported for grooming.
        rank, reason = priority_rank(labels)
        if rank is None:
            integrity_issues.append({"number": num, "reason": reason})
            continue

        # Active-increment gate: only stories inside the operator-authorized
        # sprint may be claimed. Ready stories outside it stay visible as
        # future inventory (never claimable as new implementation).
        if increment_scope is not None and num not in increment_scope:
            future_inventory.append(issue)
            continue

        candidates.append(issue)

    # Priority sort: P0..P3 then lowest issue number as deterministic tie-break.
    def _sort_key(item: Dict[str, Any]) -> tuple:
        rank, _ = priority_rank(item.get("labels", []))
        return (rank if rank is not None else 99, item["number"])

    candidates.sort(key=_sort_key)
    return {
        "candidates": candidates,
        "future_inventory": future_inventory,
        "integrity_issues": integrity_issues,
        "blocked": blocked,
        "conflicted": conflicted,
        "not_ready": not_ready,
        "missing_touches": missing_touches,
        "operator_only": operator_only,
        "my_in_flight": min(my_in_flight_issues, key=lambda x: x["number"])
        if my_in_flight_issues else None,
    }


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Fetch the next actionable issue for one agent.")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")
    parser.add_argument("--agent", type=str, default=None,
                        help="Agent id. Returns this agent's in-flight issue first (resume).")
    parser.add_argument("--claim", action="store_true",
                        help="Claim the first candidate that can be claimed. Requires --agent.")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release claims idle longer than HOURS with no PR and no branch.")
    args = parser.parse_args()

    if args.claim and not args.agent:
        print("[ERROR] --claim requires --agent.", file=sys.stderr)
        sys.exit(1)

    issues = list_open_issues()

    if args.reap_after:
        if reap_stale_claims(issues, args.reap_after):
            issues = list_open_issues()

    pr_files = list_open_pr_files_by_issue()
    parts = build_candidates(
        issues, args.agent, pr_files_by_issue=pr_files,
        increment_scope=active_increment_scope(),
    )
    candidates = parts["candidates"]
    my_in_flight = parts["my_in_flight"]

    # Session resume: a worktree branch named .../issue-<N>-... wins over
    # anything else only when that issue is still open and still claimed by
    # this agent. A retained worktree after merge/close/release must not block
    # claiming available work.
    current_branch = get_current_branch()
    branch_issue = None
    m = re.search(r"issue-(\d+)", current_branch, re.IGNORECASE)
    if m:
        branch_num = int(m.group(1))
        match = next((i for i in issues if i["number"] == branch_num), None)
        holder = claimed_by(match) if match else None
        operator_only = bool(match and needs_human(match.get("labels", [])))
        if match and args.agent and holder == args.agent and not operator_only:
            branch_issue = branch_num
        else:
            if not match:
                reason = "not open"
            elif operator_only:
                reason = "operator-only (needs-human)"
            elif not args.agent:
                reason = "no --agent to validate ownership"
            elif holder and holder != args.agent:
                reason = f"held by '{holder}'"
            else:
                reason = "not claimed by this agent"
            print(
                f"[INFO] Branch references #{branch_num} but it is {reason}; "
                "ignoring stale resume.",
                file=sys.stderr,
            )

    resume = branch_issue or (my_in_flight["number"] if my_in_flight else None)

    claimed_now = None
    if args.claim:
        if resume:
            claimed_now = resume
            print(f"[INFO] Resuming your in-flight issue #{resume}; not claiming new work.")
        else:
            from claim_issue import EXIT_CONFLICT, EXIT_OK, claim_issue

            # After a lost race, another agent's newly claimed touches: may
            # invalidate later candidates from this snapshot. Rebuild before
            # each retry so overlapping work is not claimed from stale data.
            attempted = set()
            while True:
                parts = build_candidates(
                    issues, args.agent, pr_files_by_issue=pr_files
                )
                candidates = parts["candidates"]
                remaining = [c for c in candidates if c["number"] not in attempted]
                if not remaining:
                    print("[INFO] No claimable issue available.", file=sys.stderr)
                    break
                cand = remaining[0]
                attempted.add(cand["number"])
                rc = claim_issue(cand["number"], args.agent)
                if rc == EXIT_OK:
                    claimed_now = cand["number"]
                    break
                if rc == EXIT_CONFLICT:
                    print(
                        f"[INFO] #{cand['number']} unavailable; rebuilding candidates.",
                        file=sys.stderr,
                    )
                    issues = list_open_issues()
                    continue
                print(
                    f"[ERROR] Claiming #{cand['number']} failed; aborting candidate walk.",
                    file=sys.stderr,
                )
                break

    next_issue = candidates[0] if candidates else None
    res = {
        "agent": args.agent,
        "session_branch": current_branch,
        "resumable_in_flight_issue": resume,
        "claimed_now": claimed_now,
        "next_progressive_issue": next_issue["number"] if next_issue else None,
        "next_issue_title": next_issue["title"] if next_issue else None,
        "total_open_issues": len(issues),
        "claimable_now": [i["number"] for i in candidates],
        "parallel_eligible_issues": [
            i["number"] for i in candidates
            if is_parallel_eligible(i.get("body") or "", i.get("labels", []))
        ],
        "blocked_by_dependencies": parts["blocked"],
        "blocked_by_file_conflict": parts["conflicted"],
        "not_ready": parts["not_ready"],
        "missing_touches": parts["missing_touches"],
        "operator_only_issues": parts["operator_only"],
        "held_by_other_agents": [
            {"number": i["number"], "agent": claimed_by(i)}
            for i in issues if claimed_by(i) and claimed_by(i) != args.agent
        ],
    }

    if args.json:
        print(json.dumps(res, indent=2))
        return

    print("=== Aru_Agentic_SDLC: Issue Dependency Evaluation ===")
    if args.agent:
        print(f"👤 Agent: {args.agent}")
    if resume:
        print(f"🔄 In-flight issue to resume: #{resume}")
    if claimed_now and claimed_now != resume:
        print(f"🔒 Claimed: #{claimed_now}")
    if next_issue:
        print(f"🎯 Next Actionable Issue: #{next_issue['number']} - {next_issue['title']}")
    else:
        print("✨ No unblocked, unclaimed issues available.")
    if res["parallel_eligible_issues"]:
        print(f"⚡ Claimable in parallel right now: {res['parallel_eligible_issues']}")
    if res["held_by_other_agents"]:
        held = ", ".join(f"#{h['number']}({h['agent']})" for h in res["held_by_other_agents"])
        print(f"🚧 Held by other agents: {held}")
    if res["blocked_by_file_conflict"]:
        cf = ", ".join(f"#{c['number']}" for c in res["blocked_by_file_conflict"])
        print(f"📁 Deferred - file conflict with in-flight work: {cf}")
    if res["missing_touches"]:
        print(f"📝 Deferred - missing touches declaration: {res['missing_touches']}")


if __name__ == "__main__":
    main()
