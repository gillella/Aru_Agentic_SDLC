#!/usr/bin/env python3
"""fleet_status.py - read-only recovery status for one governed repository.

Reports authoritative GitHub issue, pull-request, and claim facts joined
against trusted local Git worktree state. Exit codes: complete 0, error 1,
waiting 2, blocked 3.
"""
# Compatibility-named: the fleet supervisor this file was built for is gone,
# and what remains is the single question a returning operator asks -- what is
# open, who holds it, and which worktrees are still on disk. This command never
# writes, schedules, or decides eligibility; GitHub and Git stay authoritative.
#
# One run costs a fixed number of GitHub commands: the open-issue inventory,
# the board snapshot, and the open-PR list are each read exactly once and
# joined locally, so cost does not grow with the size of the backlog. The
# per-issue board query this file used to make was an N+1 scan that exhausted
# the shared GraphQL quota on a large board.

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

from common import (
    claimed_by,
    get_repo_slug,
    label_names,
    query_open_issues,
    run_cmd,
    worktree_agent_of,
)
from github_inventory import open_pull_requests as rest_open_pull_requests
from picker_board_inventory import governed_board_inventory

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_WAITING = 2
EXIT_BLOCKED = 3

_ISSUE_BRANCH_RE = re.compile(r"issue-(\d+)", re.IGNORECASE)


def _identity(labels: List[str], prefix: str) -> Optional[str]:
    """Return the lowest-sorting ``<prefix><id>`` label value, or None."""
    values = sorted(n[len(prefix):] for n in labels if n.startswith(prefix))
    return values[0] if values else None


def _fail(reason: str, summary: str, state: str = "error",
          code: int = EXIT_ERROR) -> Dict[str, Any]:
    """Build the fail-closed result. Incomplete state is never reported as ok."""
    return {
        "state": state, "exit_code": code, "summary": summary,
        "reasons": [reason], "open_issues_count": None, "open_prs_count": None,
        "issues": [], "pull_requests": [], "claims": [], "worktrees": [],
    }


# gh is trusted to transport the answer, not to shape it. The open-issue
# inventory normalizes its own rows exactly this way, and an unchecked pull
# request row turns a malformed page into a traceback instead of the error
# state this command promises.
def _readable_pr(row: Any) -> bool:
    """True when a row can be read without raising; anything else fails closed."""
    if not isinstance(row, dict) or not isinstance(row.get("number"), int):
        return False
    labels = row.get("labels", [])
    return isinstance(labels, list) and all(
        isinstance(label, dict) and isinstance(label.get("name"), str)
        for label in labels
    )


# The paginated REST reader keeps this inventory off the shared GraphQL
# budget, and it returns the complete list or nothing at all, so there is no
# truncated page here to mistake for a short one.
def list_open_prs(slug: str) -> Optional[List[Dict[str, Any]]]:
    """Read every open PR once, or None when the inventory is unreadable."""
    result = rest_open_pull_requests(run_cmd, slug)
    if not isinstance(result, list):
        return None
    return result if all(_readable_pr(row) for row in result) else None


def list_worktrees() -> Optional[List[Dict[str, str]]]:
    """Return ``{path, branch}`` per worktree, or None when Git cannot answer."""
    # A live checkout always lists at least its own main worktree, so an empty
    # or failed read is an unreadable one, not an empty one. Reporting it as
    # "no worktrees" would hide the leftover state this command exists to find.
    code, out, _err = run_cmd(["git", "worktree", "list", "--porcelain"], check=False)
    if code != 0 or not out.strip():
        return None
    rows: List[Dict[str, str]] = []
    for block in out.split("\n\n"):
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            key, _sep, value = line.partition(" ")
            if value:
                fields[key] = value.strip()
        if fields.get("worktree"):
            rows.append({
                "path": fields["worktree"],
                "branch": fields.get("branch", "").replace("refs/heads/", ""),
            })
    return rows


def _issue_rows(issues: List[Dict[str, Any]],
                board: Dict[int, str]) -> List[Dict[str, Any]]:
    """Join each open issue with its board status from the single snapshot."""
    rows = []
    for issue in issues:
        labels = label_names(issue)
        label_status = _identity(labels, "status:")
        board_status = board.get(issue["number"], "")
        # Board and label are written together; a mismatch is the exact signal
        # that a previous session died between the two writes.
        drifted = (bool(label_status) and bool(board_status)
                   and label_status != board_status.lower().replace(" ", "-"))
        rows.append({
            "number": issue["number"], "title": issue.get("title", ""),
            "label_status": label_status, "board_status": board_status,
            "agent": claimed_by(issue), "drifted": drifted,
        })
    return sorted(rows, key=lambda row: row["number"])


def _pr_rows(prs: List[Dict[str, Any]], slug: str) -> List[Dict[str, Any]]:
    """Report PR identity and authorship. Review verdicts belong to merge_pr."""
    rows = [{
        "number": pr["number"], "title": pr.get("title", ""),
        "branch": pr.get("headRefName", ""), "head": pr.get("headRefOid", ""),
        "draft": bool(pr.get("isDraft")),
        "author": _identity(label_names(pr), "author:"),
        "updated_at": pr.get("updatedAt") or "",
        # The list endpoint the reader normalizes does not carry a browse URL,
        # and the operator view would lose a link it has always printed.
        "url": f"https://github.com/{slug}/pull/{pr['number']}",
    } for pr in prs]
    return sorted(rows, key=lambda row: row["number"])


def _worktree_rows(worktrees: List[Dict[str, str]],
                   open_numbers: set) -> List[Dict[str, Any]]:
    """Flag worktrees whose issue is no longer open -- recoverable leftovers."""
    rows = []
    for entry in worktrees:
        match = _ISSUE_BRANCH_RE.search(entry["branch"])
        number = int(match.group(1)) if match else None
        rows.append({
            "path": entry["path"], "branch": entry["branch"], "issue": number,
            "agent": worktree_agent_of(entry["path"]) or None,
            "stale": number is not None and number not in open_numbers,
        })
    return rows


def _reasons(issues: List[Dict[str, Any]], prs: List[Dict[str, Any]],
             worktrees: List[Dict[str, Any]]) -> List[str]:
    """One recovery fact per line: issues first, then PRs, then worktrees."""
    lines = []
    for row in issues:
        held = f" held by {row['agent']}" if row["agent"] else ""
        lines.append(f"Issue #{row['number']} is {row['board_status'] or 'unknown'}{held}.")
        if row["drifted"]:
            lines.append(
                f"Issue #{row['number']} label 'status:{row['label_status']}' "
                f"drifts from board status '{row['board_status']}'."
            )
    for row in prs:
        author = f" by {row['author']}" if row["author"] else ""
        # The word "review" is load-bearing: slack_control_room.status_text()
        # selects open review work by filtering these lines for it, so a PR
        # line without it reports "none" while the PR is still open. Neither
        # phrase asserts a verdict -- an open PR awaits one or the other.
        state = "draft, not yet in review" if row["draft"] else "awaiting review or merge"
        lines.append(
            f"PR #{row['number']} is open{author} on {row['branch']} ({state}).")
    lines.extend(
        f"Worktree '{row['path']}' remains for closed issue #{row['issue']}."
        for row in worktrees if row["stale"]
    )
    return lines


def _collect(slug: str) -> Dict[str, Any]:
    """Read the repository the caller has already selected as the cwd."""
    issues = query_open_issues()
    if issues is None:
        return _fail("Failed to list open issues from GitHub.",
                     "ERROR: Could not read open issues.")
    open_numbers = {issue["number"] for issue in issues}
    inventory = governed_board_inventory(slug, open_numbers)
    if inventory is None:
        return _fail(
            f"Could not read one complete governed project board for '{slug}'; it "
            "is missing, ambiguous, truncated, or omits an open issue.",
            "BLOCKED: Governed project board state is unusable.",
            state="blocked", code=EXIT_BLOCKED,
        )
    board, ready_count = inventory
    prs = list_open_prs(slug)
    if prs is None:
        return _fail("Failed to list open pull requests from GitHub.",
                     "ERROR: Could not read open pull requests.")

    worktrees = list_worktrees()
    if worktrees is None:
        return _fail("Failed to read local Git worktree state.",
                     "ERROR: Could not read local worktrees.")

    issue_rows, pr_rows = _issue_rows(issues, board), _pr_rows(prs, slug)
    worktree_rows = _worktree_rows(worktrees, open_numbers)
    claims = (
        [{"type": "issue", "number": r["number"], "agent": r["agent"]}
         for r in issue_rows if r["agent"]]
        + [{"type": "pr", "number": r["number"], "agent": r["author"]}
           for r in pr_rows if r["author"]]
    )
    open_work = bool(issue_rows or pr_rows
                     or any(r["issue"] is not None for r in worktree_rows))
    return {
        "state": "waiting" if open_work else "complete",
        "exit_code": EXIT_WAITING if open_work else EXIT_COMPLETE,
        "summary": (
            f"WAITING: {len(issue_rows)} open issue(s), {len(pr_rows)} open PR(s), "
            f"{len(claims)} claim(s)." if open_work else
            "COMPLETE: no open issues, no open PRs, no claims, no issue worktrees."
        ),
        "reasons": _reasons(issue_rows, pr_rows, worktree_rows),
        "repo": slug,
        "open_issues_count": len(issue_rows),
        "open_prs_count": len(pr_rows),
        "ready_count": ready_count,
        "issues": issue_rows, "pull_requests": pr_rows,
        "claims": claims, "worktrees": worktree_rows,
    }


def evaluate_fleet_status(repo_dir: str = ".") -> Dict[str, Any]:
    """Report authoritative recovery status for ``repo_dir``. Never writes."""
    try:
        target = os.path.abspath(repo_dir)
    except (OSError, TypeError, ValueError) as exc:
        return _fail(f"Could not resolve repository directory '{repo_dir}': {exc}",
                     "ERROR: Invalid repository directory.")
    if not os.path.isdir(target):
        return _fail(f"Repository directory does not exist: {target}",
                     "ERROR: Invalid repository directory.")
    previous = os.getcwd()
    try:
        os.chdir(target)
        slug = get_repo_slug()
        if not slug:
            return _fail("Could not resolve the GitHub repository from origin.",
                         "ERROR: Unable to resolve repository slug.")
        return _collect(slug)
    except OSError as exc:
        return _fail(f"Could not read repository directory '{target}': {exc}",
                     "ERROR: Could not access repository directory.")
    finally:
        os.chdir(previous)


def format_status(status: Dict[str, Any]) -> str:
    """Render the operator's plain-text view of one status result."""
    lines = ["=== Aru_Agentic_SDLC: Factory Status ===",
             f"State: {status['state'].upper()}",
             f"Summary: {status['summary']}"]
    worktrees = status.get("worktrees") or []
    if worktrees:
        lines.append(f"\nWorktrees ({len(worktrees)}):")
        lines.extend(
            f"  • [{'stale' if row['stale'] else 'active'}] {row['path']} "
            f"({row['branch'] or 'detached'})" for row in worktrees
        )
    if status.get("reasons"):
        lines.append("\nDetails:")
        lines.extend(f"  • {reason}" for reason in status["reasons"])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report read-only issue, PR, claim, and worktree status.")
    parser.add_argument("--json", action="store_true", help="Emit the status as JSON")
    parser.add_argument("--repo-dir", default=".", help="Repository working directory")
    args = parser.parse_args()
    status = evaluate_fleet_status(args.repo_dir)
    print(json.dumps(status, indent=2) if args.json else format_status(status))
    sys.exit(status["exit_code"])


if __name__ == "__main__":
    main()
