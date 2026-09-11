#!/usr/bin/env python3
"""The governed fail-closed merge and close-out helper."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from check_ci import ci_verdict, finalization_verdict, check_run_inventory
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
    UNAVAILABLE, authority_assigned_at, evidence_time, external_state, _trusted_actor,
)

CODING_REVIEW_MARKER_RE = re.compile(r"<!--\s*aru-coding-review:v1\s+(.*?)-->", re.DOTALL)
CODING_REVIEW_MARKER_PREFIX_RE = re.compile(r"<!--\s*aru-coding-review:", re.IGNORECASE)
CODING_REVIEW_KEYS = {
    "head", "reviewer", "family", "submitted_by", "verdict",
    "summary", "verification", "findings", "issues",
    "acceptance_criteria_reviewed",
    "diff_reviewed",
    "surrounding_code_reviewed",
}
CODING_FINDING_KEYS = {"severity", "file", "line", "summary", "resolved"}
CODING_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
GENERIC_APPROVALS = {"approve", "approved", "looks good", "lgtm", "no issues"}


def _actor_is_trusted(actor: Any, service: str) -> bool:
    return isinstance(actor, dict) and _trusted_actor({"user": actor}, service)


def assigned_service(pr: dict[str, Any]) -> str:
    labels = [name for name in label_names(pr) if name.startswith(REVIEW_PREFIX)]
    if len(labels) != 1:
        raise KernelError("PR must have exactly one review:<authority> label")
    service = labels[0][len(REVIEW_PREFIX) :]
    if service in RETIRED_EXTERNAL_REVIEWERS:
        raise KernelError("retired review authority; run create_pr.py --refresh-reviewer")
    if service not in REVIEW_AUTHORITIES:
        raise KernelError(f"unsupported review authority: {service}")
    reviewer_labels = [name for name in label_names(pr) if name.startswith(REVIEWER_PREFIX)]
    actor_labels = [name for name in label_names(pr) if name.startswith(REVIEWER_ACTOR_PREFIX)]
    if service in CODING_REVIEWERS and (len(reviewer_labels) != 1 or len(actor_labels) != 1):
        raise KernelError("coding review authority requires one reviewer identity and actor")
    if service in REVIEW_SERVICES and (reviewer_labels or actor_labels):
        raise KernelError("external review authority conflicts with coding reviewer metadata")
    return service


def _pull_records(kind: str, number: int) -> list[dict[str, Any]]:
    resource = "pulls" if kind == "reviews" else "issues"
    return gh_paginated(f"repos/{repo_slug()}/{resource}/{number}/{kind}?per_page=100")


def pull_reviews(number: int) -> list[dict[str, Any]]:
    return _pull_records("reviews", number)


def pull_comments(number: int) -> list[dict[str, Any]]:
    return _pull_records("comments", number)


def pull_events(number: int) -> list[dict[str, Any]]:
    return _pull_records("events", number)


def pull_review_checks(head: str) -> list[dict[str, Any]]:
    pages = gh_json(
        [
            "api", "--paginate", "--slurp",
            f"repos/{repo_slug()}/commits/{head}/check-runs?per_page=100&filter=latest",
        ]
    )
    return check_run_inventory(pages, paginated=True)


def _service_reviews(reviews: list[dict], head: str, service: str):
    """Share actor/head validation without treating stale records as approval."""
    for review in reviews:
        if not isinstance(review, dict):
            raise KernelError("review evidence is malformed")
        actor = review.get("user") or review.get("author")
        commit_id = review.get("commit_id") or (review.get("commit") or {}).get("oid")
        if _actor_is_trusted(actor, service) and commit_id == head:
            yield review


def _successful_service_review(reviews: list[dict], head: str, service: str, assigned_at: datetime) -> bool:
    records = list(_service_reviews(reviews, head, service))
    if any(str(record.get("state") or "").upper() == "CHANGES_REQUESTED" for record in records):
        return False
    return any(str(record.get("state") or "").upper() == "APPROVED"
               and not review_evidence_unavailable(record)
               and evidence_time(record, subject="external reviewer evidence") >= assigned_at
               for record in records)


def _trusted_changes_requested_at_head(reviews: list[dict], head: str, service: str, assigned_at: datetime) -> bool:
    return any(evidence_time(record, subject="external reviewer evidence") >= assigned_at
               and str(record.get("state") or "").upper() == "CHANGES_REQUESTED"
               for record in _service_reviews(reviews, head, service))


def _marker_payload(body: Any, prefix: re.Pattern, pattern: re.Pattern, validate) -> tuple:
    """Share strict marker cardinality, JSON and schema validation."""
    if not isinstance(body, str):
        return False, None
    if not prefix.search(body):
        return True, None
    matches = pattern.findall(body)
    if len(matches) != 1:
        return False, None
    try:
        payload = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return False, None
    return (True, payload) if validate(payload) else (False, None)


def _one_identity_label(pr: dict[str, Any], prefix: str) -> str | None:
    values = [name[len(prefix) :] for name in label_names(pr) if name.startswith(prefix)]
    return values[0] if len(values) == 1 else None


def _text(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


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
        and type(line) is int and line > 0
        and _text(summary, 10)
        and isinstance(finding.get("resolved"), bool)
    )


def _valid_coding_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or set(payload) != CODING_REVIEW_KEYS:
        return False
    head = payload.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return False
    for key in ("reviewer", "family", "submitted_by"):
        if not _text(payload.get(key)):
            return False
    verdict = payload.get("verdict")
    if verdict not in {"APPROVE", "REQUEST_CHANGES"}:
        return False
    summary = payload.get("summary")
    if not _text(summary, 40):
        return False
    if re.sub(r"\s+", " ", summary.strip().lower()) in GENERIC_APPROVALS:
        return False
    verification = payload.get("verification")
    if (not isinstance(verification, list) or not verification
            or any(not _text(item, 10) for item in verification)):
        return False
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not _valid_finding(item) for item in findings):
        return False
    issues = payload.get("issues")
    if (not isinstance(issues, list) or not issues
            or any(type(item) is not int or item <= 0 for item in issues)
            or issues != sorted(set(issues))):
        return False
    return all(payload[key] is True for key in CODING_REVIEW_KEYS if key.endswith("_reviewed"))


def parse_coding_review(review: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    if review.get("body") is None:
        return True, None
    return _marker_payload(
        review["body"], CODING_REVIEW_MARKER_PREFIX_RE, CODING_REVIEW_MARKER_RE,
        _valid_coding_payload,
    )


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
    reviews = pull_reviews(number)
    if service in CODING_REVIEWERS:
        return successful_coding_agent_review(
            pr, reviews, service, issue_numbers or linked_issues(str(pr.get("body") or "")),
        )
    comments = pull_comments(number)
    events = pull_events(number)
    checks = pull_review_checks(head)
    assigned_at = authority_assigned_at(pr, events, service)
    if external_state(service, reviews=reviews, comments=comments, checks=checks,
                      head=head, since=assigned_at) == UNAVAILABLE:
        return False
    if _trusted_changes_requested_at_head(reviews, head, service, assigned_at):
        return False
    # Only an approving exact-head review counts; a green/no-op check is not a
    # verdict, and assigned_service() refuses retired services before this point.
    return _successful_service_review(reviews, head, service, assigned_at)


def require_mergeable(pr: dict[str, Any], queue: dict[str, object]) -> None:
    if queue["configured"] or queue["entry"] is not None or queue["auto_merge"] is not None:
        raise KernelError("merge queues and pending auto-merge are unsupported; no merge submitted")
    merge_state = pr.get("mergeStateStatus")
    if pr.get("mergeable") != "MERGEABLE":
        raise KernelError("PR is not currently mergeable")
    if merge_state not in {"CLEAN", "UNSTABLE"}:
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
    ci, service = require_ci_review(pr, number, head, issues, risk_tier)
    return {
        "pr": number, "head": head, "branch": pr["headRefName"],
        "base": pr["baseRefName"], "base_sha": base_sha,
        "issues": issue_evidence, "changed_paths": changed_paths, "risk_tier": risk_tier,
        "ci": ci["checks"], "reviewer": service or "not-required", "feedback": 0,
        "merge_queue": queue["configured"],
        "queue_entry": queue["entry"], "auto_merge": queue["auto_merge"],
    }


def require_ci_review(pr: dict, number: int, head: str, issues: list[int], risk_tier: int, *, context: str = "", finalizing: bool = False) -> tuple:
    """Admission and close-out consume the same CI, thread and verdict gates."""
    ci = finalization_verdict(pr) if finalizing else ci_verdict(number)
    if ci["head"] != head or ci["state"] != "success":
        raise KernelError("exact-current-head required GitHub checks are not successful")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved {context}review thread(s)")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        raise KernelError(f"a submitted {context}review still requests changes")
    service = assigned_service(pr) if risk_tier >= 2 else None
    if service:
        require_review_verdict(pr, number, service, issues, context=f" ({context.strip()})" if context else "")
    return ci, service


def require_review_verdict(pr: dict, number: int, service: str, issues: list[int], *, context: str = "") -> None:
    if service in CODING_REVIEWERS:
        verdict = coding_review_verdict(pr, pull_reviews(number), service, issues)
        if verdict == "REQUEST_CHANGES":
            raise KernelError(f"{service} exact-head authoritative review requested changes")
        valid = verdict == "APPROVE"
    else:
        valid = exact_head_review(pr, number, service, issues)
    if not valid:
        raise KernelError(f"{service} has no successful exact-head verdict{context}")


def revalidate_review(pr: dict[str, Any], number: int, gates: dict[str, Any], issues: list[int]) -> None:
    """Recheck live review validity after the final PR/issue/queue reads."""
    if gates["risk_tier"] >= 2:
        service = assigned_service(pr)
        if service != gates["reviewer"]:
            raise KernelError("review authority changed before merge submission")
        require_review_verdict(pr, number, service, issues, context=" before merge submission")
    feedback = fetch_feedback(number)
    if feedback:
        raise KernelError(f"{len(feedback)} unresolved review thread(s) before merge submission")


def merge(number: int, expected_head: str, *, dry_run: bool = False) -> dict[str, object]:
    gates = evaluate(number, expected_head)
    if dry_run:
        return {"merged": False, "gates": gates}
    issue_numbers = [int(item["issue"]) for item in gates["issues"]]
    live_gates = evaluate(number, expected_head)
    if live_gates != gates:
        raise KernelError("merge authority changed during final gate evaluation")
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
        raise KernelError("GitHub did not confirm the expected-head merge; no issue closed")
    return finalize_queued(number, expected_head)


def finalize_queued(number: int, expected_head: str) -> dict[str, object]:
    pr = pull_request(number)
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise KernelError("PR has not merged yet")
    if pr.get("headRefOid") != expected_head:
        raise KernelError("expected head does not match the merged PR head")
    changed_paths = pull_changed_paths(number)
    numbers = linked_issues(str(pr.get("body") or ""))
    issue_gate(numbers, changed_paths, allow_closed=True, allow_done=True)
    risk_tier = review_risk_tier(changed_paths)
    _ci, service = require_ci_review(pr, number, expected_head, numbers, risk_tier, context="post-merge ", finalizing=True)
    evidence = close_out(numbers, changed_paths)
    return {
        "merged": True, "finalized": True, "pr": number, "head": expected_head,
        "merge_commit": pr["mergeCommit"]["oid"],
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
