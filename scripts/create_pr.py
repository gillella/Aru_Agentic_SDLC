#!/usr/bin/env python3
"""Open the one governed PR for a claimed issue.

Review is GitHub-native: any account other than the author approves the exact
head, and merge_pr.py reads that. This helper only opens the PR and moves the
issue to In Review, and asks GitHub to request the reviewers the repository
declares in `.aru/review.json`, so GitHub notifies the account that owes the
approval. It applies no review labels. Requesting is notification, not authority:
the merge still turns on a submitted approval of this pull request's exact head.
Only the reviewer declaration is read from the default branch, so a change cannot
nominate its own reviewers.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import board_template
import review_authority
from common import (
    AGENT_PREFIX,
    KernelError,
    default_branch_name,
    gh_json,
    git,
    issue,
    json_print,
    label_names,
    repo_slug,
    run,
    set_status,
    status_of,
)


def current_branch() -> str:
    branch = git(["branch", "--show-current"])
    if not branch or branch in {"main", "master"}:
        raise KernelError("pull requests must be opened from a feature branch")
    return branch


def current_agent(record: dict[str, Any]) -> str:
    values = [name[len(AGENT_PREFIX):] for name in label_names(record) if name.startswith(AGENT_PREFIX)]
    if len(values) != 1:
        raise KernelError("issue must have exactly one claimant")
    return values[0]


def require_published_head(branch: str) -> str:
    head = git(["rev-parse", "HEAD"])
    result = run(
        ["git", "-c", "core.fsmonitor=false", "ls-remote", "--heads", "origin", branch],
        check=False,
    )
    fields = result.stdout.strip().split()
    if result.returncode or len(fields) != 2 or fields[0] != head:
        raise KernelError("push the exact current head before creating the PR")
    return head


def local_changed_paths() -> list[str]:
    """Return both sides of every changed path on the published issue branch."""
    base = f"origin/{default_branch_name()}"
    output = git(["diff", "--name-status", "--find-renames", f"{base}...HEAD"])
    paths: list[str] = []
    for line in output.splitlines():
        fields = line.split("\t")
        status = fields[0][:1] if fields and fields[0] else ""
        expected = 3 if status in {"R", "C"} else 2
        if status not in {"A", "C", "D", "M", "R", "T"} or len(fields) != expected:
            raise KernelError("local changed-path evidence is malformed")
        paths.extend(fields[1:])
    if not paths:
        raise KernelError("published branch has no changed files")
    return sorted(set(paths))


def require_current_owner(number: int, owner: str, status: str = "In Progress") -> None:
    live_issue = issue(number)
    if status_of(live_issue) != status or current_agent(live_issue) != owner:
        raise KernelError("issue ownership changed before PR creation")



def request_reviewers(pr_number: int) -> list[str]:
    """Request the declared reviewers so GitHub notifies them; returns those requested.

    The declaration is read from the default branch by ``review_authority``, never
    from this pull request's head, so a change cannot nominate its own reviewers.
    Never a gate: an account GitHub refuses -- the pull request's own author, or one
    that cannot review this repository -- is reported and skipped, and a repository
    that declares nobody requests nobody.
    """
    requested = []
    for login in review_authority.load_policy().reviewers:
        try:
            run(["gh", "pr", "edit", str(pr_number), "--add-reviewer", login])
        except KernelError as exc:
            sys.stderr.write(f"note: could not request review from {login}: {exc}\n")
            continue
        requested.append(login)
    return requested



def announce(pr: dict[str, Any]) -> None:
    """Board card and review request: notification, never authority.

    Both are best-effort by contract. A failure here is reported and never refuses
    or rolls back a pull request that is otherwise sound, so a board outage cannot
    stop governed work.
    """
    pr_node_id = pr.get("id")
    if isinstance(pr_node_id, str) and pr_node_id:
        try:
            board_template.add_pr_to_board(repo_slug(), pr_node_id, Path.cwd())
        except Exception as exc:
            sys.stderr.write(f"note: could not add PR to Project Board: {exc}\n")
    try:
        request_reviewers(int(pr["number"]))
    except Exception as exc:
        sys.stderr.write(f"note: could not request reviewers: {exc}\n")


def create(number: int, title: str, body: str, agent: str | None = None) -> dict[str, object]:
    record = issue(number)
    if status_of(record) != "In Progress":
        raise KernelError("issue must be In Progress")
    owner = current_agent(record)
    if agent and agent != owner:
        raise KernelError("provided agent does not own the issue")
    if re.search(r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+", body):
        raise KernelError("body must not contain a caller-supplied closing directive")
    branch = current_branch()
    if f"issue-{number}-" not in branch:
        raise KernelError("current branch does not belong to the issue")
    head = require_published_head(branch)
    local_changed_paths()  # refuses an empty or malformed published diff before opening anything
    arguments = ["gh", "pr", "create", "--title", title, "--body", body.rstrip() + f"\n\nCloses #{number}\n"]
    require_current_owner(number, owner)
    run(arguments)
    rollback_target: str | int = branch
    try:
        pr = gh_json(["pr", "view", branch, "--json", "number,url,state,headRefOid,id"])
        if (
            not isinstance(pr, dict)
            or not isinstance(pr.get("number"), int)
            or not isinstance(pr.get("url"), str)
            or pr.get("state") != "OPEN"
        ):
            raise KernelError("created PR snapshot is malformed")
        rollback_target = int(pr["number"])
        if pr.get("headRefOid") != head:
            raise KernelError("created PR is not bound to the published head")
        require_current_owner(number, owner)
        set_status(
            number,
            "In Review",
            expected_current="In Progress",
            pre_mutation_check=lambda: require_current_owner(number, owner),
        )
        require_current_owner(number, owner, "In Review")
        announce(pr)
    except KernelError as post_create_error:
        try:
            run(["gh", "pr", "close", str(rollback_target), "--comment",
                 "Aru closed this PR because post-creation validation did not settle."])
            closed = gh_json(["pr", "view", str(rollback_target), "--json", "number,state"])
            if not isinstance(closed, dict) or closed.get("state") != "CLOSED":
                raise KernelError("created PR rollback did not settle closed")
        except KernelError as rollback_error:
            raise KernelError(f"{rollback_error}; original post-create failure: {post_create_error}") from rollback_error
        raise
    return {
        "pr": int(pr["number"]), "url": pr["url"], "head": head,
        "next_action": "await-approval-by-another-account",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--title", required=True)
    body_source = parser.add_mutually_exclusive_group(required=True)
    body_source.add_argument("--body")
    body_source.add_argument("--body-file")
    parser.add_argument("--agent")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        body = args.body if args.body is not None else Path(args.body_file).read_text(encoding="utf-8")
        result = create(args.issue, args.title, body, args.agent)
    except (KernelError, OSError) as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
