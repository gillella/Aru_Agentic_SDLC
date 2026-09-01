#!/usr/bin/env python3
"""Validated repository review policy and read-only effective-status reporting."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

from common import (
    CODING_REVIEWERS,
    EXTERNAL_REVIEWERS,
    REVIEWER_CONFIG_ENV,
    REVIEW_AUTHORITIES,
    REVIEW_BINDING_PREFIX,
    REVIEW_REGISTRATION_PREFIX,
    CodingCandidate,
    KernelError,
    canonical_github_actor,
    configured_coding_reviewers,
    gh_json,
    probe_coding_candidate,
    registered_coding_actors,
    same_github_actor,
)

PRIMARY_PREFIX = "review-policy:primary="
FALLBACK_PREFIX = "review-policy:fallback-"
TIMEOUT_PREFIX = "review-policy:timeout="
POLICY_PREFIX = "review-policy:"
DEFAULT_TIMEOUT_SECONDS = 15 * 60
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 24 * 60 * 60
_FALLBACK_RE = re.compile(r"review-policy:fallback-([1-9][0-9]*)=(.+)")
ProbeRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ReviewPolicy:
    primary: str
    fallbacks: tuple[str, ...]
    timeout_seconds: int
    sources: dict[str, str]

    @property
    def authorities(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)

    def as_dict(self) -> dict[str, object]:
        return {
            "primary": self.primary,
            "fallbacks": list(self.fallbacks),
            "timeout_seconds": self.timeout_seconds,
            "sources": dict(self.sources),
        }

    def after(self, authority: str) -> tuple[str, ...]:
        try:
            index = self.authorities.index(authority)
        except ValueError as exc:
            raise KernelError(
                "assigned authority is absent from the effective review policy"
            ) from exc
        return self.authorities[index + 1 :]


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
        for service in EXTERNAL_REVIEWERS
        if REVIEW_REGISTRATION_PREFIX + service in inventory
    )


def registration_states(names: Iterable[str]) -> dict[str, str]:
    registered = set(registered_external_reviewers(names))
    return {
        service: "available" if service in registered else "unavailable"
        for service in EXTERNAL_REVIEWERS
    }


def default_review_policy(registered: Iterable[str]) -> ReviewPolicy:
    registered = set(registered)
    installed = tuple(service for service in EXTERNAL_REVIEWERS if service in registered)
    primary = installed[0] if installed else CODING_REVIEWERS[0]
    fallbacks = tuple(family for family in CODING_REVIEWERS if family != primary)
    if primary in CODING_REVIEWERS and installed:
        fallbacks = (*fallbacks, installed[0])
    return ReviewPolicy(
        primary=primary,
        fallbacks=fallbacks,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        sources={
            "primary": "kernel-default",
            "fallbacks": "kernel-default",
            "timeout_seconds": "kernel-default",
        },
    )


def _one_value(names: Iterable[str], prefix: str, subject: str) -> str | None:
    values = [name[len(prefix) :] for name in names if name.startswith(prefix)]
    if len(values) > 1:
        raise KernelError(f"review policy {subject} is ambiguous")
    return values[0] if values else None


def _configured_fallbacks(names: Iterable[str]) -> tuple[str, ...] | None:
    ranks: dict[int, str] = {}
    found = False
    for name in names:
        if not name.startswith(FALLBACK_PREFIX):
            continue
        found = True
        match = _FALLBACK_RE.fullmatch(name)
        if match is None:
            raise KernelError("review policy fallback declaration is malformed")
        rank = int(match.group(1))
        if rank in ranks:
            raise KernelError("review policy fallback rank is ambiguous")
        ranks[rank] = match.group(2)
    if not found:
        return None
    expected = list(range(1, len(ranks) + 1))
    if sorted(ranks) != expected:
        raise KernelError("review policy fallback ranks must be contiguous from 1")
    return tuple(ranks[rank] for rank in expected)


def review_policy_from_labels(names: Iterable[str]) -> ReviewPolicy:
    names = tuple(names)
    unknown = [
        name
        for name in names
        if name.startswith(POLICY_PREFIX)
        and not (
            name.startswith(PRIMARY_PREFIX)
            or name.startswith(FALLBACK_PREFIX)
            or name.startswith(TIMEOUT_PREFIX)
        )
    ]
    if unknown:
        raise KernelError("review policy contains unsupported declarations")
    registered = registered_external_reviewers(names)
    default = default_review_policy(registered)
    primary_value = _one_value(names, PRIMARY_PREFIX, "primary")
    fallback_values = _configured_fallbacks(names)
    timeout_value = _one_value(names, TIMEOUT_PREFIX, "timeout")
    primary = primary_value or default.primary
    fallbacks = fallback_values if fallback_values is not None else default.fallbacks
    timeout = default.timeout_seconds
    if timeout_value is not None:
        if not timeout_value.isdigit():
            raise KernelError("review policy timeout must be an integer number of seconds")
        timeout = int(timeout_value)
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise KernelError(
                f"review policy timeout must be {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} seconds"
            )
    authorities = (primary, *fallbacks)
    if any(authority not in REVIEW_AUTHORITIES for authority in authorities):
        raise KernelError("review policy contains an unsupported authority")
    if len(authorities) != len(set(authorities)):
        raise KernelError("review policy contains duplicate authorities")
    registered_set = set(registered)
    missing_external = [
        authority
        for authority in authorities
        if authority in EXTERNAL_REVIEWERS and authority not in registered_set
    ]
    if missing_external:
        raise KernelError(
            "review policy references unregistered external authority: "
            + ", ".join(missing_external)
        )
    return ReviewPolicy(
        primary=primary,
        fallbacks=fallbacks,
        timeout_seconds=timeout,
        sources={
            "primary": "repository-label" if primary_value is not None else "kernel-default",
            "fallbacks": (
                "repository-labels" if fallback_values is not None else "kernel-default"
            ),
            "timeout_seconds": (
                "repository-label" if timeout_value is not None else "kernel-default"
            ),
        },
    )


def load_repository_review_policy() -> tuple[ReviewPolicy, tuple[str, ...]]:
    names = repository_label_names()
    return review_policy_from_labels(names), names


def _default_probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


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
    reviewer_actors: dict[str, str] | None = None,
    runner: ProbeRunner = _default_probe,
) -> tuple[str, str, str] | None:
    order = ordered_coding_families(family_order, author_family)
    if not order:
        return None
    configured = configured_coding_reviewers()
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    author_actor = canonical_github_actor(author_actor)
    for family in order:
        available: list[CodingCandidate] = []
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "").lower()
            candidate: CodingCandidate = (family, identity, actor, subscription)
            probe_ok = probe_coding_candidate(candidate, runner)
            if (
                probe_ok
                and identity != author_identity
                and actor
                and not same_github_actor(actor, author_actor)
            ):
                available.append(candidate)
        if available:
            candidate = available[rotation_key % len(available)]
            return candidate[0], candidate[1], candidate[2]
    return None


def select_reviewer_from_order(
    authorities: Iterable[str],
    *,
    number: int,
    author_identity: str,
    author_family: str,
    author_actor: str,
    external_states: dict[str, str],
    reviewer_actors: dict[str, str] | None,
    probe_runner: ProbeRunner,
    coding_probe: Callable[..., tuple[str, str, str] | None] = probe_coding_reviewer,
) -> tuple[str, str | None, str | None] | None:
    authorities = tuple(authorities)
    for authority in authority_order_for_author(authorities, author_family):
        if authority in EXTERNAL_REVIEWERS:
            if external_states.get(authority) in {"available", "pending"}:
                return authority, None, None
            continue
        if not author_actor:
            record = gh_json(["api", "user"])
            author_actor = str(record.get("login") or "") if isinstance(record, dict) else ""
            if not author_actor:
                raise KernelError("current GitHub author identity is unavailable")
        coding = coding_probe(
            author_identity=author_identity,
            author_family=author_family,
            author_actor=author_actor,
            rotation_key=number,
            family_order=(authority,),
            reviewer_actors=reviewer_actors,
            runner=probe_runner,
        )
        if coding is not None:
            return coding
    return None


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
    try:
        configured = configured_coding_reviewers()
    except KernelError as exc:
        configured = {}
        errors.append(str(exc))
    try:
        actors = registered_coding_actors()
    except KernelError as exc:
        actors = {}
        errors.append(str(exc))
    normalized_author_actor = canonical_github_actor(author_actor)
    coding: list[dict[str, object]] = []
    for family in CODING_REVIEWERS:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "")
            eligible = bool(
                actor
                and identity != author_identity
                and not same_github_actor(actor, normalized_author_actor)
            )
            probe_state = "not-requested"
            if probe and eligible:
                candidate: CodingCandidate = (family, identity, actor, subscription)
                probe_state = "ok" if probe_coding_candidate(candidate, runner) else "failed"
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
    configured_families = {str(item["family"]) for item in coding}
    required_families = set(policy.authorities).intersection(CODING_REVIEWERS)
    for family in sorted(required_families - configured_families):
        errors.append(f"policy coding authority {family} has no local configured identity")
    unused_external = set(registered) - set(policy.authorities)
    for service in sorted(unused_external):
        warnings.append(f"registered external reviewer {service} is not used by the policy")
    return {
        "schema": "aru.reviewer-status/v1",
        "valid": not errors,
        "policy": policy.as_dict(),
        "external_reviewers": [
            {
                "service": service,
                "registered": service in registered,
                "policy_role": (
                    "primary"
                    if service == policy.primary
                    else "fallback"
                    if service in policy.fallbacks
                    else "unused"
                ),
                "availability": "observed-on-pr" if service in registered else "unregistered",
            }
            for service in EXTERNAL_REVIEWERS
        ],
        "coding_reviewers": coding,
        "exclusions": {
            "author_identity": author_identity or None,
            "author_actor": normalized_author_actor or None,
        },
        "configuration_sources": {
            "repository_policy": "GitHub review-policy:* label definitions",
            "external_registration": "GitHub reviewer-registered:* label definitions",
            "coding_inventory": REVIEWER_CONFIG_ENV,
            "reviewer_bindings": f"GitHub {REVIEW_BINDING_PREFIX}* label definitions",
            "runtime_availability": "bounded on-demand probe" if probe else "not probed",
        },
        "errors": errors,
        "warnings": warnings,
    }
