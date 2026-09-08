#!/usr/bin/env python3
"""Validated repository review policy and read-only effective-status reporting."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from common import (
    ACTIVE_EXTERNAL_REVIEWERS,
    RETIRED_EXTERNAL_REVIEWERS,
    CODING_REVIEWERS,
    EXTERNAL_REVIEWERS,
    REVIEWER_CONFIG_ENV,
    REVIEW_BINDING_PREFIX,
    REVIEW_REGISTRATION_PREFIX,
    KernelError,
    canonical_github_actor,
    configured_coding_reviewers,
    gh_json,
    git,
    gh_paginated,
    label_names,
    normalized_identity,
    registered_coding_actors,
    repo_slug,
    same_github_actor,
)
from review_evidence import (
    AVAILABLE,
    PENDING,
    UNAVAILABLE,
    authority_assigned_at,
    coderabbit_capability_state,
    external_state,
)
from reviewer_probe import (
    CodingCandidate,
    ProbeRunner,
    _default_probe,
    available_coding_reviewers,
)

TIMEOUT_PREFIX = "review-policy:timeout="
POLICY_PREFIX = "review-policy:"
DEFAULT_TIMEOUT_SECONDS = 15 * 60
CAPABILITY_TIMEOUT_SECONDS = 8
# After assignment CodeRabbit posts queued/in-progress within seconds; no authentic
# activity inside this window means the assignment is unproven and falls back.
ACTIVITY_GRACE_SECONDS = 120
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
ASSIGNMENT_AUDIT_PREFIXES = (
    "## Aru authoritative reviewer fallback",
    "## Aru authoritative reviewer recovery",
)
ASSIGNMENT_MARKER = re.compile(
    r"(?m)^<!-- aru-review-assignment:v1 (\{[^\n]+\}) -->$"
)


@dataclass(frozen=True)
class ReviewPolicy:
    external_reviewers: tuple[str, ...]
    coding_fallbacks: tuple[str, ...]
    timeout_seconds: int
    sources: dict[str, str]

    @property
    def authorities(self) -> tuple[str, ...]:
        return (*self.external_reviewers, *self.coding_fallbacks)

    def as_dict(self) -> dict[str, object]:
        return {
            "selection": "coderabbit-first",
            "external_reviewers": list(self.external_reviewers),
            "coding_fallbacks": list(self.coding_fallbacks),
            "timeout_seconds": self.timeout_seconds,
            "sources": dict(self.sources),
        }


def repository_label_names() -> tuple[str, ...]:
    records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("reviewer configuration labels are unavailable")
    names = [str(item.get("name") or "") for item in records]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise KernelError("reviewer configuration label inventory is malformed")
    return tuple(names)


def registered_external_reviewers(names: Iterable[str]) -> tuple[str, ...]:
    inventory = set(names)
    return tuple(
        service
        for service in ACTIVE_EXTERNAL_REVIEWERS
        if REVIEW_REGISTRATION_PREFIX + service in inventory
    )


def registration_states(names: Iterable[str]) -> dict[str, str]:
    registered = set(registered_external_reviewers(names))
    return {
        service: "available" if service in registered else "unavailable"
        for service in EXTERNAL_REVIEWERS
    }


def _statuses(pages: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    """Flatten paginated commit-status pages down to status-shaped records.

    Anything that is not a list is a transport or API failure and is refused. In
    strict mode (the bounded capability probe) a non-empty payload with no
    status-shaped record is also refused, so an unexpected API shape reads as
    unreadable rather than as "no activity yet".
    """
    if isinstance(pages, list) and all(isinstance(page, list) for page in pages):
        pages = [record for page in pages for record in page]
    if not isinstance(pages, list):
        raise KernelError("commit status inventory is malformed")
    statuses = [
        record for record in pages
        if isinstance(record, dict) and "context" in record and "state" in record
    ]
    if strict and pages and not statuses:
        raise KernelError("commit status inventory is malformed")
    return statuses


def _check_runs(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
        raise KernelError("capability check inventory is malformed")
    checks = data["check_runs"]
    if data.get("total_count") != len(checks) or any(not isinstance(c, dict) for c in checks):
        raise KernelError("capability check inventory is incomplete")
    return checks


def coderabbit_capability(head: str | None = None) -> dict[str, str]:
    """Bounded observation of CodeRabbit's authenticated activity on one head.

    Reads both supported surfaces: legacy commit statuses (`commit_status`) and
    App check runs (`review_progress`). Registration, cached inventory and a
    generic green rollup prove nothing. An unreadable or timed-out read is
    reported as unavailable so the caller falls back instead of guessing.
    """
    try:
        head = head or git(["rev-parse", "HEAD"])
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise KernelError("invalid capability head")
        slug = repo_slug()
        statuses = _statuses(gh_json([
            "api", f"repos/{slug}/commits/{head}/statuses?per_page=100",
        ], timeout=CAPABILITY_TIMEOUT_SECONDS), strict=True)
        checks = _check_runs(gh_json([
            "api", f"repos/{slug}/commits/{head}/check-runs?per_page=100&filter=latest",
        ], timeout=CAPABILITY_TIMEOUT_SECONDS))
        return coderabbit_capability_state(
            statuses, checks, head=head, observed_at=datetime.now(timezone.utc),
        )
    except KernelError:
        return {"state": UNAVAILABLE, "reason": "capability-unreadable-or-timed-out"}


def registered_external_states(
    names: tuple[str, ...] | None = None, *, head: str | None = None
) -> dict[str, str]:
    names = repository_label_names() if names is None else names
    states = {service: UNAVAILABLE for service in EXTERNAL_REVIEWERS}
    if "coderabbit" in registered_external_reviewers(names):
        states["coderabbit"] = coderabbit_capability(head)["state"]
    return states


def external_decision(
    number: int,
    pr: dict[str, object],
    authority: str,
    observed_at: datetime,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, int | None]:
    if authority in RETIRED_EXTERNAL_REVIEWERS:
        return "external-retired", None
    slug = repo_slug()
    reviews = gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    comments = gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")
    events = gh_paginated(f"repos/{slug}/issues/{number}/events?per_page=100")
    pages = gh_json([
        "api", "--paginate", "--slurp",
        f"repos/{slug}/commits/{pr['headRefOid']}/check-runs?per_page=100&filter=latest",
    ])
    if (
        not isinstance(pages, list)
        or any(not isinstance(page, dict) for page in pages)
        or any(not isinstance(page.get("check_runs"), list) for page in pages)
    ):
        raise KernelError("external review check-run inventory is malformed")
    checks = [record for page in pages for record in page["check_runs"]]
    if any(not isinstance(record, dict) for record in checks):
        raise KernelError("external review check-run inventory is malformed")
    totals = {page.get("total_count") for page in pages}
    if len(totals) != 1 or totals.pop() != len(checks):
        raise KernelError("external review check-run inventory is incomplete")
    statuses = _statuses(gh_json([
        "api", "--paginate", "--slurp",
        f"repos/{slug}/commits/{pr['headRefOid']}/statuses?per_page=100",
    ]))
    assigned_at = authority_assigned_at(pr, events, authority)
    state = external_state(
        authority,
        reviews=reviews,
        comments=comments,
        checks=checks,
        head=str(pr["headRefOid"]),
        since=assigned_at,
        statuses=statuses,
    )
    age = (observed_at - assigned_at).total_seconds()
    if age < 0:
        raise KernelError("review observation predates assignment")
    if state == AVAILABLE:
        return "external-available", None
    if state == PENDING and age < timeout_seconds:
        # Inside the completion deadline. A running or completed review keeps the
        # full deadline; denial, error, post-assignment skip or a stale run ends it
        # now; no activity (or only "queued") is tolerated for one bounded window
        # after assignment, then the unproven assignment falls back.
        capability = coderabbit_capability_state(
            statuses, checks, head=str(pr["headRefOid"]), observed_at=observed_at,
            since=assigned_at, timeout_seconds=timeout_seconds,
        )
        if capability["state"] == UNAVAILABLE:
            return "external-unavailable", None
        if capability["state"] == AVAILABLE:
            return "external-pending", timeout_seconds - int(age)
        if age >= ACTIVITY_GRACE_SECONDS:
            return "external-unavailable", None
        return "external-pending", min(timeout_seconds, ACTIVITY_GRACE_SECONDS) - int(age)
    reason = "external-unavailable" if state == UNAVAILABLE else "external-pending-timeout"
    return reason, None


def default_review_policy(registered: Iterable[str]) -> ReviewPolicy:
    registered = set(registered)
    installed = tuple(service for service in ACTIVE_EXTERNAL_REVIEWERS if service in registered)
    return ReviewPolicy(
        external_reviewers=installed,
        coding_fallbacks=CODING_REVIEWERS,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        sources={
            "external_reviewers": "registration-labels",
            "coding_fallbacks": REVIEWER_CONFIG_ENV,
            "timeout_seconds": "kernel-default",
        },
    )


def _one_value(names: Iterable[str], prefix: str, subject: str) -> str | None:
    values = [name[len(prefix) :] for name in names if name.startswith(prefix)]
    if len(values) > 1:
        raise KernelError(f"review policy {subject} is ambiguous")
    return values[0] if values else None


def review_policy_from_labels(names: Iterable[str]) -> ReviewPolicy:
    names = tuple(names)
    unknown = [
        name
        for name in names
        if name.startswith(POLICY_PREFIX)
        and not name.startswith(TIMEOUT_PREFIX)
    ]
    if unknown:
        raise KernelError("review policy contains unsupported declarations")
    registered = registered_external_reviewers(names)
    default = default_review_policy(registered)
    timeout_value = _one_value(names, TIMEOUT_PREFIX, "timeout")
    timeout = default.timeout_seconds
    if timeout_value is not None:
        if not timeout_value.isdigit():
            raise KernelError("review policy timeout must be an integer number of seconds")
        timeout = int(timeout_value)
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise KernelError(
                f"review policy timeout must be {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} seconds"
            )
    return ReviewPolicy(
        external_reviewers=default.external_reviewers,
        coding_fallbacks=default.coding_fallbacks,
        timeout_seconds=timeout,
        sources={
            "external_reviewers": "registration-labels",
            "coding_fallbacks": REVIEWER_CONFIG_ENV,
            "timeout_seconds": (
                "repository-label" if timeout_value is not None else "kernel-default"
            ),
        },
    )


def load_repository_review_policy() -> tuple[ReviewPolicy, tuple[str, ...]]:
    names = repository_label_names()
    return review_policy_from_labels(names), names


def reviewer_continuation(
    number: int, *, now: datetime | None = None
) -> dict[str, object]:
    pr = gh_json(
        [
            "pr", "view", str(number), "--json",
            "number,url,createdAt,headRefOid,labels,author,statusCheckRollup",
        ]
    )
    if not isinstance(pr, dict) or pr.get("number") != number:
        raise KernelError(f"pull request #{number} is unavailable")
    authorities = [
        name.removeprefix("review:")
        for name in label_names(pr)
        if name.startswith("review:")
        and name.removeprefix("review:") in (*EXTERNAL_REVIEWERS, *CODING_REVIEWERS)
    ]
    if len(authorities) > 1:
        raise KernelError("pull request has multiple authoritative reviewers")
    authority = authorities[0] if authorities else None
    observed_at = now or datetime.now(timezone.utc)
    if authority is None:
        return {"authority": None, "next_action": "refresh-reviewer", "retry_at": observed_at.isoformat()}
    if authority in CODING_REVIEWERS:
        return {"authority": authority, "next_action": "await-authoritative-review", "retry_at": None}
    policy = load_repository_review_policy()[0]
    reason, remaining = external_decision(number, pr, authority, observed_at, policy.timeout_seconds)
    if reason == "external-pending" and remaining is not None:
        return {
            "authority": authority,
            "next_action": "refresh-reviewer",
            "retry_at": (observed_at + timedelta(seconds=remaining)).isoformat(),
        }
    if reason == "external-available":
        return {"authority": authority, "next_action": "await-authoritative-review", "retry_at": None}
    return {"authority": authority, "next_action": "refresh-reviewer", "retry_at": observed_at.isoformat()}


def effective_review_policy(
    policy: ReviewPolicy | None, external_states: dict[str, str] | None
) -> ReviewPolicy:
    if policy is not None:
        return policy
    if external_states is not None:
        registered = [
            service
            for service, state in external_states.items()
            if state in {AVAILABLE, PENDING}
        ]
        return default_review_policy(registered)
    return load_repository_review_policy()[0]


def ordered_coding_families(
    authorities: Iterable[str], author_family: str
) -> tuple[str, ...]:
    families = [value for value in authorities if value in CODING_REVIEWERS]
    if author_family in families:
        families.remove(author_family)
        families.append(author_family)
    return tuple(families)


def authority_order_for_author(
    authorities: Iterable[str], author_family: str
) -> tuple[str, ...]:
    authorities = tuple(authorities)
    families = iter(ordered_coding_families(authorities, author_family))
    return tuple(
        next(families) if authority in CODING_REVIEWERS else authority
        for authority in authorities
    )


def probe_coding_reviewer(
    *,
    author_identity: str,
    author_family: str,
    author_actor: str = "",
    rotation_key: int,
    family_order: Iterable[str] = CODING_REVIEWERS,
    excluded_identities: Iterable[str] = (),
    reviewer_actors: dict[str, str] | None = None,
    runner: ProbeRunner = _default_probe,
) -> tuple[str, str, str] | None:
    order = ordered_coding_families(family_order, author_family)
    if not order:
        return None
    configured = configured_coding_reviewers()
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    author_identity = normalized_identity(author_identity)
    author_actor = canonical_github_actor(author_actor)
    excluded = {normalized_identity(identity) for identity in excluded_identities}
    candidates: list[CodingCandidate] = []
    for family in order:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "").lower()
            candidate: CodingCandidate = (family, identity, actor, subscription)
            if (
                identity != author_identity
                and identity not in excluded
                and actor
                and not same_github_actor(actor, author_actor)
            ):
                candidates.append(candidate)
    available = available_coding_reviewers(candidates, runner=runner)
    for family in order:
        matching = [candidate for candidate in available if candidate[0] == family]
        if matching:
            return matching[rotation_key % len(matching)]
    return None


def reviewer_candidate_key(authority: str, identity: str | None = None) -> str:
    if authority in EXTERNAL_REVIEWERS and identity is None:
        return f"external:{authority}"
    if authority in CODING_REVIEWERS and identity:
        return f"coding:{normalized_identity(identity)}"
    raise KernelError("reviewer candidate is malformed")


def _previous_audit_candidate_key(payload: dict[str, Any]) -> str | None:
    authority = payload.get("previous_authority")
    identity = payload.get("previous_reviewer")
    if authority in EXTERNAL_REVIEWERS and identity is None:
        return reviewer_candidate_key(str(authority))
    if authority in CODING_REVIEWERS and isinstance(identity, str) and identity.strip():
        return reviewer_candidate_key(str(authority), identity)
    return None


def attempted_reviewer_keys(
    number: int,
    *,
    head: str,
    authority: str,
    identity: str | None,
) -> set[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise KernelError("review assignment head is malformed")
    attempted = {reviewer_candidate_key(authority, identity)}
    comments = gh_paginated(f"repos/{repo_slug()}/issues/{number}/comments?per_page=100")
    for comment in comments:
        body = str(comment.get("body") or "")
        if not body.startswith(ASSIGNMENT_AUDIT_PREFIXES):
            continue
        matches = ASSIGNMENT_MARKER.findall(body)
        if len(matches) != 1:
            raise KernelError("review assignment audit marker is malformed or ambiguous")
        try:
            payload = json.loads(matches[0])
        except json.JSONDecodeError as exc:
            raise KernelError("review assignment audit marker is invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("head") != head:
            continue
        if key := _previous_audit_candidate_key(payload):
            attempted.add(key)
    return attempted


def select_reviewer_from_pool(
    policy: ReviewPolicy,
    *,
    number: int,
    author_identity: str,
    author_family: str,
    author_actor: str,
    external_states: dict[str, str],
    reviewer_actors: dict[str, str] | None,
    probe_runner: ProbeRunner,
    excluded: Iterable[str] = (),
    coding_probe: Callable[..., tuple[str, str, str] | None] = probe_coding_reviewer,
) -> tuple[str, str | None, str | None] | None:
    excluded = set(excluded)
    external_pool = [
        (service, None, None)
        for service in policy.external_reviewers
        if service in ACTIVE_EXTERNAL_REVIEWERS
        # "pending" means registered with no denial observed yet; CodeRabbit
        # cannot run before the review label exists, so it stays eligible.
        and external_states.get(service) in {AVAILABLE, PENDING}
        and reviewer_candidate_key(service) not in excluded
    ]
    if external_pool:
        return external_pool[0]
    if not os.environ.get(REVIEWER_CONFIG_ENV, "").strip():
        return None
    if not author_actor:
        record = gh_json(["api", "user"])
        author_actor = str(record.get("login") or "") if isinstance(record, dict) else ""
        if not author_actor:
            raise KernelError("current GitHub author identity is unavailable")
    return coding_probe(
        author_identity=author_identity,
        author_family=author_family,
        author_actor=author_actor,
        rotation_key=number,
        family_order=policy.coding_fallbacks,
        excluded_identities=(
            key.removeprefix("coding:")
            for key in excluded
            if key.startswith("coding:")
        ),
        reviewer_actors=reviewer_actors,
        runner=probe_runner,
    )


def reviewer_status(
    *,
    probe: bool = False,
    author_identity: str = "",
    author_actor: str = "",
    runner: ProbeRunner = _default_probe,
) -> dict[str, object]:
    policy, names = load_repository_review_policy()
    registered = registered_external_reviewers(names)
    errors: list[str] = []
    warnings: list[str] = []
    required_families = set(policy.coding_fallbacks)
    capability = coderabbit_capability() if probe and "coderabbit" in registered else {"state": "unprobed", "reason": "not-requested"}
    configured = {}
    if os.environ.get(REVIEWER_CONFIG_ENV, "").strip():
        try:
            configured = configured_coding_reviewers()
        except KernelError as exc:
            errors.append(str(exc))
    elif required_families:
        errors.append(f"{REVIEWER_CONFIG_ENV} is missing")
    try:
        actors = registered_coding_actors()
    except KernelError as exc:
        actors = {}
        errors.append(str(exc))
    normalized_author_identity = (
        normalized_identity(author_identity) if author_identity.strip() else ""
    )
    normalized_author_actor = canonical_github_actor(author_actor)
    coding: list[dict[str, object]] = []
    for family in CODING_REVIEWERS:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "")
            eligible = bool(
                actor
                and identity != normalized_author_identity
                and not same_github_actor(actor, normalized_author_actor)
            )
            probe_state = "not-requested"
            if probe and eligible:
                candidate: CodingCandidate = (family, identity, actor, subscription)
                available = available_coding_reviewers([candidate], runner=runner)
                probe_state = "ok" if available else "failed"
            elif probe:
                probe_state = "ineligible"
            if not actor:
                errors.append(f"configured coding identity {identity} has no reviewer binding")
            coding.append(
                {
                    "family": family,
                    "identity": identity,
                    "subscription": subscription,
                    "reviewer_actor": actor or None,
                    "eligible": eligible,
                    "probe": probe_state,
                }
            )
    if required_families and not any(item["eligible"] for item in coding):
        errors.append("no eligible independent coding fallback is configured")
    return {
        "schema": "aru.reviewer-status/v3",
        "valid": not errors,
        "policy": policy.as_dict(),
        "coderabbit_capability": capability,
        "review_evidence": {"assignment": "not-inspected", "execution": "not-inspected", "current_head_verdict": "not-inspected"},
        "external_reviewers": [
            {
                "service": service,
                "registered": REVIEW_REGISTRATION_PREFIX + service in names,
                "policy_role": ("retired" if service in RETIRED_EXTERNAL_REVIEWERS else "preferred" if service in registered else "unregistered"),
                "availability": "retired" if service in RETIRED_EXTERNAL_REVIEWERS else "unprobed" if service in registered else "unregistered",
            }
            for service in EXTERNAL_REVIEWERS
        ],
        "coding_reviewers": coding,
        "exclusions": {
            "author_identity": author_identity or None,
            "author_actor": normalized_author_actor or None,
        },
        "configuration_sources": {
            "repository_policy": "CodeRabbit-first plus optional GitHub review-policy:timeout label",
            "external_registration": "GitHub reviewer-registered:coderabbit; Sourcery and CodeAnt retired",
            "coding_inventory": REVIEWER_CONFIG_ENV,
            "reviewer_bindings": f"GitHub {REVIEW_BINDING_PREFIX}* label definitions",
            "runtime_availability": "bounded on-demand probe" if probe else "not probed",
        },
        "errors": errors,
        "warnings": warnings,
    }
