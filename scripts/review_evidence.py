"""Authenticate and interpret external-review evidence for one PR head."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from common import KernelError, review_evidence_unavailable

AVAILABLE = "available"
PENDING = "pending"
UNAVAILABLE = "unavailable"
EXTERNAL_ACTORS = {
    "coderabbit": {"coderabbitai", "coderabbitai[bot]"},
    "sourcery": {"sourcery-ai", "sourcery-ai[bot]", "sourcery"},
    "codeant": {"codeant-ai", "codeant-ai[bot]"},
}
EXTERNAL_APP_SLUGS = {
    "coderabbit": {"coderabbitai"},
    "sourcery": {"sourcery-ai", "sourcery"},
    "codeant": {"codeant-ai", "codeant"},
}


def authority_assigned_at(
    pr: dict[str, Any],
    events: list[dict[str, Any]],
    authority: str,
    *,
    review_prefix: str = "review:",
) -> datetime:
    """Return the newest authenticated label-assignment boundary."""
    created_at = parse_time(str(pr.get("createdAt") or ""))
    assignments: list[datetime] = []
    expected_label = review_prefix + authority
    for event in events:
        if not isinstance(event, dict):
            raise KernelError("review assignment evidence is malformed")
        if event.get("event") != "labeled":
            continue
        label = event.get("label")
        if not isinstance(label, dict):
            raise KernelError("review assignment evidence is malformed")
        if label.get("name") == expected_label:
            assignments.append(evidence_time(event, subject="review assignment"))
    return max(assignments, default=created_at)


def parse_time(value: str, *, subject: str = "review assignment") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise KernelError(f"{subject} timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise KernelError(f"{subject} timestamp has no timezone")
    return parsed


def evidence_time(record: dict[str, Any], *, subject: str) -> datetime:
    keys = (
        "submitted_at", "submittedAt", "completed_at", "completedAt",
        "started_at", "startedAt", "updated_at", "updatedAt", "created_at",
        "createdAt",
    )
    for key in keys:
        if value := record.get(key):
            return parse_time(str(value), subject=subject)
    raise KernelError(f"{subject} timestamp is missing")


def check_service(record: dict[str, Any], service: str) -> bool:
    name = str(record.get("name") or record.get("context") or "")
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    aliases = {
        "coderabbit": ("coderabbit",),
        "sourcery": ("sourcery", "sourceryreview"),
        "codeant": ("codeant", "codeantai"),
    }[service]
    return normalized in aliases


def _trusted_actor(record: dict[str, Any], service: str) -> bool:
    actor = record.get("user") or record.get("author") or {}
    login = str(actor.get("login") or "").lower()
    actor_type = str(actor.get("type") or actor.get("__typename") or "")
    return login in EXTERNAL_ACTORS[service] and actor_type in {"", "Bot"}


def _trusted_check(record: dict[str, Any], service: str, head: str) -> bool:
    app = record.get("app")
    return bool(
        check_service(record, service)
        and isinstance(app, dict)
        and app.get("slug") in EXTERNAL_APP_SLUGS[service]
        and record.get("head_sha") == head
    )


def _latest_state(evidence: list[tuple[datetime, str]]) -> str:
    if not evidence:
        return PENDING
    latest_at = max(observed_at for observed_at, _state in evidence)
    states = {state for observed_at, state in evidence if observed_at == latest_at}
    if len(states) != 1:
        raise KernelError("external reviewer evidence conflicts at the latest timestamp")
    return states.pop()


def _record_state(record: dict[str, Any], *, head: str) -> str | None:
    if review_evidence_unavailable(record):
        return UNAVAILABLE
    if str(record.get("state") or "").upper() not in {
        "APPROVED", "CHANGES_REQUESTED",
    }:
        return None
    commit = record.get("commit_id") or record.get("commitId")
    return AVAILABLE if commit is None or commit == head else None


def _check_state(check: dict[str, Any]) -> str | None:
    conclusion = str(check.get("conclusion") or "").upper()
    status = str(check.get("status") or "").upper()
    if review_evidence_unavailable(check):
        return UNAVAILABLE
    if conclusion == "SUCCESS":
        return PENDING
    if conclusion in {"ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "SKIPPED"}:
        return UNAVAILABLE
    if conclusion in {"FAILURE", "NEUTRAL"} or status in {
        "QUEUED", "IN_PROGRESS", "PENDING", "WAITING",
    }:
        return PENDING
    return None


def external_state(
    service: str,
    *,
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    head: str,
    since: datetime,
) -> str:
    """Return the newest trusted provider state after the current assignment."""
    evidence: list[tuple[datetime, str]] = []
    for record in [*reviews, *comments]:
        if not isinstance(record, dict):
            raise KernelError("external reviewer evidence is malformed")
        if not _trusted_actor(record, service):
            continue
        state = _record_state(record, head=head)
        if state is None:
            continue
        observed_at = evidence_time(record, subject="external reviewer evidence")
        if observed_at < since:
            continue
        evidence.append((observed_at, state))

    matching = [record for record in checks if _trusted_check(record, service, head)]
    if len(matching) > 1:
        raise KernelError("external reviewer returned ambiguous checks")
    for check in matching:
        observed_at = evidence_time(check, subject="external reviewer check")
        if observed_at < since:
            continue
        if state := _check_state(check):
            evidence.append((observed_at, state))
    return _latest_state(evidence)


__all__ = [
    "AVAILABLE", "EXTERNAL_ACTORS", "EXTERNAL_APP_SLUGS", "PENDING",
    "UNAVAILABLE", "check_service",
    "authority_assigned_at", "evidence_time", "external_state", "parse_time",
]
