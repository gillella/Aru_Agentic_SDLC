#!/usr/bin/env python3
"""The sole fail-closed merge and close-out authority."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from check_ci import ci_verdict, check_name, check_state
from common import (
    AUTHOR_FAMILY_PREFIX,
    AUTHOR_PREFIX,
    CODING_REVIEWERS,
    REVIEW_PREFIX,
    REVIEWER_ACTOR_PREFIX,
    REVIEWER_PREFIX,
    REVIEW_AUTHORITIES,
    REVIEW_SERVICES,
    KernelError,
    acceptance_items,
    gh_paginated,
    gh_json,
    issue,
    json_print,
    label_names,
    review_evidence_unavailable,
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
CODEANT_FULL_REVIEW_LABEL = "Reviewed your PR"
CODEANT_STATUS_LABELS = {
    CODEANT_FULL_REVIEW_LABEL,
    "Incremental review completed",
}
CODING_REVIEW_MARKER_RE = re.compile(
    r"<!--\s*aru-coding-review:v1\s+(.*?)-->", re.DOTALL
)
CODING_REVIEW_MARKER_PREFIX_RE = re.compile(
    r"<!--\s*aru-coding-review:", re.IGNORECASE
)
CODING_REVIEW_KEYS = {
    "head",
    "reviewer",
    "family",
    "submitted_by",
    "verdict",
    "summary",
    "verification",
    "findings",
    "issues",
    "acceptance_criteria_reviewed",
    "diff_reviewed",
    "surrounding_code_reviewed",
}
CODING_FINDING_KEYS = {"severity", "file", "line", "summary", "resolved"}
CODING_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
GENERIC_APPROVALS = {"approve", "approved", "looks good", "lgtm", "no issues"}


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
        raise KernelError("PR must have exactly one review:<authority> label")
    service = labels[0][len(REVIEW_PREFIX) :]
    if service not in REVIEW_AUTHORITIES:
        raise KernelError(f"unsupported review authority: {service}")
    reviewer_labels = [
        name for name in label_names(pr) if name.startswith(REVIEWER_PREFIX)
    ]
    actor_labels = [
        name for name in label_names(pr) if name.startswith(REVIEWER_ACTOR_PREFIX)
    ]
    if service in CODING_REVIEWERS and (len(reviewer_labels) != 1 or len(actor_labels) != 1):
        raise KernelError("coding review authority requires one reviewer identity and actor")
    if service in REVIEW_SERVICES and (reviewer_labels or actor_labels):
        raise KernelError("external review authority conflicts with coding reviewer metadata")
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
    if len(matches) != 1 or check_state(matches[0]) != "success":
        return False
    statuses = [
        record
        for record in review_statuses(str(pr.get("headRefOid") or ""))
        if review_check_matches(record, service)
    ]
    if not statuses:
        return True
    timestamps = [
        str(record.get("updated_at") or record.get("created_at") or "")
        for record in statuses
    ]
    if any(not timestamp for timestamp in timestamps):
        raise KernelError("assigned review service status timestamp is incomplete")
    latest = max(timestamps)
    current = [
        record for record, timestamp in zip(statuses, timestamps) if timestamp == latest
    ]
    states = {
        (check_state(record), review_evidence_unavailable(record)) for record in current
    }
    return states == {("success", False)}


def pull_reviews(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")


def pull_comments(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")


def review_statuses(head: str) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/commits/{head}/statuses?per_page=100")


def _successful_service_review(
    reviews: list[dict[str, Any]], head: str, service: str
) -> bool:
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


def successful_service_review(number: int, head: str, service: str) -> bool:
    return _successful_service_review(pull_reviews(number), head, service)


def _trusted_changes_requested_at_head(
    reviews: list[dict[str, Any]], head: str, service: str
) -> bool:
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author")
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        state = str(review.get("state") or "").upper()
        if _actor_is_trusted(actor, service) and commit_id == head:
            if state == "CHANGES_REQUESTED":
                return True
    return False


def trusted_codeant_review_history(reviews: list[dict[str, Any]], head: str) -> bool:
    if _trusted_changes_requested_at_head(reviews, head, "codeant"):
        return False
    for review in reviews:
        actor = review.get("user") or review.get("author")
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        state = str(review.get("state") or "").upper()
        if (
            _actor_is_trusted(actor, "codeant")
            and isinstance(commit_id, str)
            and re.fullmatch(r"[0-9a-fA-F]{40}", commit_id)
            and state in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
        ):
            return True
    return False


def _valid_codeant_record(record: Any) -> bool:
    if not isinstance(record, dict) or set(record.keys()) != CODEANT_STATUS_RECORD_KEYS:
        return False
    commit = record.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        return False
    if _parse_ts(record.get("started")) is None or _parse_ts(record.get("finished")) is None:
        return False
    if record.get("label") not in CODEANT_STATUS_LABELS:
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
    if any(record["done"] is not True for record in records):
        return False
    full_head_records = [
        record
        for record in records
        if record["label"] == CODEANT_FULL_REVIEW_LABEL
        and record["commit"].lower() == head.lower()
    ]
    if len(full_head_records) != 1:
        return False
    return True


def successful_codeant_status_review(
    number: int, head: str, reviews: list[dict[str, Any]] | None = None
) -> bool:
    reviews = pull_reviews(number) if reviews is None else reviews
    if not trusted_codeant_review_history(reviews, head):
        return False
    comments = pull_comments(number)
    return validate_codeant_status_comments(comments, head)


def _one_identity_label(pr: dict[str, Any], prefix: str) -> str | None:
    values = [name[len(prefix) :] for name in label_names(pr) if name.startswith(prefix)]
    return values[0] if len(values) == 1 else None


def _valid_finding(finding: Any) -> bool:
    if not isinstance(finding, dict) or set(finding) != CODING_FINDING_KEYS:
        return False
    path = finding.get("file")
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    if ".." in PurePosixPath(path).parts:
        return False
    line = finding.get("line")
    summary = finding.get("summary")
    return (
        finding.get("severity") in CODING_FINDING_SEVERITIES
        and isinstance(line, int)
        and not isinstance(line, bool)
        and line > 0
        and isinstance(summary, str)
        and len(summary.strip()) >= 10
        and isinstance(finding.get("resolved"), bool)
    )


def _valid_coding_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or set(payload) != CODING_REVIEW_KEYS:
        return False
    head = payload.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return False
    for key in ("reviewer", "family", "submitted_by"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False
    verdict = payload.get("verdict")
    if verdict not in {"APPROVE", "REQUEST_CHANGES"}:
        return False
    summary = payload.get("summary")
    if not isinstance(summary, str) or len(summary.strip()) < 40:
        return False
    if re.sub(r"\s+", " ", summary.strip().lower()) in GENERIC_APPROVALS:
        return False
    verification = payload.get("verification")
    if (
        not isinstance(verification, list)
        or not verification
        or any(not isinstance(item, str) or len(item.strip()) < 10 for item in verification)
    ):
        return False
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not _valid_finding(item) for item in findings):
        return False
    issues = payload.get("issues")
    if (
        not isinstance(issues, list)
        or not issues
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in issues)
        or issues != sorted(set(issues))
    ):
        return False
    return all(
        payload.get(key) is True
        for key in (
            "acceptance_criteria_reviewed",
            "diff_reviewed",
            "surrounding_code_reviewed",
        )
    )


def parse_coding_review(review: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    body = review.get("body")
    if body is None:
        return True, None
    if not isinstance(body, str):
        return False, None
    if not CODING_REVIEW_MARKER_PREFIX_RE.search(body):
        return True, None
    matches = CODING_REVIEW_MARKER_RE.findall(body)
    if len(matches) != 1:
        return False, None
    try:
        payload = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return False, None
    if not _valid_coding_payload(payload):
        return False, None
    return True, payload


def _current_coding_attestation(
    reviews: list[dict[str, Any]], head: str, reviewer_actor: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author") or {}
        if str(actor.get("login") or "").lower() != reviewer_actor.lower():
            continue
        valid, payload = parse_coding_review(review)
        if not valid:
            return None
        if payload is not None and payload["head"].lower() == head.lower():
            current.append((review, payload))
    return current[0] if len(current) == 1 else None


def _coding_assignment(pr: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    reviewer = _one_identity_label(pr, REVIEWER_PREFIX)
    reviewer_actor = _one_identity_label(pr, REVIEWER_ACTOR_PREFIX)
    author = _one_identity_label(pr, AUTHOR_PREFIX)
    family = _one_identity_label(pr, AUTHOR_FAMILY_PREFIX)
    github_author = str((pr.get("author") or {}).get("login") or "").lower()
    if not all((reviewer, reviewer_actor, author, family, github_author)) or reviewer == author:
        return None
    return reviewer, reviewer_actor, author, family, github_author


def _review_submission_matches(
    review: dict[str, Any], payload: dict[str, Any], head: str, github_author: str
) -> bool:
    actor = review.get("user") or review.get("author")
    if not isinstance(actor, dict):
        return False
    actor_login = str(actor.get("login") or "").lower()
    commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
    return bool(
        actor_login
        and actor_login != github_author
        and commit_id == head
        and payload["submitted_by"].lower() == actor_login
    )


def successful_coding_agent_review(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    authority: str,
    issue_numbers: list[int],
) -> bool:
    head = str(pr.get("headRefOid") or "")
    assignment = _coding_assignment(pr)
    if assignment is None:
        return False
    reviewer_identity, reviewer_actor, author_identity, _author_family, github_author = assignment
    current = _current_coding_attestation(reviews, head, reviewer_actor)
    if current is None:
        return False
    review, payload = current
    if not _review_submission_matches(review, payload, head, github_author):
        return False
    actor = review.get("user") or review.get("author") or {}
    if str(actor.get("login") or "").lower() != reviewer_actor.lower():
        return False
    if payload["reviewer"] != reviewer_identity or payload["family"] != authority:
        return False
    if payload["reviewer"] == author_identity or payload["issues"] != issue_numbers:
        return False
    state = str(review.get("state") or "").upper()
    expected_state = "APPROVED" if payload["verdict"] == "APPROVE" else "CHANGES_REQUESTED"
    if state != expected_state or payload["verdict"] != "APPROVE":
        return False
    if any(not finding["resolved"] for finding in payload["findings"]):
        return False
    return True


def exact_head_review(
    pr: dict[str, Any],
    number: int,
    service: str,
    issue_numbers: list[int] | None = None,
) -> bool:
    head = str(pr["headRefOid"])
    if service in CODING_REVIEWERS:
        return successful_coding_agent_review(
            pr,
            pull_reviews(number),
            service,
            issue_numbers or linked_issues(str(pr.get("body") or "")),
        )
    reviews = pull_reviews(number)
    if _trusted_changes_requested_at_head(reviews, head, service):
        return False
    if service == "codeant":
        if successful_service_check(pr, service) or _successful_service_review(
            reviews, head, service
        ):
            return True
        return successful_codeant_status_review(number, head, reviews)
    if successful_service_check(pr, service) or _successful_service_review(reviews, head, service):
        return True
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
    if not exact_head_review(pr, number, service, issues):
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
