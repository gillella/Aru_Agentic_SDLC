#!/usr/bin/env python3
"""Open a PR and govern its single authoritative reviewer assignment."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import (
    AGENT_PREFIX,
    AUTHOR_FAMILY_PREFIX,
    AUTHOR_PREFIX,
    CODING_REVIEWERS,
    EXTERNAL_REVIEWERS,
    REVIEW_BINDING_PREFIX,
    REVIEW_REGISTRATION_PREFIX,
    REVIEWER_ACTOR_PREFIX,
    REVIEWER_PREFIX,
    REVIEW_AUTHORITIES,
    REVIEW_PREFIX,
    KernelError,
    ensure_label,
    gh_json,
    gh_paginated,
    git,
    issue,
    json_print,
    label_names,
    repo_slug,
    run,
    set_status,
    status_of,
)

AVAILABLE = "available"
PENDING = "pending"
UNAVAILABLE = "unavailable"
EXTERNAL_TIMEOUT_SECONDS = 15 * 60
PROBE_PROMPT = "Reply exactly OK"
EXTERNAL_ACTORS = {
    "coderabbit": {"coderabbitai", "coderabbitai[bot]"},
    "sourcery": {"sourcery-ai", "sourcery-ai[bot]", "sourcery"},
    "codeant": {"codeant-ai", "codeant-ai[bot]"},
}
UNAVAILABLE_RE = re.compile(
    r"(?:^\s*(?:error|unavailable)\b|\b(?:quota exhausted|quota exceeded|"
    r"rate[ -]?limit(?:ed|ing)?|provider outage|service outage|"
    r"unsupported bot(?:-authored)? pr|cannot review|unable to review|"
    r"payment required|insufficient credits?|capacity exhausted|"
    r"cost (?:limit|quota|cap) (?:reached|exceeded))\b)",
    re.IGNORECASE,
)
ProbeRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def normalized_identity(value: str) -> str:
    identity = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not identity:
        raise KernelError("review identity is empty")
    return identity[:80]


def agent_family(identity: str) -> str:
    value = normalized_identity(identity)
    aliases = {
        "claude-code": ("claude",),
        "openai-codex": ("codex", "openai"),
        "xai-cursor": ("cursor", "xai"),
        "google-antigravity": ("antigravity", "google", "agy"),
    }
    for family, needles in aliases.items():
        if any(needle in value for needle in needles):
            return family
    return "human-or-other"


def current_branch() -> str:
    branch = git(["branch", "--show-current"])
    if not branch or branch in {"main", "master"}:
        raise KernelError("pull requests must be opened from a feature branch")
    return branch


def current_agent(record: dict[str, Any]) -> str:
    values = [
        name[len(AGENT_PREFIX) :]
        for name in label_names(record)
        if name.startswith(AGENT_PREFIX)
    ]
    if len(values) != 1:
        raise KernelError("issue must have exactly one claimant")
    return values[0]


def require_published_head(branch: str) -> str:
    head = git(["rev-parse", "HEAD"])
    result = run(
        ["git", "-c", "core.fsmonitor=false", "ls-remote", "--heads", "origin", branch],
        check=False,
    )
    fields = result.stdout.strip().split()
    if result.returncode or len(fields) != 2 or fields[0] != head:
        raise KernelError("push the exact current head before creating the PR")
    return head


def registered_external_states() -> dict[str, str]:
    records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("reviewer registration labels are unavailable")
    names = {str(item.get("name") or "") for item in records}
    return {
        service: AVAILABLE
        if REVIEW_REGISTRATION_PREFIX + service in names
        else UNAVAILABLE
        for service in EXTERNAL_REVIEWERS
    }


def registered_coding_actors() -> dict[str, str]:
    records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("coding reviewer identity bindings are unavailable")
    bindings: dict[str, str] = {}
    for item in records:
        name = str(item.get("name") or "")
        if not name.startswith(REVIEW_BINDING_PREFIX):
            continue
        values = name[len(REVIEW_BINDING_PREFIX) :].split("=", 1)
        if len(values) != 2:
            raise KernelError("coding reviewer identity binding is malformed")
        identity, actor = values[0].lower(), values[1].lower()
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identity)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?", actor)
            or identity in bindings
        ):
            raise KernelError("coding reviewer identity binding is malformed or ambiguous")
        bindings[identity] = actor
    return bindings


def initial_external(states: dict[str, str]) -> str | None:
    if any(state not in {AVAILABLE, PENDING, UNAVAILABLE} for state in states.values()):
        raise KernelError("external reviewer availability is malformed")
    for service in EXTERNAL_REVIEWERS:
        if states.get(service) == AVAILABLE:
            return service
    for service in EXTERNAL_REVIEWERS:
        if states.get(service) == PENDING:
            return service
    return None


def current_github_actor() -> str:
    record = gh_json(["api", "user"])
    login = str(record.get("login") or "").lower() if isinstance(record, dict) else ""
    if not login:
        raise KernelError("current GitHub author identity is unavailable")
    return login


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


def _probe_ok(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 and result.stdout.strip() == "OK"


def _command(name: str) -> str | None:
    return shutil.which(name)


def probe_coding_reviewer(
    *,
    author_identity: str,
    author_family: str,
    author_actor: str = "",
    rotation_key: int,
    reviewer_actors: dict[str, str] | None = None,
    runner: ProbeRunner = _default_probe,
) -> tuple[str, str, str] | None:
    author_identity = normalized_identity(author_identity)
    author_actor = author_actor.lower()
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    family_order = [family for family in CODING_REVIEWERS if family != author_family]
    if author_family in CODING_REVIEWERS:
        family_order.append(author_family)

    for family in family_order:
        if family == "claude-code":
            executable = str(Path.home() / ".local" / "bin" / "claude-sub")
            identities: list[str] = []
            for subscription in (1, 2, 3):
                result = runner([executable, str(subscription), "-p", PROBE_PROMPT])
                if _probe_ok(result):
                    identities.append(f"claude-code-sub-{subscription}")
            identities = [value for value in identities if value != author_identity]
            identities = [
                value
                for value in identities
                if value in actors and actors[value].lower() != author_actor
            ]
            if identities:
                identity = identities[rotation_key % len(identities)]
                return family, identity, actors[identity].lower()
            continue

        command_names = {
            "openai-codex": "codex",
            "xai-cursor": "cursor-agent",
            "google-antigravity": "agy",
        }
        identity = family
        executable = _command(command_names[family])
        actor = str(actors.get(identity) or "").lower()
        if identity == author_identity or not actor or actor == author_actor or executable is None:
            continue
        arguments = {
            "openai-codex": [executable, "exec", "--skip-git-repo-check", PROBE_PROMPT],
            "xai-cursor": [executable, "-p", PROBE_PROMPT],
            "google-antigravity": [executable, "-p", PROBE_PROMPT],
        }[family]
        if _probe_ok(runner(arguments)):
            return family, identity, actor
    return None


def choose_initial_reviewer(
    number: int,
    author_identity: str,
    author_family: str,
    author_actor: str = "",
    *,
    external_states: dict[str, str] | None = None,
    reviewer_actors: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> tuple[str, str | None, str | None]:
    external = initial_external(external_states or registered_external_states())
    if external:
        return external, None, None
    if not author_actor:
        author_actor = current_github_actor()
    coding = probe_coding_reviewer(
        author_identity=author_identity,
        author_family=author_family,
        author_actor=author_actor,
        rotation_key=number,
        reviewer_actors=reviewer_actors,
        runner=probe_runner,
    )
    if coding is None:
        raise KernelError("no external or distinct coding-agent reviewer is available")
    return coding


def _trusted_external_actor(record: dict[str, Any], service: str) -> bool:
    actor = record.get("user") or record.get("author") or {}
    login = str(actor.get("login") or "").lower()
    actor_type = str(actor.get("type") or actor.get("__typename") or "")
    return login in EXTERNAL_ACTORS[service] and actor_type in {"", "Bot"}


def _check_service(record: dict[str, Any], service: str) -> bool:
    name = str(record.get("name") or record.get("context") or "")
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    aliases = {
        "coderabbit": ("coderabbit",),
        "sourcery": ("sourcery", "sourceryreview"),
        "codeant": ("codeant", "codeantai"),
    }[service]
    return normalized in aliases


def _parse_time(value: str, *, subject: str = "review assignment") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise KernelError(f"{subject} timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise KernelError(f"{subject} timestamp has no timezone")
    return parsed


def _evidence_time(record: dict[str, Any], *, subject: str) -> datetime:
    keys = (
        "submitted_at", "submittedAt", "completedAt", "startedAt",
        "updated_at", "updatedAt", "created_at", "createdAt",
    )
    for key in keys:
        value = record.get(key)
        if value:
            return _parse_time(str(value), subject=subject)
    raise KernelError(f"{subject} timestamp is missing")


def _latest_state(evidence: list[tuple[datetime, str]], *, subject: str) -> str | None:
    if not evidence:
        return None
    latest_at = max(observed_at for observed_at, _state in evidence)
    states = {state for observed_at, state in evidence if observed_at == latest_at}
    if len(states) != 1:
        raise KernelError(f"{subject} evidence conflicts at the latest timestamp")
    return states.pop()


def _external_evidence(records: list[dict[str, Any]], service: str) -> list[tuple[datetime, str]]:
    evidence: list[tuple[datetime, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise KernelError("external reviewer evidence is malformed")
        if not _trusted_external_actor(record, service):
            continue
        if str(record.get("state") or "").upper() in {
            "APPROVED", "CHANGES_REQUESTED", "COMMENTED"
        }:
            state = AVAILABLE
        elif UNAVAILABLE_RE.search(str(record.get("body") or "")):
            state = UNAVAILABLE
        else:
            continue
        evidence.append((_evidence_time(record, subject="external reviewer evidence"), state))
    return evidence


def external_state(
    pr: dict[str, Any],
    service: str,
    *,
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> str:
    evidence = _external_evidence([*reviews, *comments], service)

    checks = pr.get("statusCheckRollup")
    if not isinstance(checks, list):
        raise KernelError("external review check state is incomplete")
    matching = [item for item in checks if isinstance(item, dict) and _check_service(item, service)]
    if len(matching) > 1:
        raise KernelError("external reviewer returned ambiguous checks")
    if matching:
        check = matching[0]
        state = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if state == "SUCCESS":
            check_state = AVAILABLE
        elif state in {"ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "SKIPPED"}:
            check_state = UNAVAILABLE
        elif state in {"FAILURE", "NEUTRAL"}:
            check_state = AVAILABLE
        elif status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"}:
            check_state = PENDING
        else:
            check_state = None
        if check_state:
            evidence.append((_evidence_time(check, subject="external reviewer check"), check_state))
    return _latest_state(evidence, subject="external reviewer") or PENDING


def _authority_assigned_at(
    pr: dict[str, Any], events: list[dict[str, Any]], authority: str
) -> datetime:
    created_at = _parse_time(str(pr.get("createdAt") or ""))
    assignments: list[datetime] = []
    expected_label = REVIEW_PREFIX + authority
    for event in events:
        if not isinstance(event, dict):
            raise KernelError("review assignment evidence is malformed")
        if event.get("event") != "labeled":
            continue
        label = event.get("label")
        if not isinstance(label, dict):
            raise KernelError("review assignment evidence is malformed")
        if label.get("name") == expected_label:
            assignments.append(_evidence_time(event, subject="review assignment"))
    return max(assignments, default=created_at)


def _external_decision(
    number: int, pr: dict[str, Any], authority: str, observed_at: datetime
) -> tuple[str, int | None]:
    slug = repo_slug()
    reviews = gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    comments = gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")
    events = gh_paginated(f"repos/{slug}/issues/{number}/events?per_page=100")
    state = external_state(pr, authority, reviews=reviews, comments=comments)
    age = (observed_at - _authority_assigned_at(pr, events, authority)).total_seconds()
    if age < 0:
        raise KernelError("review observation predates assignment")
    if state == AVAILABLE:
        return "external-available", None
    if state == PENDING and age < EXTERNAL_TIMEOUT_SECONDS:
        return "external-pending", EXTERNAL_TIMEOUT_SECONDS - int(age)
    reason = "external-unavailable" if state == UNAVAILABLE else "external-pending-15m"
    return reason, None


def _one_authority(pr: dict[str, Any]) -> str:
    authorities = [
        name[len(REVIEW_PREFIX) :]
        for name in label_names(pr)
        if name.startswith(REVIEW_PREFIX)
    ]
    if len(authorities) != 1 or authorities[0] not in REVIEW_AUTHORITIES:
        raise KernelError("PR must have exactly one supported review authority")
    reviewer_identities = [
        name for name in label_names(pr) if name.startswith(REVIEWER_PREFIX)
    ]
    reviewer_actors = [
        name for name in label_names(pr) if name.startswith(REVIEWER_ACTOR_PREFIX)
    ]
    if authorities[0] in CODING_REVIEWERS and (
        len(reviewer_identities) != 1 or len(reviewer_actors) != 1
    ):
        raise KernelError("coding authority requires one reviewer identity and actor")
    if authorities[0] in EXTERNAL_REVIEWERS and (reviewer_identities or reviewer_actors):
        raise KernelError("external authority conflicts with coding reviewer metadata")
    return authorities[0]


def _one_label_value(pr: dict[str, Any], prefix: str) -> str:
    values = [name[len(prefix) :] for name in label_names(pr) if name.startswith(prefix)]
    if len(values) != 1:
        raise KernelError(f"PR must have exactly one {prefix} identity label")
    return values[0]


def _same_assignment(reference: dict[str, Any], live: dict[str, Any]) -> bool:
    return bool(
        isinstance(live, dict)
        and live.get("headRefOid") == reference.get("headRefOid")
        and _one_authority(live) == _one_authority(reference)
        and _one_label_value(live, AUTHOR_PREFIX)
        == _one_label_value(reference, AUTHOR_PREFIX)
        and _one_label_value(live, AUTHOR_FAMILY_PREFIX)
        == _one_label_value(reference, AUTHOR_FAMILY_PREFIX)
    )


def replace_authority(
    number: int,
    pr: dict[str, Any],
    authority: str,
    reviewer_identity: str | None,
    reviewer_actor: str | None,
    before_write: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    fields = "number,createdAt,headRefOid,labels,author,statusCheckRollup"
    live = gh_json(["pr", "view", str(number), "--json", fields])
    if not _same_assignment(pr, live):
        raise KernelError("review authority changed during fallback selection")
    if authority in CODING_REVIEWERS and not all((reviewer_identity, reviewer_actor)):
        raise KernelError("coding authority replacement requires identity and actor")
    if authority in EXTERNAL_REVIEWERS and any((reviewer_identity, reviewer_actor)):
        raise KernelError("external authority replacement cannot retain coding metadata")
    review_label = REVIEW_PREFIX + authority
    description = (
        f"Coding review: {authority}"
        if authority in CODING_REVIEWERS
        else f"External review: {authority}"
    )
    color = "5319e7" if authority in CODING_REVIEWERS else "0e8a16"
    ensure_label(review_label, color=color, description=description)
    assignment_labels = [review_label]
    if reviewer_identity and reviewer_actor:
        reviewer_label = REVIEWER_PREFIX + reviewer_identity
        actor_label = REVIEWER_ACTOR_PREFIX + reviewer_actor
        ensure_label(
            reviewer_label,
            color="5319e7",
            description=f"Assigned reviewer: {reviewer_identity}",
        )
        ensure_label(
            actor_label,
            color="5319e7",
            description=f"Trusted review actor: {reviewer_actor}",
        )
        assignment_labels.extend([reviewer_label, actor_label])
    live = gh_json(["pr", "view", str(number), "--json", fields])
    if not _same_assignment(pr, live):
        raise KernelError("review authority changed during fallback selection")
    if before_write:
        before_write(live)
    controlled = [
        name
        for name in label_names(live)
        if name.startswith((REVIEW_PREFIX, REVIEWER_PREFIX, REVIEWER_ACTOR_PREFIX))
    ]
    arguments = ["gh", "pr", "edit", str(number), "--add-label", ",".join(assignment_labels)]
    removed = [name for name in controlled if name not in assignment_labels]
    if removed:
        arguments.extend(["--remove-label", ",".join(removed)])
    run(arguments)


def recover_coding_authority(
    number: int,
    pr: dict[str, Any],
    authority: str,
    reason: str,
    observed_at: datetime,
) -> dict[str, Any]:
    reason = reason.strip()
    if len(reason) < 10:
        raise KernelError("coding reviewer unavailability reason is too short")
    external = initial_external(registered_external_states())
    if external is None:
        raise KernelError("no registered external reviewer is available")
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "reason": "coding-reviewer-unavailable",
        "detail": reason,
        "new_authority": external,
    }
    body = (
        "## Aru authoritative reviewer recovery\n\n"
        f"Fallback attempt: replace unavailable coding authority `{authority}` "
        f"with registered external authority `{external}`. Detail: {reason}. "
        "The labels remain authoritative if this transition command fails.\n\n"
        f"<!-- aru-review-assignment:v1 {json.dumps(status, sort_keys=True)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])
    replace_authority(number, pr, external, None, None)
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != external:
        raise KernelError("external reviewer recovery was not confirmed")
    return {
        "pr": number,
        "authority": external,
        "action": "fallback",
        "reason": "coding-reviewer-unavailable",
    }


def refresh_assignment(
    number: int,
    *,
    now: datetime | None = None,
    coding_unavailable_reason: str | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> dict[str, Any]:
    pr = gh_json(
        [
            "pr",
            "view",
            str(number),
            "--json",
            "number,url,createdAt,headRefOid,labels,author,statusCheckRollup",
        ]
    )
    if not isinstance(pr, dict) or pr.get("number") != number:
        raise KernelError(f"pull request #{number} is unavailable")
    authority = _one_authority(pr)
    if authority in CODING_REVIEWERS:
        if not coding_unavailable_reason:
            return {
                "pr": number,
                "authority": authority,
                "action": "retained",
                "reason": "coding-agent-assigned",
            }
        return recover_coding_authority(
            number,
            pr,
            authority,
            coding_unavailable_reason,
            now or datetime.now(timezone.utc),
        )

    observed_at = now or datetime.now(timezone.utc)
    reason, remaining = _external_decision(number, pr, authority, observed_at)
    if reason in {"external-available", "external-pending"}:
        result = {"pr": number, "authority": authority, "action": "retained", "reason": reason}
        if remaining is not None:
            result["remaining_seconds"] = remaining
        return result

    author_identity = _one_label_value(pr, AUTHOR_PREFIX)
    author_family = _one_label_value(pr, AUTHOR_FAMILY_PREFIX)
    coding = probe_coding_reviewer(
        author_identity=author_identity,
        author_family=author_family,
        author_actor=str((pr.get("author") or {}).get("login") or ""),
        rotation_key=number,
        runner=probe_runner,
    )
    if coding is None:
        raise KernelError("no distinct coding-agent reviewer has available capacity")
    coding_family, reviewer_identity, reviewer_actor = coding
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "reason": reason,
        "new_authority": coding_family,
        "reviewer": reviewer_identity,
        "reviewer_actor": reviewer_actor,
        "reviewer_family": coding_family,
    }
    body = (
        "## Aru authoritative reviewer fallback\n\n"
        f"Fallback attempt: assign `{reviewer_identity}` ({coding_family}) through "
        f"GitHub actor `{reviewer_actor}` because `{authority}` was "
        f"{reason.replace('-', ' ')}. The labels remain authoritative if this "
        "transition command fails.\n\n"
        f"<!-- aru-review-assignment:v1 {json.dumps(status, sort_keys=True)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])

    def confirm_external_fallback(live: dict[str, Any]) -> None:
        confirmed, _remaining = _external_decision(
            number, live, authority, now or datetime.now(timezone.utc)
        )
        if confirmed not in {"external-unavailable", "external-pending-15m"}:
            raise KernelError(f"external reviewer recovered before fallback: {confirmed}")

    replace_authority(
        number,
        pr,
        coding_family,
        reviewer_identity,
        reviewer_actor,
        confirm_external_fallback,
    )
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != coding_family:
        raise KernelError("review authority replacement was not confirmed")
    if _one_label_value(updated, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("reviewer identity replacement was not confirmed")
    if _one_label_value(updated, REVIEWER_ACTOR_PREFIX) != reviewer_actor:
        raise KernelError("reviewer actor replacement was not confirmed")
    return {
        "pr": number,
        "authority": coding_family,
        "reviewer": reviewer_identity,
        "action": "fallback",
        "reason": reason,
    }


def require_current_owner(number: int, owner: str) -> None:
    live_issue = issue(number)
    if status_of(live_issue) != "In Progress" or current_agent(live_issue) != owner:
        raise KernelError("issue ownership changed before PR creation")


def create(
    number: int,
    title: str,
    body: str,
    agent: str | None = None,
    *,
    author_family: str | None = None,
    author_actor: str = "",
    external_states: dict[str, str] | None = None,
    reviewer_actors: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> dict[str, object]:
    record = issue(number)
    if status_of(record) != "In Progress":
        raise KernelError("issue must be In Progress")
    owner = current_agent(record)
    if agent and agent != owner:
        raise KernelError("provided agent does not own the issue")
    if re.search(r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+", body):
        raise KernelError("body must not contain a caller-supplied closing directive")
    branch = current_branch()
    if f"issue-{number}-" not in branch:
        raise KernelError("current branch does not belong to the issue")
    head = require_published_head(branch)
    owner_identity = normalized_identity(owner)
    family = normalized_identity(author_family) if author_family else agent_family(owner_identity)
    authority, reviewer_identity, reviewer_actor = choose_initial_reviewer(
        number,
        owner_identity,
        family,
        author_actor,
        external_states=external_states,
        reviewer_actors=reviewer_actors,
        probe_runner=probe_runner,
    )
    review_label = REVIEW_PREFIX + authority
    author_label = AUTHOR_PREFIX + owner_identity
    family_label = AUTHOR_FAMILY_PREFIX + family
    label_metadata = {
        review_label: ("0e8a16", f"Authoritative review: {authority}"),
        author_label: ("1d76db", f"PR authored by {owner_identity}"),
        family_label: ("1d76db", f"Author model family: {family}"),
    }
    labels = [review_label, author_label, family_label]
    if reviewer_identity:
        reviewer_label = REVIEWER_PREFIX + reviewer_identity
        actor_label = REVIEWER_ACTOR_PREFIX + str(reviewer_actor)
        label_metadata[reviewer_label] = ("5319e7", f"Assigned reviewer: {reviewer_identity}")
        label_metadata[actor_label] = ("5319e7", f"Trusted review actor: {reviewer_actor}")
        labels.extend([reviewer_label, actor_label])
    for label, (color, description) in label_metadata.items():
        ensure_label(label, color=color, description=description)
    final_body = body.rstrip() + f"\n\nCloses #{number}\n"
    arguments = ["gh", "pr", "create", "--title", title, "--body", final_body]
    for label in labels:
        arguments.extend(["--label", label])
    require_current_owner(number, owner)
    run(arguments)
    pr = gh_json(["pr", "view", branch, "--json", "number,url,headRefOid,labels"])
    if pr.get("headRefOid") != head:
        raise KernelError("created PR is not bound to the published head")
    if _one_authority(pr) != authority:
        raise KernelError("created PR does not have exactly one assigned reviewer")
    if reviewer_identity and _one_label_value(pr, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("created PR does not identify the coding-agent reviewer")
    if reviewer_actor and _one_label_value(pr, REVIEWER_ACTOR_PREFIX) != reviewer_actor:
        raise KernelError("created PR does not bind the coding reviewer actor")
    set_status(number, "In Review")
    return {
        "pr": int(pr["number"]),
        "url": pr["url"],
        "head": head,
        "reviewer": authority,
        "reviewer_identity": reviewer_identity,
        "reviewer_actor": reviewer_actor,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int)
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--agent")
    parser.add_argument("--author-family")
    parser.add_argument("--author-github-login")
    parser.add_argument("--refresh-reviewer", type=int, metavar="PR")
    parser.add_argument("--coding-reviewer-unavailable")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.refresh_reviewer:
            if any(
                (
                    args.issue,
                    args.title,
                    args.body,
                    args.agent,
                    args.author_family,
                    args.author_github_login,
                )
            ):
                raise KernelError("review refresh cannot include PR creation arguments")
            result = refresh_assignment(
                args.refresh_reviewer,
                coding_unavailable_reason=args.coding_reviewer_unavailable,
            )
        else:
            if args.coding_reviewer_unavailable:
                raise KernelError("coding reviewer unavailability requires --refresh-reviewer")
            if args.issue is None or args.title is None or args.body is None:
                raise KernelError("--issue, --title, and --body are required for PR creation")
            result = create(
                args.issue,
                args.title,
                args.body,
                args.agent,
                author_family=args.author_family,
                author_actor=args.author_github_login or "",
            )
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    elif args.refresh_reviewer:
        print(f"review authority: {result['authority']} ({result['reason']})")
    else:
        print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
