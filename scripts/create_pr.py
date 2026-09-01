#!/usr/bin/env python3
"""Open a PR and govern its single authoritative reviewer assignment."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import (
    AGENT_PREFIX,
    AUTHOR_FAMILY_PREFIX,
    AUTHOR_PREFIX,
    CODING_REVIEWERS,
    EXTERNAL_REVIEWERS,
    REVIEWER_ACTOR_PREFIX,
    REVIEWER_PREFIX,
    REVIEW_AUTHORITIES,
    REVIEW_PREFIX,
    KernelError,
    agent_family,
    ensure_label,
    gh_json,
    gh_paginated,
    git,
    issue,
    json_print,
    label_names,
    normalized_identity,
    review_evidence_unavailable,
    repo_slug,
    run,
    set_status,
    status_of,
)
from local_verification import bind_local_verification, refresh_verification
from review_policy import (
    ProbeRunner,
    ReviewPolicy,
    _default_probe,
    default_review_policy,
    load_repository_review_policy,
    probe_coding_reviewer,
    registration_states,
    reviewer_status,
    select_reviewer_from_order,
)

AVAILABLE = "available"
PENDING = "pending"
UNAVAILABLE = "unavailable"
EXTERNAL_ACTORS = {
    "coderabbit": {"coderabbitai", "coderabbitai[bot]"},
    "sourcery": {"sourcery-ai", "sourcery-ai[bot]", "sourcery"},
    "codeant": {"codeant-ai", "codeant-ai[bot]"},
}

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

def registered_external_states(names: tuple[str, ...] | None = None) -> dict[str, str]:
    if names is None:
        records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise KernelError("reviewer registration labels are unavailable")
        names = tuple(str(item.get("name") or "") for item in records)
    states = registration_states(names)
    return {
        service: AVAILABLE if state == "available" else UNAVAILABLE
        for service, state in states.items()
    }

def choose_initial_reviewer(
    number: int,
    author_identity: str,
    author_family: str,
    author_actor: str = "",
    *,
    external_states: dict[str, str] | None = None,
    policy: ReviewPolicy | None = None,
    reviewer_actors: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> tuple[str, str | None, str | None]:
    if external_states is None and policy is None:
        policy, names = load_repository_review_policy()
        states = registered_external_states(names)
    else:
        states = external_states if external_states is not None else registered_external_states()
        if policy is None:
            registered = [
                service
                for service in EXTERNAL_REVIEWERS
                if states.get(service) in {AVAILABLE, PENDING}
            ]
            policy = default_review_policy(registered)
    if any(state not in {AVAILABLE, PENDING, UNAVAILABLE} for state in states.values()):
        raise KernelError("external reviewer availability is malformed")
    selected = select_reviewer_from_order(
        policy.authorities,
        number=number,
        author_identity=author_identity,
        author_family=author_family,
        author_actor=author_actor,
        external_states=states,
        reviewer_actors=reviewer_actors,
        probe_runner=probe_runner,
        coding_probe=probe_coding_reviewer,
    )
    if selected is not None:
        return selected
    raise KernelError("no external or distinct coding-agent reviewer is available")

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
    if len(states := {state for observed_at, state in evidence if observed_at == latest_at}) != 1:
        raise KernelError(f"{subject} evidence conflicts at the latest timestamp")
    return states.pop()

def _external_evidence(records: list[dict[str, Any]], service: str) -> list[tuple[datetime, str]]:
    evidence: list[tuple[datetime, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise KernelError("external reviewer evidence is malformed")
        if not _trusted_external_actor(record, service):
            continue
        if review_evidence_unavailable(record):
            state = UNAVAILABLE
        elif str(record.get("state") or "").upper() in {
            "APPROVED", "CHANGES_REQUESTED", "COMMENTED"
        }:
            state = AVAILABLE
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
    statuses: list[dict[str, Any]] | None = None,
) -> str:
    evidence = _external_evidence([*reviews, *comments], service)

    checks = pr.get("statusCheckRollup")
    if not isinstance(checks, list):
        raise KernelError("external review check state is incomplete")
    matching = [item for item in checks if isinstance(item, dict) and _check_service(item, service)]
    if len(matching) > 1:
        raise KernelError("external reviewer returned ambiguous checks")
    detailed = [item for item in statuses or [] if isinstance(item, dict) and _check_service(item, service)]
    for check in detailed or matching:
        state = str(check.get("conclusion") or check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if review_evidence_unavailable(check):
            check_state = UNAVAILABLE
        elif state == "SUCCESS":
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
    number: int,
    pr: dict[str, Any],
    authority: str,
    observed_at: datetime,
    timeout_seconds: int = 2 * 60,
) -> tuple[str, int | None]:
    slug = repo_slug()
    reviews = gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    comments = gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")
    events = gh_paginated(f"repos/{slug}/issues/{number}/events?per_page=100")
    statuses_complete = True
    try:
        statuses = gh_paginated(
            f"repos/{slug}/commits/{pr['headRefOid']}/statuses?per_page=100"
        )
    except KernelError:
        statuses = []
        statuses_complete = False
    state = external_state(
        pr,
        authority,
        reviews=reviews,
        comments=comments,
        statuses=statuses,
    )
    if not statuses_complete and state == AVAILABLE:
        state = PENDING
    age = (observed_at - _authority_assigned_at(pr, events, authority)).total_seconds()
    if age < 0:
        raise KernelError("review observation predates assignment")
    if state == AVAILABLE:
        return "external-available", None
    if state == PENDING and age < timeout_seconds:
        return "external-pending", timeout_seconds - int(age)
    reason = "external-unavailable" if state == UNAVAILABLE else "external-pending-timeout"
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
    reason: str | None,
    observed_at: datetime,
    policy: ReviewPolicy,
    external_states: dict[str, str],
) -> dict[str, Any]:
    if not reason:
        return {
            "pr": number,
            "authority": authority,
            "action": "retained",
            "reason": "coding-agent-assigned",
        }
    reason = reason.strip()
    if len(reason) < 10:
        raise KernelError("coding reviewer unavailability reason is too short")
    external = next(
        (
            candidate
            for candidate in policy.authorities
            if candidate in EXTERNAL_REVIEWERS
            and external_states.get(candidate) in {AVAILABLE, PENDING}
        ),
        None,
    )
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


def _effective_policy(
    policy: ReviewPolicy | None,
    external_states: dict[str, str] | None,
) -> tuple[ReviewPolicy, dict[str, str]]:
    if policy is not None and external_states is not None:
        return policy, external_states
    loaded_policy, names = load_repository_review_policy()
    return policy or loaded_policy, external_states or registered_external_states(names)


def refresh_assignment(
    number: int,
    *,
    now: datetime | None = None,
    coding_unavailable_reason: str | None = None,
    policy: ReviewPolicy | None = None,
    external_states: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> dict[str, Any]:
    policy, external_states = _effective_policy(policy, external_states)
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
        return recover_coding_authority(
            number,
            pr,
            authority,
            coding_unavailable_reason,
            now or datetime.now(timezone.utc),
            policy,
            external_states,
        )

    observed_at = now or datetime.now(timezone.utc)
    reason, remaining = _external_decision(
        number, pr, authority, observed_at, policy.timeout_seconds
    )
    if reason in {"external-available", "external-pending"}:
        result = {"pr": number, "authority": authority, "action": "retained", "reason": reason}
        if remaining is not None:
            result["remaining_seconds"] = remaining
        return result

    author_identity = _one_label_value(pr, AUTHOR_PREFIX)
    author_family = _one_label_value(pr, AUTHOR_FAMILY_PREFIX)
    selected = select_reviewer_from_order(
        policy.after(authority),
        number=number,
        author_identity=author_identity,
        author_family=author_family,
        author_actor=str((pr.get("author") or {}).get("login") or ""),
        external_states=external_states,
        reviewer_actors=None,
        probe_runner=probe_runner,
        coding_probe=probe_coding_reviewer,
    )
    if selected is None:
        raise KernelError("no configured fallback reviewer has available capacity")
    new_authority, reviewer_identity, reviewer_actor = selected
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "reason": reason,
        "new_authority": new_authority,
        "reviewer": reviewer_identity,
        "reviewer_actor": reviewer_actor,
        "reviewer_family": new_authority if new_authority in CODING_REVIEWERS else None,
    }
    reviewer_description = (
        f"assign `{reviewer_identity}` ({new_authority}) through GitHub actor "
        f"`{reviewer_actor}`"
        if reviewer_identity and reviewer_actor
        else f"assign registered external authority `{new_authority}`"
    )
    body = (
        "## Aru authoritative reviewer fallback\n\n"
        f"Fallback attempt: {reviewer_description} because `{authority}` was "
        f"{reason.replace('-', ' ')}. The labels remain authoritative if this "
        "transition command fails.\n\n"
        f"<!-- aru-review-assignment:v1 {json.dumps(status, sort_keys=True)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])

    def confirm_external_fallback(live: dict[str, Any]) -> None:
        confirmed, _remaining = _external_decision(
            number,
            live,
            authority,
            now or datetime.now(timezone.utc),
            policy.timeout_seconds,
        )
        if confirmed not in {"external-unavailable", "external-pending-timeout"}:
            raise KernelError(f"external reviewer recovered before fallback: {confirmed}")

    replace_authority(
        number,
        pr,
        new_authority,
        reviewer_identity,
        reviewer_actor,
        confirm_external_fallback,
    )
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != new_authority:
        raise KernelError("review authority replacement was not confirmed")
    if reviewer_identity and _one_label_value(updated, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("reviewer identity replacement was not confirmed")
    if reviewer_actor and _one_label_value(updated, REVIEWER_ACTOR_PREFIX) != reviewer_actor:
        raise KernelError("reviewer actor replacement was not confirmed")
    result = {
        "pr": number,
        "authority": new_authority,
        "action": "fallback",
        "reason": reason,
    }
    if reviewer_identity:
        result["reviewer"] = reviewer_identity
    return result

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
    final_body, _checks = bind_local_verification(
        body.rstrip() + f"\n\nCloses #{number}\n",
        head,
    )
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

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int)
    for name in ("--title", "--body", "--body-file", "--agent", "--author-family", "--author-github-login"):
        parser.add_argument(name)
    parser.add_argument("--refresh-reviewer", type=int, metavar="PR")
    parser.add_argument("--refresh-verification", type=int, metavar="PR")
    parser.add_argument("--coding-reviewer-unavailable")
    parser.add_argument("--reviewer-status", action="store_true")
    parser.add_argument("--probe-reviewers", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _resolve_body_argument(args: argparse.Namespace) -> str | None:
    if args.body and args.body_file:
        raise KernelError("provide exactly one of --body or --body-file")
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return args.body


def _reject_unexpected_args(args: argparse.Namespace, message: str, names: tuple[str, ...]) -> None:
    if any(getattr(args, name) for name in names):
        raise KernelError(message)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    plain_output = ""
    try:
        if args.reviewer_status:
            _reject_unexpected_args(
                args,
                "reviewer status cannot include PR mutation arguments",
                (
                    "issue",
                    "title",
                    "body",
                    "body_file",
                    "author_family",
                    "refresh_reviewer",
                    "refresh_verification",
                    "coding_reviewer_unavailable",
                ),
            )
            result = reviewer_status(
                probe=args.probe_reviewers,
                author_identity=args.agent or "",
                author_actor=args.author_github_login or "",
            )
            policy = result["policy"]
            plain_output = (
                f"review policy: {policy['primary']} -> "
                f"{', '.join(policy['fallbacks']) or 'none'} "
                f"({policy['timeout_seconds']}s timeout; valid={result['valid']})"
            )
        elif args.refresh_reviewer:
            _reject_unexpected_args(
                args,
                "review refresh cannot include PR creation arguments",
                (
                    "issue",
                    "title",
                    "body",
                    "body_file",
                    "agent",
                    "author_family",
                    "author_github_login",
                    "refresh_verification",
                    "reviewer_status",
                    "probe_reviewers",
                ),
            )
            result = refresh_assignment(
                args.refresh_reviewer,
                coding_unavailable_reason=args.coding_reviewer_unavailable,
            )
            plain_output = f"review authority: {result['authority']} ({result['reason']})"
        elif args.refresh_verification:
            _reject_unexpected_args(
                args,
                "verification refresh cannot include PR creation arguments",
                (
                    "issue",
                    "title",
                    "agent",
                    "author_family",
                    "author_github_login",
                    "refresh_reviewer",
                    "coding_reviewer_unavailable",
                    "reviewer_status",
                    "probe_reviewers",
                ),
            )
            body = _resolve_body_argument(args)
            if body is None:
                raise KernelError("--body or --body-file is required for verification refresh")
            result = refresh_verification(args.refresh_verification, body)
            plain_output = f"verification refreshed for PR #{result['pr']}"
        else:
            if args.coding_reviewer_unavailable:
                raise KernelError("coding reviewer unavailability requires --refresh-reviewer")
            body = _resolve_body_argument(args)
            if args.issue is None or args.title is None or body is None:
                raise KernelError(
                    "--issue, --title, and exactly one of --body or --body-file are required for PR creation"
                )
            result = create(
                args.issue,
                args.title,
                body,
                args.agent,
                author_family=args.author_family,
                author_actor=args.author_github_login or "",
            )
            plain_output = str(result["url"])
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print(plain_output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
