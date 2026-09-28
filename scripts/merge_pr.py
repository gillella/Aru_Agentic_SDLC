#!/usr/bin/env python3
"""The governed fail-closed merge and close-out helper."""

from __future__ import annotations

import argparse
import re
import time
from typing import Any

import merge_authority
from check_ci import ci_verdict, finalization_verdict
from common import KernelError, canonical_github_actor, gh_paginated, json_print, repo_slug, run, same_github_actor
from fetch_pr_feedback import fetch_feedback
from merge_state import (
    base_snapshot, close_out, issue_gate, linked_issues, merge_queue_snapshot,
    pull_changed_paths, pull_request,
)


class GateRefusal(KernelError):
    """A refusal that names the declared gate it enforces.

    Subclasses KernelError, so every existing handler still catches it; the gate
    id makes the refusal attributable to scripts/policy.toml rather than to a
    bare string. tests/test_policy.py proves the mapping is total in both
    directions for this helper.
    """

    def __init__(self, gate: str, message: str) -> None:
        super().__init__(message)
        self.gate = gate


def refuse(gate: str, message: str, *, cause: BaseException | None = None) -> None:
    error = GateRefusal(gate, message)
    if cause is not None:
        raise error from cause
    raise error


MERGEABLE_STATES = {"CLEAN", "UNSTABLE"}
DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
REVIEW_STATES = DECISIVE_REVIEW_STATES | {"COMMENTED", "PENDING"}


def pull_reviews(number: int) -> list[dict[str, Any]]:
    return gh_paginated(f"repos/{repo_slug()}/pulls/{number}/reviews?per_page=100")


def latest_decisive_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate the complete review list and keep each account's last decision."""
    if not isinstance(reviews, list):
        refuse("approval-by-another-account", "review evidence is malformed")
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        user = review.get("user") if isinstance(review, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        state = review.get("state") if isinstance(review, dict) else None
        if (not isinstance(login, str) or not login.strip()
                or state not in REVIEW_STATES):
            refuse("approval-by-another-account", "review evidence is malformed")
        if state in DECISIVE_REVIEW_STATES:
            commit = review.get("commit_id")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                refuse("approval-by-another-account", "review evidence is malformed")
            # Group by account: logins are case-insensitive and an App has two spellings.
            latest[canonical_github_actor(login)] = review
    return latest


def approved_at_head(pr: dict[str, Any], reviews: list[dict[str, Any]]) -> bool:
    """Whether another account approved this head with no active change request."""
    head = pr.get("headRefOid")
    author = pr["author"].get("login") if isinstance(pr.get("author"), dict) else None
    if not isinstance(author, str) or not author.strip():
        refuse("approval-by-another-account", "PR author is unreadable")
    latest = latest_decisive_reviews(reviews)
    if any(review["state"] == "CHANGES_REQUESTED" for review in latest.values()):
        return False
    return any(
        review["state"] == "APPROVED" and review.get("commit_id") == head
        and not same_github_actor(account, author)
        for account, review in latest.items()
    )


def require_mergeable(pr: dict[str, Any], queue: dict[str, object]) -> None:
    if queue["configured"] or queue["entry"] is not None or queue["auto_merge"] is not None:
        refuse("base-head-race", "merge queues and pending auto-merge are unsupported; no merge submitted")
    merge_state = pr.get("mergeStateStatus")
    if pr.get("mergeable") != "MERGEABLE":
        refuse("base-head-race", "PR is not currently mergeable")
    # GitHub also reports BLOCKED while its required approval is missing or changes are
    # requested, so require_unblocked() judges BLOCKED only after the review gates.
    if merge_state not in MERGEABLE_STATES | {"BLOCKED"}:
        refuse("base-head-race", f"PR merge state is {merge_state}")


def require_unblocked(pr: dict[str, Any]) -> None:
    # With the merge-authority gate on, its required check is absent until merge()
    # posts it, so GitHub reports BLOCKED for every PR. GitHub still refuses the
    # submission if anything else blocks, and await_unblocked() names that case.
    if pr.get("mergeStateStatus") == "BLOCKED" and not merge_authority.configured():
        refuse("helper-only-merge-optional", "PR merge state is BLOCKED")


def await_unblocked(number: int, head: str, attempts: int = 6) -> None:
    """Give GitHub a bounded moment to recompute mergeability after authorization."""
    for attempt in range(attempts):
        pr = pull_request(number)
        if pr.get("headRefOid") != head:
            refuse("helper-only-merge-optional", "PR head changed after merge authorization")
        if pr.get("mergeStateStatus") in MERGEABLE_STATES:
            return
        if attempt + 1 < attempts:
            time.sleep(2)
    refuse("helper-only-merge-optional", "PR is still blocked after merge authorization; another ruleset requirement is unmet")


def _revoke_authorization(head: str, cause: KernelError) -> None:
    # A newer failed run supersedes the success, so a bare `gh pr merge` cannot
    # finish a submission this helper abandoned.
    try:
        merge_authority.post(head, "failure", "Merge submission failed; re-run merge_pr.py")
    except KernelError as revoke_error:
        refuse("helper-only-merge-optional", f"{revoke_error}; original merge failure: {cause}", cause=cause)


def evaluate(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        refuse("five-statuses", "PR is not an open, ready pull request")
    head = pr.get("headRefOid")
    if (
        head != expected_head
        or not isinstance(head, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", head)
    ):
        refuse("base-head-race", "expected head does not match the current PR head")
    base_sha = base_snapshot(pr)
    queue = merge_queue_snapshot(number, expected_head, base_sha)
    require_mergeable(pr, queue)

    changed_paths = pull_changed_paths(number)
    issues = linked_issues(str(pr.get("body") or ""))
    issue_evidence = issue_gate(issues, changed_paths)
    ci = require_ci_review(pr, number, head)
    require_unblocked(pr)
    return {
        "pr": number, "head": head, "branch": pr["headRefName"],
        "base": pr["baseRefName"], "base_sha": base_sha,
        "issues": issue_evidence, "changed_paths": changed_paths,
        "ci": ci["checks"], "approved": True, "feedback": 0,
        "merge_queue": queue["configured"],
        "queue_entry": queue["entry"], "auto_merge": queue["auto_merge"],
    }


def require_ci_review(pr: dict, number: int, head: str, *, context: str = "", finalizing: bool = False) -> dict:
    """Admission and close-out consume the same CI, thread and approval gates."""
    ci = finalization_verdict(pr) if finalizing else ci_verdict(number)
    if ci["head"] != head or ci["state"] != "success":
        refuse("exact-head-consumer-verification", "exact-current-head required GitHub checks are not successful")
    feedback = fetch_feedback(number)
    if feedback:
        refuse("unresolved-findings", f"{len(feedback)} unresolved {context}review thread(s) or blocking summary finding(s)")
    if pr.get("reviewDecision") not in {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
        refuse("approval-by-another-account", "PR review decision is malformed")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        refuse("approval-by-another-account", f"a submitted {context}review still requests changes")
    reviews = pull_reviews(number)
    if any(review["state"] == "CHANGES_REQUESTED"
           for review in latest_decisive_reviews(reviews).values()):
        refuse("approval-by-another-account", f"a submitted {context}review still requests changes")
    if not approved_at_head(pr, reviews):
        refuse("approval-by-another-account", f"no {context}approval of the exact head by an account other than the author")
    return ci


def revalidate_review(pr: dict[str, Any], number: int) -> None:
    """Recheck the approval and threads after the final PR/issue/queue reads."""
    reviews = pull_reviews(number)
    if not approved_at_head(pr, reviews):
        refuse("approval-by-another-account", "approval of the exact head was withdrawn before merge submission")
    feedback = fetch_feedback(number)
    if feedback:
        refuse("unresolved-findings", f"{len(feedback)} unresolved review thread(s) or blocking summary finding(s) before merge submission")


def merge(number: int, expected_head: str, *, dry_run: bool = False) -> dict[str, object]:
    gates = evaluate(number, expected_head)
    if dry_run:
        return {"merged": False, "gates": gates}
    issue_numbers = [int(item["issue"]) for item in gates["issues"]]
    live_gates = evaluate(number, expected_head)
    if live_gates != gates:
        refuse("current-board-and-dependencies", "merge authority changed during final gate evaluation")
    command = ["gh", "pr", "merge", str(number), "--merge", "--match-head-commit", expected_head]
    # One bounded semantic reread after CI/review reads, immediately before
    # submission. Separate GitHub metadata reads and merge remain non-atomic.
    final_pr = pull_request(number)
    if (
        final_pr.get("state") != "OPEN" or final_pr.get("isDraft") is not False
        or final_pr.get("headRefOid") != expected_head
        or final_pr.get("baseRefName") != gates["base"]
        or base_snapshot(final_pr) != gates["base_sha"]
        or final_pr.get("reviewDecision") == "CHANGES_REQUESTED"
        or linked_issues(str(final_pr.get("body") or "")) != issue_numbers
    ):
        refuse("base-head-race", "PR authorization changed before merge submission")
    if issue_gate(issue_numbers, gates["changed_paths"]) != gates["issues"]:
        refuse("current-board-and-dependencies", "issue authorization changed before merge submission")
    final_queue = merge_queue_snapshot(number, expected_head, str(gates["base_sha"]))
    if (final_queue["configured"], final_queue["entry"], final_queue["auto_merge"]) != (
        gates["merge_queue"], gates["queue_entry"], gates["auto_merge"],
    ):
        refuse("base-head-race", "merge queue or pending request changed before merge submission")
    revalidate_review(final_pr, number)
    authorized = merge_authority.post(expected_head, "success", f"merge_pr.py gates passed for PR #{number}")
    try:
        if authorized is not None:
            await_unblocked(number, expected_head)
        run(command)
    except KernelError as exc:
        if authorized is not None:
            _revoke_authorization(expected_head, exc)
        raise
    merged = pull_request(number)
    if merged.get("headRefOid") != expected_head:
        refuse("base-head-race", "PR head changed during merge submission")
    if not merged.get("mergedAt"):
        refuse("issue-done-and-cleanup", "GitHub did not confirm the expected-head merge; no issue closed")
    return finalize_queued(number, expected_head)


def finalize_queued(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        refuse("issue-done-and-cleanup", "PR has not merged yet")
    if pr.get("headRefOid") != expected_head:
        refuse("issue-done-and-cleanup", "expected head does not match the merged PR head")
    changed_paths = pull_changed_paths(number)
    numbers = linked_issues(str(pr.get("body") or ""))
    issue_gate(numbers, changed_paths, allow_closed=True, allow_done=True)
    require_ci_review(pr, number, expected_head, context="post-merge ", finalizing=True)
    evidence = close_out(numbers, changed_paths)
    return {
        "merged": True, "finalized": True, "pr": number, "head": expected_head,
        "merge_commit": pr["mergeCommit"]["oid"],
        "issues": [int(item["issue"]) for item in evidence],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.finalize and args.dry_run:
            raise KernelError("--finalize and --dry-run cannot be combined")
        result = (
            finalize_queued(args.pr, args.expected_head)
            if args.finalize
            else merge(args.pr, args.expected_head, dry_run=args.dry_run)
        )
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        message = f"finalized PR #{args.pr}" if args.finalize else "merge gates passed" if args.dry_run else (
            f"merged PR #{args.pr}" if result["merged"] else f"submitted PR #{args.pr} for GitHub merge"
        )
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
