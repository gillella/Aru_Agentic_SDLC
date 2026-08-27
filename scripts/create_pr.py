#!/usr/bin/env python3
"""Open a PR and assign one stable external reviewer."""

from __future__ import annotations

import argparse
import re

from common import (
    AGENT_PREFIX,
    REVIEW_PREFIX,
    REVIEW_SERVICES,
    KernelError,
    ensure_label,
    gh_json,
    git,
    issue,
    label_names,
    run,
    set_status,
    status_of,
)


def reviewer_for_issue(number: int) -> str:
    return REVIEW_SERVICES[number % len(REVIEW_SERVICES)]


def current_branch() -> str:
    branch = git(["branch", "--show-current"])
    if not branch or branch in {"main", "master"}:
        raise KernelError("pull requests must be opened from a feature branch")
    return branch


def current_agent(record: dict) -> str:
    values = [name[len(AGENT_PREFIX) :] for name in label_names(record) if name.startswith(AGENT_PREFIX)]
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
    reviewer = reviewer_for_issue(number)
    review_label = REVIEW_PREFIX + reviewer
    author_label = "author:" + owner
    ensure_label(review_label, color="0e8a16", description=f"External review: {reviewer}")
    ensure_label(author_label, color="1d76db", description=f"PR authored by {owner}")
    final_body = body.rstrip() + f"\n\nCloses #{number}\n"
    run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            final_body,
            "--label",
            review_label,
            "--label",
            author_label,
        ]
    )
    pr = gh_json(
        [
            "pr",
            "view",
            branch,
            "--json",
            "number,url,headRefOid,labels",
        ]
    )
    if pr.get("headRefOid") != head:
        raise KernelError("created PR is not bound to the published head")
    reviews = [name for name in label_names(pr) if name.startswith(REVIEW_PREFIX)]
    if reviews != [review_label]:
        raise KernelError("created PR does not have exactly one assigned reviewer")
    set_status(number, "In Review")
    return {
        "pr": int(pr["number"]),
        "url": pr["url"],
        "head": head,
        "reviewer": reviewer,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--agent")
    args = parser.parse_args()
    try:
        result = create(args.issue, args.title, args.body, args.agent)
    except KernelError as exc:
        parser.error(str(exc))
    print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
