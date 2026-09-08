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


def _trusted_status(record: dict[str, Any], service: str) -> bool:
    """A commit status counts only when the provider's own App account created it.

    CodeRabbit reports through the commit Status API (context "CodeRabbit"),
    not through check runs. The context string is free text anyone with write
    access can post, so trust rests on the authenticated creator, exactly as it
    does for review objects.
    """
    creator = record.get("creator") or {}
    login = str(creator.get("login") or "").lower()
    creator_type = str(creator.get("type") or creator.get("__typename") or "")
    return (
        check_service(record, service)
        and login in EXTERNAL_ACTORS[service]
        and creator_type in {"", "Bot"}
    )


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
    if len(states) > 1:
        states.discard(PENDING)  # progress adds no verdict beside simultaneous substantive evidence
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
    return AVAILABLE if commit == head else None


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


def _status_state(status: dict[str, Any]) -> str | None:
    # Mirrors _check_state for the Status API: "pending" is progress, "success"
    # is a finished run but never a verdict (the review object carries that),
    # and denials, errors and skips are unavailability.
    if review_evidence_unavailable(status):
        return UNAVAILABLE
    state = str(status.get("state") or "").lower()
    if state == "pending":
        return PENDING
    if state == "success":
        return PENDING
    if state in {"error", "failure"}:
        return UNAVAILABLE
    return None


def _status_evidence(
    statuses: list[dict[str, Any]], service: str, since: datetime
) -> list[tuple[datetime, str]]:
    evidence: list[tuple[datetime, str]] = []
    for status in statuses:
        if not isinstance(status, dict):
            raise KernelError("external reviewer status evidence is malformed")
        if not _trusted_status(status, service):
            continue
        observed_at = evidence_time(status, subject="external reviewer status")
        if observed_at >= since and (state := _status_state(status)):
            evidence.append((observed_at, state))
    return evidence


def external_state(
    service: str,
    *,
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    head: str,
    since: datetime,
    statuses: list[dict[str, Any]] = (),
) -> str:
    """Return the newest trusted provider state after the current assignment."""
    evidence = _status_evidence(statuses, service, since)
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


LABEL_GATED_SKIP_RE = re.compile(r"review skipped.{0,40}label", re.IGNORECASE)
RUNNING_RE = re.compile(r"in progress|running|reviewing", re.IGNORECASE)
COMPLETED_RE = re.compile(r"review (?:completed|complete|finished)", re.IGNORECASE)
# signal -> (state while fresh, reason while fresh, reason once older than the deadline)
_FRESHNESS = {
    "running": (AVAILABLE, "current-head-review-running", "review-stalled"),
    "completed": (AVAILABLE, "current-head-review-completed", "completed-without-current-verdict"),
    "queued": (PENDING, "review-queued", "review-stalled"),
}


def _status_signal(record: dict[str, Any]) -> str:
    """Classify one trusted commit status (CodeRabbit's legacy `commit_status` mirror)."""
    description = str(record.get("description") or "")
    state = str(record.get("state") or "").lower()
    if LABEL_GATED_SKIP_RE.search(description):
        return "label-skip"
    if review_evidence_unavailable(record):
        return "denied"
    if state == "pending":
        return "running" if RUNNING_RE.search(description) else "queued"
    if state == "success":
        return "completed" if COMPLETED_RE.search(description) else "generic"
    return "error"


def _check_signal(record: dict[str, Any]) -> str:
    """Classify one trusted check run (CodeRabbit's default `review_progress` surface)."""
    output = record.get("output") if isinstance(record.get("output"), dict) else {}
    text = " ".join(str(v or "") for v in (record.get("name"), output.get("title"), output.get("summary")))
    if LABEL_GATED_SKIP_RE.search(text):
        return "label-skip"
    if review_evidence_unavailable(record):
        return "denied"
    status = str(record.get("status") or "").upper()
    conclusion = str(record.get("conclusion") or "").upper()
    if status in {"QUEUED", "PENDING", "WAITING", "REQUESTED"}:
        return "queued"
    if status == "IN_PROGRESS":
        return "running"
    if status == "COMPLETED" and conclusion == "SUCCESS":
        return "completed"
    if status == "COMPLETED" and conclusion == "NEUTRAL":
        return "generic"
    return "error"


def _capability_signals(
    statuses: list[dict[str, Any]], checks: list[dict[str, Any]], head: str | None
) -> list[tuple[datetime, str]]:
    signals: list[tuple[datetime, str]] = []
    for record in statuses:
        if not isinstance(record, dict):
            raise KernelError("commit status inventory is malformed")
        if _trusted_status(record, "coderabbit"):
            signals.append((evidence_time(record, subject="capability status"), _status_signal(record)))
    for record in checks:
        if not isinstance(record, dict):
            raise KernelError("capability check inventory is malformed")
        if head is not None and _trusted_check(record, "coderabbit", head):
            signals.append((evidence_time(record, subject="capability check"), _check_signal(record)))
    return signals


def coderabbit_capability_state(
    statuses: list[dict[str, Any]],
    checks: list[dict[str, Any]] = (),
    *,
    head: str | None = None,
    observed_at: datetime,
    since: datetime | None = None,
    timeout_seconds: int = 900,
) -> dict[str, str]:
    """Judge usable CodeRabbit access from its authenticated activity on one head.

    Both supported surfaces count: commit statuses created by `coderabbitai[bot]`
    and check runs owned by the CodeRabbit App for this head. The newest trusted
    signal decides. ``available``: a review is running or completed within the
    deadline. ``pending``: nothing observed yet, a queued run, a generic success,
    or the label-gated skip posted before assignment; CodeRabbit cannot run
    before `review:coderabbit` exists, so it stays eligible and the caller bounds
    the wait. ``unavailable``: denial, error, rate limit, a skip after
    assignment, a stalled or stale run, or a timestamp in the future. Untrusted
    creators and apps are ignored, so spoofed evidence changes nothing.
    """
    signals = _capability_signals(statuses, checks, head)
    if not signals:
        return {"state": PENDING, "reason": "no-coderabbit-evidence-yet"}
    seen_at = max(observed_at for observed_at, _signal in signals)
    newest = {signal for observed_at, signal in signals if observed_at == seen_at}
    if len(newest) != 1:
        # Two authentic signals in the same second that disagree (e.g. pending and
        # error) must not be settled by API response order: fail closed.
        return {"state": UNAVAILABLE, "reason": "conflicting-evidence"}
    signal = newest.pop()
    age = (observed_at - seen_at).total_seconds()
    if age < 0:
        return {"state": UNAVAILABLE, "reason": "future-timestamp"}
    if signal == "label-skip":
        if since is None or seen_at < since:
            return {"state": PENDING, "reason": "awaiting-review-label"}
        return {"state": UNAVAILABLE, "reason": "skipped-after-assignment"}
    if signal in {"denied", "error"}:
        return {"state": UNAVAILABLE, "reason": f"provider-{signal}"}
    if signal not in _FRESHNESS:
        return {"state": PENDING, "reason": "unrecognized-success"}
    fresh_state, fresh_reason, stale_reason = _FRESHNESS[signal]
    if age < timeout_seconds:
        return {"state": fresh_state, "reason": fresh_reason}
    return {"state": UNAVAILABLE, "reason": stale_reason}


__all__ = [
    "AVAILABLE", "EXTERNAL_ACTORS", "EXTERNAL_APP_SLUGS", "PENDING",
    "UNAVAILABLE", "check_service", "coderabbit_capability_state",
    "authority_assigned_at", "evidence_time", "external_state", "parse_time",
]
