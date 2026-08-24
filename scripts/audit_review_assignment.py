#!/usr/bin/env python3
"""Read-only audit of a pull request's deterministic review assignment."""

import argparse
import json
import re

from merge_pr import (
    CODEANT_LOGINS,
    CODERABBIT_LOGINS,
    REVIEW_SERVICE_LABELS,
    SOURCERY_LOGINS,
    assigned_review_service,
    fetch_pr,
    linked_issues,
    review_evidence,
    review_service_for_issue,
)


SCHEMA = "aru.review-assignment-audit.v1"
SERVICES = ("coderabbit", "sourcery", "codeant")
SERVICE_LOGINS = {
    "coderabbit": {login.lower() for login in CODERABBIT_LOGINS},
    "sourcery": {login.lower() for login in SOURCERY_LOGINS},
    "codeant": {login.lower() for login in CODEANT_LOGINS},
}
HEAD_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
AGGREGATE_THREAD_FIELDS = (
    "unresolved",
    "unfixed",
    "outdated_unfixed",
    "outdated_addressed",
    "body_addressed",
    "withdrawn",
)
SERVICE_THREAD_FIELDS = ("unresolved", "unfixed", "outdated_unfixed")


def _base_report(pr_number, expected_head):
    return {
        "schema": SCHEMA,
        "ok": False,
        "pr": {
            "requested_number": pr_number,
            "number": None,
            "state": None,
            "is_draft": None,
        },
        "assignment": {
            "linked_issues": [],
            "expected_service": None,
            "review_labels": [],
            "label_service": None,
            "validated_service": None,
        },
        "heads": {
            "expected": expected_head,
            "pr": None,
            "review_evidence": None,
        },
        "checks": {
            "total": 0,
            "by_review_service": {service: 0 for service in SERVICES},
            "items": [],
        },
        "reviews": {
            "total": 0,
            "exact_head": 0,
            "assigned_service_total": 0,
            "assigned_service_exact_head": 0,
            "by_service_total": {service: 0 for service in SERVICES},
            "by_service_exact_head": {service: 0 for service in SERVICES},
            "by_state": {},
        },
        "threads": {
            "aggregate": None,
            "assigned_service": None,
            "by_service": None,
        },
        "mismatches": [],
    }


def _add_mismatch(report, code, message):
    report["mismatches"].append({"code": code, "message": message})


def _valid_head(value):
    return isinstance(value, str) and HEAD_RE.fullmatch(value) is not None


def _assignment_summary(pr, report):
    issue_numbers = linked_issues(pr.get("body") or "")
    linked = [
        {"number": number, "service": review_service_for_issue(number)}
        for number in issue_numbers
    ]
    expected_services = {item["service"] for item in linked}
    expected = next(iter(expected_services)) if len(expected_services) == 1 else None

    raw_labels = pr.get("labels")
    labels_well_formed = isinstance(raw_labels, list) and all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        for item in raw_labels or []
    )
    review_labels = sorted(
        item["name"] for item in (raw_labels or [])
        if isinstance(item, dict) and item.get("name") in REVIEW_SERVICE_LABELS
    )
    label_service = (
        review_labels[0].split(":", 1)[1] if len(review_labels) == 1 else None
    )

    report["assignment"] = {
        "linked_issues": linked,
        "expected_service": expected,
        "review_labels": review_labels,
        "label_service": label_service,
        "validated_service": assigned_review_service(pr) if labels_well_formed else None,
    }
    if not issue_numbers:
        _add_mismatch(
            report,
            "linked_issue_missing",
            "PR body has no Closes #<issue> link, so assignment cannot be recomputed.",
        )
    elif len(expected_services) != 1:
        _add_mismatch(
            report,
            "deterministic_assignment_ambiguous",
            "Linked issues deterministically map to different review services.",
        )
    if not labels_well_formed:
        _add_mismatch(report, "review_labels_malformed", "PR label data is malformed.")
    elif len(review_labels) != 1:
        _add_mismatch(
            report,
            "review_label_count",
            f"Expected exactly one review:* label; found {len(review_labels)}.",
        )
    elif expected is not None and label_service != expected:
        _add_mismatch(
            report,
            "review_label_mismatch",
            f"Current label assigns {label_service}; linked issues assign {expected}.",
        )
    return expected


def _review_service_for_check(name):
    normalized = name.strip().lower()
    if normalized == "coderabbit":
        return "coderabbit"
    if normalized == "sourcery review":
        return "sourcery"
    if normalized.startswith("codeant"):
        return "codeant"
    return None


def _check_summary(pr, assigned_service, report):
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list):
        _add_mismatch(report, "checks_unavailable", "Current-head check data is unavailable.")
        return
    items = []
    by_service = {service: 0 for service in SERVICES}
    for item in rollup:
        if not isinstance(item, dict):
            _add_mismatch(report, "checks_malformed", "A current-head check entry is malformed.")
            return
        name = item.get("name") or item.get("context")
        status = item.get("status") or item.get("state")
        if not isinstance(name, str) or not name or not isinstance(status, str) or not status:
            _add_mismatch(report, "checks_malformed", "A current-head check lacks a name or status.")
            return
        review_service = _review_service_for_check(name)
        if review_service:
            by_service[review_service] += 1
        items.append({
            "type": item.get("__typename") or "unknown",
            "name": name,
            "status": status,
            "conclusion": item.get("conclusion"),
            "review_service": review_service,
        })
    report["checks"] = {
        "total": len(items),
        "by_review_service": by_service,
        "items": items,
    }
    unexpected = [
        service for service, count in by_service.items()
        if count and assigned_service and service != assigned_service
    ]
    if unexpected:
        _add_mismatch(
            report,
            "unexpected_review_service_check",
            f"Unassigned review-service checks ran: {', '.join(unexpected)}.",
        )


def _service_for_login(login):
    normalized = login.lower()
    return next(
        (service for service, logins in SERVICE_LOGINS.items() if normalized in logins),
        None,
    )


def _review_summary(evidence, head, assigned_service, report):
    reviews = evidence.get("reviews")
    if not isinstance(reviews, list):
        _add_mismatch(report, "reviews_malformed", "Review evidence is not a list.")
        return
    summary = report["reviews"]
    for review in reviews:
        if not isinstance(review, dict):
            _add_mismatch(report, "reviews_malformed", "A review entry is malformed.")
            return
        author = review.get("author")
        commit = review.get("commit")
        state = review.get("state")
        if (
            not isinstance(author, dict)
            or not isinstance(author.get("login"), str)
            or not author["login"]
            or not isinstance(state, str)
            or commit is not None and not isinstance(commit, dict)
        ):
            _add_mismatch(report, "reviews_malformed", "A review entry lacks trusted fields.")
            return
        oid = (commit or {}).get("oid")
        if oid is not None and not _valid_head(oid):
            _add_mismatch(report, "reviews_malformed", "A review commit OID is malformed.")
            return
        service = _service_for_login(author["login"])
        exact_head = isinstance(head, str) and oid == head
        summary["total"] += 1
        summary["by_state"][state] = summary["by_state"].get(state, 0) + 1
        if exact_head:
            summary["exact_head"] += 1
            if service:
                summary["by_service_exact_head"][service] += 1
        if service:
            summary["by_service_total"][service] += 1
        if assigned_service and service == assigned_service:
            summary["assigned_service_total"] += 1
            if exact_head:
                summary["assigned_service_exact_head"] += 1
    unexpected = [
        service for service, count in summary["by_service_total"].items()
        if count and assigned_service and service != assigned_service
    ]
    if unexpected:
        _add_mismatch(
            report,
            "unexpected_review_service_review",
            f"Unassigned review services submitted reviews: {', '.join(unexpected)}.",
        )


def _nonnegative_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _thread_summary(evidence, assigned_service, report):
    aggregate = {field: evidence.get(field) for field in AGGREGATE_THREAD_FIELDS}
    by_service = evidence.get("service_threads")
    valid = all(_nonnegative_count(value) for value in aggregate.values())
    valid = valid and isinstance(by_service, dict)
    normalized = {}
    for service in SERVICES:
        counts = by_service.get(service) if isinstance(by_service, dict) else None
        if not isinstance(counts, dict):
            valid = False
            continue
        selected = {field: counts.get(field) for field in SERVICE_THREAD_FIELDS}
        if not all(_nonnegative_count(value) for value in selected.values()):
            valid = False
        normalized[service] = selected
    if not valid:
        _add_mismatch(report, "threads_malformed", "Review-thread counts are unavailable or malformed.")
        return
    report["threads"] = {
        "aggregate": aggregate,
        "assigned_service": normalized.get(assigned_service),
        "by_service": normalized,
    }
    unexpected = [
        service for service, counts in normalized.items()
        if assigned_service
        and service != assigned_service
        and any(counts.values())
    ]
    if unexpected:
        _add_mismatch(
            report,
            "unexpected_review_service_thread",
            f"Unassigned review services own thread evidence: {', '.join(unexpected)}.",
        )


def audit_review_assignment(pr_number, expected_head=None):
    """Return the review-assignment audit record for ``pr_number``."""
    report = _base_report(pr_number, expected_head)
    if expected_head is not None and not _valid_head(expected_head):
        _add_mismatch(report, "expected_head_invalid", "--expected-head must be a full commit SHA.")

    pr = fetch_pr(pr_number)
    if not isinstance(pr, dict):
        _add_mismatch(report, "pr_unavailable", "Pull request data is unavailable.")
        return report
    report["pr"].update({
        "number": pr.get("number"),
        "state": pr.get("state"),
        "is_draft": pr.get("isDraft"),
    })
    if pr.get("number") != pr_number:
        _add_mismatch(report, "pr_number_mismatch", "Fetched PR number differs from the request.")

    assigned_service = _assignment_summary(pr, report)
    pr_head = pr.get("headRefOid")
    report["heads"]["pr"] = pr_head
    if not pr_head:
        _add_mismatch(report, "pr_head_missing", "PR snapshot has no head commit OID.")
    elif not _valid_head(pr_head):
        _add_mismatch(report, "pr_head_invalid", "PR snapshot head commit OID is malformed.")
    if expected_head is not None and _valid_head(expected_head) and pr_head != expected_head:
        _add_mismatch(report, "expected_head_mismatch", "PR head differs from --expected-head.")

    _check_summary(pr, assigned_service, report)
    evidence = review_evidence(pr_number)
    if not isinstance(evidence, dict):
        _add_mismatch(report, "review_evidence_unavailable", "Review evidence is unavailable.")
        return report
    evidence_head = evidence.get("head_oid")
    report["heads"]["review_evidence"] = evidence_head
    if not evidence_head:
        _add_mismatch(report, "evidence_head_missing", "Review evidence has no head commit OID.")
    elif not _valid_head(evidence_head):
        _add_mismatch(report, "evidence_head_invalid", "Review evidence head commit OID is malformed.")
    if pr_head != evidence_head:
        _add_mismatch(
            report,
            "evidence_head_mismatch",
            "PR snapshot and review evidence cover different heads.",
        )

    _review_summary(evidence, pr_head, assigned_service, report)
    _thread_summary(evidence, assigned_service, report)
    report["ok"] = not report["mismatches"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--expected-head", help="full commit SHA expected for the audit")
    args = parser.parse_args(argv)
    report = audit_review_assignment(args.pr, expected_head=args.expected_head)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
