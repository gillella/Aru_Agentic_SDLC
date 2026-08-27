#!/usr/bin/env python3
"""The sole fail-closed merge and close-out authority."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from typing import Any

from check_ci import ci_verdict, check_name, check_state
from common import (
    REVIEW_PREFIX,
    REVIEW_SERVICES,
    KernelError,
    acceptance_items,
    gh_paginated,
    gh_json,
    issue,
    json_print,
    label_names,
    repo_slug,
    run,
    set_status,
    status_of,
)
from fetch_pr_feedback import fetch_feedback

REVIEW_ACTORS = {
    "coderabbit": {"coderabbitai", "coderabbitai[bot]"},
    "sourcery": {"sourcery-ai", "sourcery-ai[bot]", "sourcery"},
    "codeant": {"codeant-ai", "codeant-ai[bot]"},
}
CODEANT_STATUS_MARKER_RE = re.compile(
    r"<!--\s*codeant-review-status:(.*?)-->", re.DOTALL
)
CODEANT_MARKER_PREFIX_RE = re.compile(r"<!--\s*codeant-review-status", re.IGNORECASE)
CODEANT_STATUS_RECORD_KEYS = {"label", "commit", "started", "finished", "done"}


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _actor_is_trusted(actor: Any, service: str) -> bool:
    if not isinstance(actor, dict):
        return False
    login = str(actor.get("login") or "").lower()
    if login not in REVIEW_ACTORS[service]:
        return False
    actor_type = str(actor.get("type") or actor.get("__typename") or "")
    if actor_type and actor_type != "Bot":
        return False
    return True


def pull_request(number: int) -> dict[str, Any]:
    data = gh_json(
        [
            "pr",
            "view",
            str(number),
            "--json",
            (
                "number,title,body,state,isDraft,headRefOid,headRefName,baseRefName,"
                "mergeStateStatus,labels,statusCheckRollup,reviewDecision,author,url,"
                "mergedAt,mergeCommit"
            ),
        ]
    )
    if not isinstance(data, dict) or data.get("number") != number:
        raise KernelError(f"pull request #{number} is unavailable")
    return data


def linked_issues(body: str) -> list[int]:
    pattern = r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\s*$"
    return sorted({int(value) for value in re.findall(pattern, body or "")})


def assigned_service(pr: dict[str, Any]) -> str:
    labels = [name for name in label_names(pr) if name.startswith(REVIEW_PREFIX)]
    if len(labels) != 1:
        raise KernelError("PR must have exactly one review:<service> label")
    service = labels[0][len(REVIEW_PREFIX) :]
    if service not in REVIEW_SERVICES:
        raise KernelError(f"unsupported review service: {service}")
    return service


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def review_check_matches(record: dict[str, Any], service: str) -> bool:
    name = normalized(check_name(record))
    aliases = {
        "coderabbit": ("coderabbit",),
        "sourcery": ("sourceryreview", "sourcery"),
        "codeant": ("codeantai", "codeant"),
    }[service]
    return name in aliases


def successful_service_check(pr: dict[str, Any], service: str) -> bool:
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list):
        raise KernelError("review check state is incomplete")
    matches = [
        record
        for record in rollup
        if isinstance(record, dict) and review_check_matches(record, service)
    ]
    if len(matches) > 1:
        raise KernelError("assigned review service returned ambiguous checks")
    return len(matches) == 1 and check_state(matches[0]) == "success"


def pull_reviews(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")


def pull_comments(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")


def successful_service_review(number: int, head: str, service: str) -> bool:
    reviews = pull_reviews(number)
    approved = []
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author")
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        if _actor_is_trusted(actor, service) and commit_id == head:
            state = str(review.get("state") or "").upper()
            if state == "CHANGES_REQUESTED":
                return False
            if state == "APPROVED":
                approved.append(review)
    return bool(approved)


def codeant_reviews_allow_status(reviews: list[dict[str, Any]], head: str) -> bool:
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author")
        if not _actor_is_trusted(actor, "codeant"):
            continue
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        state = str(review.get("state") or "").upper()
        if commit_id == head:
            if state == "CHANGES_REQUESTED":
                return False
    return True


def _valid_codeant_record(record: Any) -> bool:
    if not isinstance(record, dict) or set(record.keys()) != CODEANT_STATUS_RECORD_KEYS:
        return False
    commit = record.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        return False
    if _parse_ts(record.get("started")) is None or _parse_ts(record.get("finished")) is None:
        return False
    if not isinstance(record.get("label"), str) or not record.get("label", "").strip():
        return False
    return isinstance(record.get("done"), bool)


def parse_codeant_status_payload(
    comment: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]] | None]:
    actor = comment.get("user") or comment.get("author")
    if not _actor_is_trusted(actor, "codeant"):
        return True, None
    body = comment.get("body")
    if not isinstance(body, str):
        return False, None
    if not CODEANT_MARKER_PREFIX_RE.search(body):
        return True, None
    matches = CODEANT_STATUS_MARKER_RE.findall(body)
    if len(matches) != 1:
        return False, None
    try:
        payload = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return False, None
    if not isinstance(payload, list):
        return False, None
    if any(not _valid_codeant_record(record) for record in payload):
        return False, None
    return True, payload


def validate_codeant_status_comments(comments: list[dict[str, Any]], head: str) -> bool:
    trusted_payloads: list[list[dict[str, Any]]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise KernelError("comment evidence is malformed")
        valid, payload = parse_codeant_status_payload(comment)
        if not valid:
            return False
        if payload is not None:
            trusted_payloads.append(payload)
    if len(trusted_payloads) != 1:
        return False

    records = trusted_payloads[0]
    head_records = [r for r in records if str(r.get("commit") or "").lower() == head.lower()]
    if len(head_records) != 1:
        return False
    return head_records[0]["done"] is True


def successful_codeant_status_review(number: int, head: str) -> bool:
    reviews = pull_reviews(number)
    if not codeant_reviews_allow_status(reviews, head):
        return False
    comments = pull_comments(number)
    return validate_codeant_status_comments(comments, head)


def exact_head_review(pr: dict[str, Any], number: int, service: str) -> bool:
    head = str(pr["headRefOid"])
    if successful_service_check(pr, service) or successful_service_review(number, head, service):
        return True
    if service == "codeant":
        return successful_codeant_status_review(number, head)
    return False


def base_snapshot(pr: dict[str, Any]) -> tuple[str, int]:
    slug = repo_slug()
    base = str(pr["baseRefName"])
    head = str(pr["headRefOid"])
    commit = gh_json(["api", f"repos/{slug}/commits/{base}"])
    compare = gh_json(["api", f"repos/{slug}/compare/{base}...{head}"])
    base_sha = commit.get("sha") if isinstance(commit, dict) else None
    behind = compare.get("behind_by") if isinstance(compare, dict) else None
    if not isinstance(base_sha, str) or len(base_sha) != 40 or not isinstance(behind, int):
        raise KernelError("base comparison is incomplete")
    return base_sha, behind


def issue_gate(numbers: list[int]) -> list[dict[str, object]]:
    if not numbers:
        raise KernelError("PR body must contain a closing issue directive")
    evidence: list[dict[str, object]] = []
    for number in numbers:
        record = issue(number)
        items = acceptance_items(str(record.get("body") or ""))
        if status_of(record) != "In Review":
            raise KernelError(f"issue #{number} is not In Review")
        if not items or any(not done for done, _ in items):
            raise KernelError(f"issue #{number} has incomplete Acceptance Criteria")
        evidence.append({"issue": number, "criteria": len(items)})
    return evidence


def evaluate(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        raise KernelError("PR is not an open, ready pull request")
    head = pr.get("headRefOid")
    if head != expected_head or not isinstance(head, str) or len(head) != 40:
        raise KernelError("expected head does not match the current PR head")
    if pr.get("mergeStateStatus") not in {"CLEAN", "HAS_HOOKS", "UNSTABLE"}:
        raise KernelError(f"PR merge state is {pr.get('mergeStateStatus')}")

    issues = linked_issues(str(pr.get("body") or ""))
    issue_evidence = issue_gate(issues)
    ci = ci_verdict(number)
    if ci["head"] != head or ci["state"] != "success":
        raise KernelError("exact-current-head CI is not successful")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved review thread(s)")
    service = assigned_service(pr)
    if not exact_head_review(pr, number, service):
        raise KernelError(f"{service} has no successful exact-head verdict")
    base_sha, behind = base_snapshot(pr)
    if behind:
        raise KernelError(f"PR head is behind {pr['baseRefName']} by {behind} commit(s)")
    return {
        "pr": number,
        "head": head,
        "base": pr["baseRefName"],
        "base_sha": base_sha,
        "branch": pr["headRefName"],
        "issues": issue_evidence,
        "ci": ci["checks"],
        "reviewer": service,
        "feedback": 0,
    }


def close_out(numbers: list[int]) -> None:
    for number in numbers:
        record = issue(number)
        set_status(number, "Done")
        if record.get("state") != "CLOSED":
            run(["gh", "issue", "close", str(number), "--reason", "completed"])


def merge(number: int, expected_head: str, *, dry_run: bool = False) -> dict[str, object]:
    gates = evaluate(number, expected_head)
    if dry_run:
        return {"merged": False, "gates": gates}

    live = pull_request(number)
    if live.get("headRefOid") != expected_head:
        raise KernelError("PR head changed after gate evaluation")
    current_base, behind = base_snapshot(live)
    if current_base != gates["base_sha"] or behind:
        raise KernelError("base changed after gate evaluation")
    run(
        [
            "gh",
            "pr",
            "merge",
            str(number),
            "--merge",
            "--delete-branch",
            "--match-head-commit",
            expected_head,
        ]
    )
    merged = pull_request(number)
    if not merged.get("mergedAt") or merged.get("headRefOid") != expected_head:
        raise KernelError("GitHub did not confirm the expected-head merge")
    issue_numbers = [int(item["issue"]) for item in gates["issues"]]
    close_out(issue_numbers)
    return {
        "merged": True,
        "pr": number,
        "head": expected_head,
        "merge_commit": (merged.get("mergeCommit") or {}).get("oid"),
        "issues": issue_numbers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = merge(args.pr, args.expected_head, dry_run=args.dry_run)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print("merge gates passed" if args.dry_run else f"merged PR #{args.pr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
