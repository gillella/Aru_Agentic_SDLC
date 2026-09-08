#!/usr/bin/env python3
"""The governed fail-closed merge and close-out helper."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from check_ci import ci_verdict, check_name, check_state
from common import (
    AUTHOR_FAMILY_PREFIX, AUTHOR_PREFIX, CODING_REVIEWERS, REVIEW_PREFIX,
    REVIEWER_ACTOR_PREFIX, REVIEWER_PREFIX, REVIEW_AUTHORITIES, REVIEW_SERVICES, RETIRED_EXTERNAL_REVIEWERS,
    KernelError, gh_paginated, gh_json, json_print, label_names, same_github_actor,
    review_risk_tier, review_evidence_unavailable, repo_slug, run,
)
from fetch_pr_feedback import fetch_feedback
from merge_state import (
    base_snapshot, close_out, issue_gate, linked_issues, merge_queue_snapshot,
    pull_changed_paths, pull_request,
)
from review_evidence import (
    UNAVAILABLE, authority_assigned_at, evidence_time, external_state,
)

REVIEW_ACTORS = {
    "coderabbit": {"coderabbitai", "coderabbitai[bot]"},
    "sourcery": {"sourcery-ai", "sourcery-ai[bot]", "sourcery"},
    "codeant": {"codeant-ai", "codeant-ai[bot]"},
}
REVIEW_APP_SLUGS = {
    "coderabbit": {"coderabbitai"},
    "sourcery": {"sourcery-ai", "sourcery"},
    "codeant": {"codeant-ai", "codeant"},
}
CODEANT_STATUS_MARKER_RE = re.compile(
    r"<!--\s*codeant-review-status:(.*?)-->", re.DOTALL
)
CODEANT_MARKER_PREFIX_RE = re.compile(r"<!--\s*codeant-review-status", re.IGNORECASE)
CODEANT_STATUS_RECORD_KEYS = {"label", "commit", "started", "finished", "done"}
CODEANT_FULL_REVIEW_LABEL = "Reviewed your PR"
CODEANT_STATUS_LABELS = {CODEANT_FULL_REVIEW_LABEL, "Incremental review completed"}
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


def assigned_service(pr: dict[str, Any]) -> str:
    labels = [name for name in label_names(pr) if name.startswith(REVIEW_PREFIX)]
    if len(labels) != 1:
        raise KernelError("PR must have exactly one review:<authority> label")
    service = labels[0][len(REVIEW_PREFIX) :]
    if service in RETIRED_EXTERNAL_REVIEWERS:
        raise KernelError("retired review authority; run create_pr.py --refresh-reviewer")
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


def successful_service_check(
    pr: dict[str, Any],
    service: str,
    checks: list[dict[str, Any]],
    assigned_at: datetime,
) -> bool:
    head = str(pr.get("headRefOid") or "")
    summary = pr.get("statusCheckRollup")
    if not isinstance(summary, list):
        raise KernelError("review check state is incomplete")
    summarized = [
        record
        for record in summary
        if isinstance(record, dict) and review_check_matches(record, service)
    ]
    if len(summarized) > 1:
        raise KernelError("assigned review service returned ambiguous checks")
    if len(summarized) != 1 or check_state(summarized[0]) != "success":
        return False
    matches = [
        record
        for record in checks
        if review_check_matches(record, service)
    ]
    if len(matches) > 1:
        raise KernelError("assigned review service returned ambiguous checks")
    if len(matches) != 1:
        return False
    match = matches[0]
    app = match.get("app")
    return bool(
        isinstance(app, dict)
        and app.get("slug") in REVIEW_APP_SLUGS[service]
        and match.get("head_sha") == head
        and check_state(match) == "success"
        and not review_evidence_unavailable(match)
        and evidence_time(match, subject="external reviewer check") >= assigned_at
    )


def pull_reviews(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")


def pull_comments(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")


def pull_events(number: int) -> list[dict[str, Any]]:
    slug = repo_slug()
    return gh_paginated(f"repos/{slug}/issues/{number}/events?per_page=100")


def pull_review_checks(head: str) -> list[dict[str, Any]]:
    pages = gh_json(
        [
            "api", "--paginate", "--slurp",
            f"repos/{repo_slug()}/commits/{head}/check-runs?per_page=100&filter=latest",
        ]
    )
    if (
        not isinstance(pages, list)
        or any(not isinstance(page, dict) for page in pages)
        or any(not isinstance(page.get("check_runs"), list) for page in pages)
    ):
        raise KernelError("review check-run inventory is malformed")
    checks = [record for page in pages for record in page["check_runs"]]
    totals = {page.get("total_count") for page in pages}
    if (
        any(not isinstance(record, dict) for record in checks)
        or len(totals) != 1
        or totals.pop() != len(checks)
    ):
        raise KernelError("review check-run inventory is incomplete")
    return checks


def _successful_service_review(
    reviews: list[dict[str, Any]], head: str, service: str, assigned_at: datetime
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
            if (
                state == "APPROVED"
                and not review_evidence_unavailable(review)
                and evidence_time(review, subject="external reviewer evidence")
                >= assigned_at
            ):
                approved.append(review)
    return bool(approved)


def _trusted_changes_requested_at_head(
    reviews: list[dict[str, Any]],
    head: str,
    service: str,
    assigned_at: datetime,
) -> bool:
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author")
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        state = str(review.get("state") or "").upper()
        if (
            _actor_is_trusted(actor, service)
            and commit_id == head
            and evidence_time(review, subject="external reviewer evidence")
            >= assigned_at
        ):
            if state == "CHANGES_REQUESTED":
                return True
    return False


def trusted_codeant_review_history(
    reviews: list[dict[str, Any]], head: str, assigned_at: datetime
) -> bool:
    if _trusted_changes_requested_at_head(reviews, head, "codeant", assigned_at):
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
            and evidence_time(review, subject="external reviewer evidence")
            >= assigned_at
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

def validate_codeant_status_comments(
    comments: list[dict[str, Any]], head: str, assigned_at: datetime
) -> bool:
    trusted_payloads: list[list[dict[str, Any]]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise KernelError("comment evidence is malformed")
        actor = comment.get("user") or comment.get("author")
        valid, payload = parse_codeant_status_payload(comment)
        if _actor_is_trusted(actor, "codeant") and (not valid or payload is not None) and (
            evidence_time(comment, subject="external reviewer evidence") < assigned_at
        ):
            continue
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
        and _parse_ts(record["finished"]) >= assigned_at
    ]
    if len(full_head_records) != 1:
        return False
    return True


def successful_codeant_status_review(
    head: str,
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    assigned_at: datetime,
) -> bool:
    if not trusted_codeant_review_history(reviews, head, assigned_at):
        return False
    return validate_codeant_status_comments(comments, head, assigned_at)


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
        if not same_github_actor(str(actor.get("login") or ""), reviewer_actor):
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
    github_author = str((pr.get("author") or {}).get("login") or "")
    if not all((reviewer, reviewer_actor, author, family, github_author)) or reviewer == author:
        return None
    return reviewer, reviewer_actor, author, family, github_author


def _review_submission_matches(
    review: dict[str, Any], payload: dict[str, Any], head: str, github_author: str
) -> bool:
    actor = review.get("user") or review.get("author")
    if not isinstance(actor, dict):
        return False
    actor_login = str(actor.get("login") or "")
    commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
    return bool(
        actor_login
        and not same_github_actor(actor_login, github_author)
        and commit_id == head
        and same_github_actor(payload["submitted_by"], actor_login)
    )


def successful_coding_agent_review(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    authority: str,
    issue_numbers: list[int],
) -> bool:
    return coding_review_verdict(pr, reviews, authority, issue_numbers) == "APPROVE"


def coding_review_verdict(
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    authority: str,
    issue_numbers: list[int],
) -> str | None:
    head = str(pr.get("headRefOid") or "")
    assignment = _coding_assignment(pr)
    if assignment is None:
        return None
    reviewer_identity, reviewer_actor, author_identity, _author_family, github_author = assignment
    current = _current_coding_attestation(reviews, head, reviewer_actor)
    if current is None:
        return None
    review, payload = current
    if not _review_submission_matches(review, payload, head, github_author):
        return None
    actor = review.get("user") or review.get("author") or {}
    if not same_github_actor(str(actor.get("login") or ""), reviewer_actor):
        return None
    if payload["reviewer"] != reviewer_identity or payload["family"] != authority:
        return None
    if payload["reviewer"] == author_identity or payload["issues"] != issue_numbers:
        return None
    state = str(review.get("state") or "").upper()
    expected_state = "APPROVED" if payload["verdict"] == "APPROVE" else "CHANGES_REQUESTED"
    if state != expected_state:
        return None
    if payload["verdict"] == "REQUEST_CHANGES":
        return "REQUEST_CHANGES"
    if any(not finding["resolved"] for finding in payload["findings"]):
        return None
    return "APPROVE"


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
    comments = pull_comments(number)
    events = pull_events(number)
    checks = pull_review_checks(head)
    assigned_at = authority_assigned_at(pr, events, service)
    if external_state(
        service,
        reviews=reviews,
        comments=comments,
        checks=checks,
        head=head,
        since=assigned_at,
    ) == UNAVAILABLE:
        return False
    if _trusted_changes_requested_at_head(reviews, head, service, assigned_at):
        return False
    if _successful_service_review(reviews, head, service, assigned_at):
        return True
    if service == "coderabbit":
        return False  # green/no-op check is not a substantive current-head verdict
    if service == "codeant":
        return successful_codeant_status_review(head, reviews, comments, assigned_at)
    return successful_service_check(pr, service, checks, assigned_at)


def require_mergeable(pr: dict[str, Any], queue: dict[str, object]) -> None:
    submitted = queue["entry"] is not None or queue["auto_merge"] is not None
    merge_state = pr.get("mergeStateStatus")
    if not submitted and pr.get("mergeable") != "MERGEABLE":
        raise KernelError("PR is not currently mergeable")
    allowed = merge_state in {"CLEAN", "UNSTABLE"}
    queued_behind = merge_state == "BEHIND" and bool(queue["configured"])
    if not allowed and not queued_behind and not submitted:
        raise KernelError(f"PR merge state is {merge_state}")


def evaluate(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        raise KernelError("PR is not an open, ready pull request")
    head = pr.get("headRefOid")
    if (
        head != expected_head
        or not isinstance(head, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", head)
    ):
        raise KernelError("expected head does not match the current PR head")
    base_sha = base_snapshot(pr)
    queue = merge_queue_snapshot(number, expected_head, base_sha)
    require_mergeable(pr, queue)

    changed_paths = pull_changed_paths(number)
    risk_tier = review_risk_tier(changed_paths)
    issues = linked_issues(str(pr.get("body") or ""))
    issue_evidence = issue_gate(issues, changed_paths)
    ci = ci_verdict(number)
    if ci["head"] != head or ci["state"] != "success":
        raise KernelError("exact-current-head required GitHub checks are not successful")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved review thread(s)")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        raise KernelError("a submitted review still requests changes")
    service: str | None = None
    if risk_tier >= 2:
        service = assigned_service(pr)
        if service in CODING_REVIEWERS:
            verdict = coding_review_verdict(pr, pull_reviews(number), service, issues)
            if verdict == "REQUEST_CHANGES":
                raise KernelError(f"{service} exact-head authoritative review requested changes")
            if verdict != "APPROVE":
                raise KernelError(f"{service} has no successful exact-head verdict")
        elif not exact_head_review(pr, number, service, issues):
            raise KernelError(f"{service} has no successful exact-head verdict")
    return {
        "pr": number,
        "head": head,
        "base": pr["baseRefName"],
        "base_sha": base_sha,
        "branch": pr["headRefName"],
        "issues": issue_evidence,
        "changed_paths": changed_paths,
        "risk_tier": risk_tier,
        "ci": ci["checks"],
        "reviewer": service or "not-required",
        "feedback": 0,
        "merge_queue": queue["configured"],
        "queue_entry": queue["entry"],
        "auto_merge": queue["auto_merge"],
    }


def revalidate_review(pr: dict[str, Any], number: int, gates: dict[str, Any], issues: list[int]) -> None:
    """Recheck live review validity after the final PR/issue/queue reads."""
    if gates["risk_tier"] >= 2:
        service = assigned_service(pr)
        if service != gates["reviewer"]:
            raise KernelError("review authority changed before merge submission")
        if service in CODING_REVIEWERS:
            verdict = coding_review_verdict(pr, pull_reviews(number), service, issues)
            if verdict == "REQUEST_CHANGES":
                raise KernelError(f"{service} exact-head authoritative review requested changes")
            valid = verdict == "APPROVE"
        else:
            valid = exact_head_review(pr, number, service, issues)
        if not valid:
            raise KernelError(f"{service} has no successful exact-head verdict before merge submission")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved review thread(s) before merge submission")


def merge(number: int, expected_head: str, *, dry_run: bool = False) -> dict[str, object]:
    gates = evaluate(number, expected_head)
    if dry_run:
        return {"merged": False, "gates": gates}
    issue_numbers = [int(item["issue"]) for item in gates["issues"]]
    if gates["queue_entry"] is not None or gates["auto_merge"] is not None:
        return {
            "merged": False,
            "queued": gates["queue_entry"] is not None,
            "auto_merge": gates["auto_merge"] is not None,
            "pr": number,
            "head": expected_head,
            "issues": issue_numbers,
            "next_action": "finalize-queued-merge",
        }
    live_gates = evaluate(number, expected_head)
    if live_gates != gates:
        raise KernelError("merge authority changed during final gate evaluation")
    command = ["gh", "pr", "merge", str(number)]
    if not gates["merge_queue"]:
        command.append("--merge")
    command.extend(["--match-head-commit", expected_head])
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
        raise KernelError("PR authorization changed before merge submission")
    if issue_gate(issue_numbers, gates["changed_paths"]) != gates["issues"]:
        raise KernelError("issue authorization changed before merge submission")
    final_queue = merge_queue_snapshot(number, expected_head, str(gates["base_sha"]))
    if (final_queue["configured"], final_queue["entry"], final_queue["auto_merge"]) != (
        gates["merge_queue"], gates["queue_entry"], gates["auto_merge"],
    ):
        raise KernelError("merge queue or pending request changed before merge submission")
    revalidate_review(final_pr, number, gates, issue_numbers)
    run(command)
    merged = pull_request(number)
    if merged.get("headRefOid") != expected_head:
        raise KernelError("PR head changed during merge submission")
    if not merged.get("mergedAt"):
        if not gates["merge_queue"]:
            raise KernelError("GitHub did not confirm the expected-head merge")
        queued = merge_queue_snapshot(number, expected_head, str(gates["base_sha"]))
        if queued["entry"] is None and queued["auto_merge"] is None:
            raise KernelError("GitHub did not confirm merge-queue or auto-merge submission")
        return {
            "merged": False,
            "queued": queued["entry"] is not None,
            "auto_merge": queued["auto_merge"] is not None,
            "pr": number,
            "head": expected_head,
            "issues": issue_numbers,
            "next_action": "finalize-queued-merge",
        }
    return finalize_queued(number, expected_head)


def finalize_queued(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise KernelError("queued PR has not merged yet")
    if pr.get("headRefOid") != expected_head:
        raise KernelError("expected head does not match the merged PR head")
    merge_commit = (pr.get("mergeCommit") or {}).get("oid")
    if not isinstance(merge_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit):
        raise KernelError("merged PR has no exact merge commit")
    changed_paths = pull_changed_paths(number)
    numbers = linked_issues(str(pr.get("body") or ""))
    issue_gate(
        numbers, changed_paths, allow_closed=True, allow_done=True
    )
    ci = ci_verdict(number)
    if ci["head"] != expected_head or ci["state"] != "success":
        raise KernelError("exact-head required GitHub checks are not successful")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved post-queue review thread(s)")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        raise KernelError("a submitted post-queue review requests changes")
    risk_tier = review_risk_tier(changed_paths)
    service: str | None = None
    if risk_tier >= 2:
        service = assigned_service(pr)
        if service in CODING_REVIEWERS:
            verdict = coding_review_verdict(pr, pull_reviews(number), service, numbers)
            if verdict != "APPROVE":
                raise KernelError(f"{service} post-queue exact-head review is not approved")
        elif not exact_head_review(pr, number, service, numbers):
            raise KernelError(f"{service} post-queue exact-head review is not approved")
    evidence = close_out(numbers, changed_paths)
    return {
        "merged": True,
        "finalized": True,
        "pr": number,
        "head": expected_head,
        "merge_commit": merge_commit,
        "issues": [int(item["issue"]) for item in evidence],
        "risk_tier": risk_tier,
        "reviewer": service or "not-required",
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
