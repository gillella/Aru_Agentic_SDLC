#!/usr/bin/env python3
"""Open a PR and govern its single authoritative reviewer assignment."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
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
    default_branch_name,
    ensure_label,
    gh_json,
    git,
    issue,
    json_print,
    label_names,
    normalized_identity,
    review_risk_tier,
    run,
    set_status,
    status_of,
)
from review_evidence import (  # noqa: F401 -- compatibility exports
    AVAILABLE,
    PENDING,
    UNAVAILABLE,
    external_state,
    parse_time as _parse_time,
)
from reviewer_probe import ProbeRunner, _default_probe
from merge_state import pull_changed_paths
from review_policy import (
    ACTIVITY_GRACE_SECONDS,
    ReviewPolicy,
    attempted_reviewer_keys,
    effective_review_policy,
    external_decision as _external_decision,
    load_repository_review_policy,
    probe_coding_reviewer,
    registered_external_states,
    reviewer_continuation,  # noqa: F401 -- compatibility export
    reviewer_status,
    select_reviewer_from_pool,
)

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
def local_changed_paths() -> list[str]:
    """Return both sides of every changed path on the published issue branch."""
    base = f"origin/{default_branch_name()}"
    output = git(["diff", "--name-status", "--find-renames", f"{base}...HEAD"])
    paths: list[str] = []
    for line in output.splitlines():
        fields = line.split("\t")
        status = fields[0][:1] if fields and fields[0] else ""
        expected = 3 if status in {"R", "C"} else 2
        if status not in {"A", "C", "D", "M", "R", "T"} or len(fields) != expected:
            raise KernelError("local changed-path evidence is malformed")
        paths.extend(fields[1:])
    if not paths:
        raise KernelError("published branch has no changed files")
    return sorted(set(paths))

def choose_initial_reviewer(
    number: int, author_identity: str, author_family: str, author_actor: str = "", *,
    external_states: dict[str, str] | None = None,
    policy: ReviewPolicy | None = None,
    reviewer_actors: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> tuple[str, str | None, str | None]:
    if policy is None and external_states is None:
        effective, names = load_repository_review_policy()
        states = registered_external_states(names)
    else:
        states = external_states if external_states is not None else registered_external_states()
        effective = effective_review_policy(policy, external_states)
    selected = select_reviewer_from_pool(
        effective,
        number=number,
        author_identity=author_identity,
        author_family=author_family,
        author_actor=author_actor,
        external_states=states,
        reviewer_actors=reviewer_actors,
        probe_runner=probe_runner,
        coding_probe=probe_coding_reviewer,
    )
    if selected is None:
        raise KernelError("no external or distinct coding-agent reviewer is available")
    return selected


def _attempted_reviewer_keys(number: int, pr: dict[str, Any]) -> set[str]:
    authority = _one_authority(pr)
    identity = _one_label_value(pr, REVIEWER_PREFIX) if authority in CODING_REVIEWERS else None
    return attempted_reviewer_keys(
        number,
        head=str(pr.get("headRefOid") or ""),
        authority=authority,
        identity=identity,
    )

def _optional_authority(pr: dict[str, Any]) -> str | None:
    authorities = [
        name[len(REVIEW_PREFIX) :]
        for name in label_names(pr)
        if name.startswith(REVIEW_PREFIX)
    ]
    reviewer_identities = [
        name for name in label_names(pr) if name.startswith(REVIEWER_PREFIX)
    ]
    reviewer_actors = [
        name for name in label_names(pr) if name.startswith(REVIEWER_ACTOR_PREFIX)
    ]
    if not authorities:
        if reviewer_identities or reviewer_actors:
            raise KernelError("reviewer metadata exists without a review authority")
        return None
    if len(authorities) != 1 or authorities[0] not in REVIEW_AUTHORITIES:
        raise KernelError("PR must have exactly one supported review authority")
    if authorities[0] in CODING_REVIEWERS and (
        len(reviewer_identities) != 1 or len(reviewer_actors) != 1
    ):
        raise KernelError("coding authority requires one reviewer identity and actor")
    if authorities[0] in EXTERNAL_REVIEWERS and (reviewer_identities or reviewer_actors):
        raise KernelError("external authority conflicts with coding reviewer metadata")
    return authorities[0]


def _one_authority(pr: dict[str, Any]) -> str:
    authority = _optional_authority(pr)
    if authority is None:
        raise KernelError("PR must have exactly one supported review authority")
    return authority

def _one_label_value(pr: dict[str, Any], prefix: str) -> str:
    values = [name[len(prefix) :] for name in label_names(pr) if name.startswith(prefix)]
    if len(values) != 1:
        raise KernelError(f"PR must have exactly one {prefix} identity label")
    return values[0]

def _same_assignment(reference: dict[str, Any], live: dict[str, Any]) -> bool:
    return bool(
        isinstance(live, dict)
        and live.get("headRefOid") == reference.get("headRefOid")
        and _optional_authority(live) == _optional_authority(reference)
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
    probe_runner: ProbeRunner,
    policy: ReviewPolicy | None = None,
    external_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    reason = reason.strip()
    if len(reason) < 10:
        raise KernelError("coding reviewer unavailability reason is too short")
    effective = policy or load_repository_review_policy()[0]
    states = external_states if external_states is not None else registered_external_states(head=str(pr["headRefOid"]))
    previous_reviewer = _one_label_value(pr, REVIEWER_PREFIX)
    selected = select_reviewer_from_pool(
        effective,
        number=number,
        author_identity=_one_label_value(pr, AUTHOR_PREFIX),
        author_family=_one_label_value(pr, AUTHOR_FAMILY_PREFIX),
        author_actor=str((pr.get("author") or {}).get("login") or ""),
        external_states=states,
        reviewer_actors=None,
        probe_runner=probe_runner,
        excluded=_attempted_reviewer_keys(number, pr),
        coding_probe=probe_coding_reviewer,
    )
    if selected is None:
        raise KernelError("no untried reviewer has available capacity")
    new_authority, reviewer_identity, reviewer_actor = selected
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "previous_reviewer": previous_reviewer,
        "reason": "coding-reviewer-unavailable",
        "detail": reason,
        "new_authority": new_authority,
        "reviewer": reviewer_identity,
        "reviewer_actor": reviewer_actor,
    }
    body = (
        "## Aru authoritative reviewer recovery\n\n"
        f"Fallback attempt: replace unavailable coding authority `{authority}` "
        f"with untried authority `{new_authority}`. Detail: {reason}. "
        "The labels remain authoritative if this transition command fails.\n\n"
        f"<!-- aru-review-assignment:v1 {json.dumps(status, sort_keys=True)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])
    replace_authority(number, pr, new_authority, reviewer_identity, reviewer_actor)
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != new_authority:
        raise KernelError("reviewer recovery was not confirmed")
    if reviewer_identity and _one_label_value(updated, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("coding reviewer recovery identity was not confirmed")
    if reviewer_actor and _one_label_value(updated, REVIEWER_ACTOR_PREFIX) != reviewer_actor:
        raise KernelError("coding reviewer recovery actor was not confirmed")
    result = {
        "pr": number,
        "authority": new_authority,
        "action": "fallback",
        "reason": "coding-reviewer-unavailable",
    }
    if reviewer_identity:
        result["reviewer"] = reviewer_identity
    return result


def assign_missing_authority(
    number: int,
    pr: dict[str, Any],
    observed_at: datetime,
    probe_runner: ProbeRunner,
    *,
    policy: ReviewPolicy | None = None,
    external_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    risk_tier = review_risk_tier(pull_changed_paths(number))
    if risk_tier < 2:
        return {
            "pr": number, "authority": None, "action": "retained",
            "reason": "review-not-required", "retry_at": None,
            "next_action": "merge-when-checks-pass",
        }
    author_identity = _one_label_value(pr, AUTHOR_PREFIX)
    author_family = _one_label_value(pr, AUTHOR_FAMILY_PREFIX)
    effective = policy or load_repository_review_policy()[0]
    states = external_states if external_states is not None else registered_external_states(head=str(pr["headRefOid"]))
    authority, identity, actor = choose_initial_reviewer(
        number,
        author_identity,
        author_family,
        str((pr.get("author") or {}).get("login") or ""),
        policy=effective,
        external_states=states,
        probe_runner=probe_runner,
    )
    replace_authority(number, pr, authority, identity, actor)
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != authority:
        raise KernelError("newly required review authority was not confirmed")
    external = authority in EXTERNAL_REVIEWERS
    delay = effective.timeout_seconds if states.get(authority) == AVAILABLE else min(effective.timeout_seconds, ACTIVITY_GRACE_SECONDS)
    return {
        "pr": number, "authority": authority, "reviewer": identity,
        "action": "assigned", "reason": "risk-tier-requires-review",
        "risk_tier": risk_tier,
        "retry_at": (
            (observed_at + timedelta(seconds=delay)).isoformat()
            if external else None
        ),
        "next_action": "refresh-reviewer" if external else "await-authoritative-review",
    }


def _refresh_external_authority(
    number: int,
    pr: dict[str, Any],
    authority: str,
    observed_at: datetime,
    probe_runner: ProbeRunner,
    *,
    policy: ReviewPolicy | None = None,
    external_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    effective = policy or load_repository_review_policy()[0]
    reason, remaining = _external_decision(
        number, pr, authority, observed_at, effective.timeout_seconds
    )
    if reason in {"external-available", "external-pending"}:
        result = {
            "pr": number, "authority": authority, "action": "retained",
            "reason": reason, "retry_at": None,
            "next_action": "await-authoritative-review",
        }
        if remaining is not None:
            result["remaining_seconds"] = remaining
            result["retry_at"] = (
                observed_at + timedelta(seconds=remaining)
            ).isoformat()
            result["next_action"] = "refresh-reviewer"
        return result

    states = external_states if external_states is not None else registered_external_states(head=str(pr["headRefOid"]))
    author_identity = _one_label_value(pr, AUTHOR_PREFIX)
    author_family = _one_label_value(pr, AUTHOR_FAMILY_PREFIX)
    selected = select_reviewer_from_pool(
        effective,
        number=number,
        author_identity=author_identity,
        author_family=author_family,
        author_actor=str((pr.get("author") or {}).get("login") or ""),
        external_states=states,
        reviewer_actors=None,
        probe_runner=probe_runner,
        excluded=_attempted_reviewer_keys(number, pr),
        coding_probe=probe_coding_reviewer,
    )
    if selected is None:
        raise KernelError("no untried reviewer has available capacity")
    new_authority, reviewer_identity, reviewer_actor = selected
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "previous_reviewer": None,
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
            datetime.now(timezone.utc),
            effective.timeout_seconds,
        )
        if confirmed not in {"external-unavailable", "external-pending-timeout", "external-retired"}:
            raise KernelError(f"external reviewer recovered before fallback: {confirmed}")

    replace_authority(
        number, pr, new_authority, reviewer_identity, reviewer_actor,
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
        "pr": number, "authority": new_authority,
        "action": "fallback", "reason": reason,
    }
    if reviewer_identity:
        result["reviewer"] = reviewer_identity
    return result


def refresh_assignment(
    number: int,
    *,
    now: datetime | None = None,
    coding_unavailable_reason: str | None = None,
    probe_runner: ProbeRunner = _default_probe,
    policy: ReviewPolicy | None = None,
    external_states: dict[str, str] | None = None,
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
    observed_at = now or datetime.now(timezone.utc)
    if policy is None:
        policy, names = load_repository_review_policy()
        if external_states is None:
            external_states = registered_external_states(names, head=str(pr["headRefOid"]))
    authority = _optional_authority(pr)
    if authority is None:
        if coding_unavailable_reason:
            raise KernelError("coding reviewer unavailability requires a coding authority")
        return assign_missing_authority(
            number,
            pr,
            observed_at,
            probe_runner,
            policy=policy,
            external_states=external_states,
        )
    if authority in CODING_REVIEWERS:
        if not coding_unavailable_reason:
            return {
                "pr": number,
                "authority": authority,
                "action": "retained",
                "reason": "coding-agent-assigned",
                "retry_at": None,
                "next_action": "await-authoritative-review",
            }
        return recover_coding_authority(
            number,
            pr,
            authority,
            coding_unavailable_reason,
            observed_at,
            probe_runner,
            policy,
            external_states,
        )

    return _refresh_external_authority(
        number,
        pr,
        authority,
        observed_at,
        probe_runner,
        policy=policy,
        external_states=external_states,
    )

def require_current_owner(number: int, owner: str, status: str = "In Progress") -> None:
    live_issue = issue(number)
    if status_of(live_issue) != status or current_agent(live_issue) != owner:
        raise KernelError("issue ownership changed before PR creation")

def create(  # noqa: C901, PLR0912, PLR0915 -- one fail-closed creation transaction
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
    final_body = body.rstrip() + f"\n\nCloses #{number}\n"
    owner_identity = normalized_identity(owner)
    family = normalized_identity(author_family) if author_family else agent_family(owner_identity)
    risk_tier = review_risk_tier(local_changed_paths())
    authority: str | None = None
    reviewer_identity: str | None = None
    reviewer_actor: str | None = None
    policy: ReviewPolicy | None = None
    if risk_tier >= 2:
        policy = effective_review_policy(None, external_states)
        external_states = external_states if external_states is not None else registered_external_states(head=head)
        authority, reviewer_identity, reviewer_actor = choose_initial_reviewer(
            number,
            owner_identity,
            family,
            author_actor,
            external_states=external_states,
            policy=policy,
            reviewer_actors=reviewer_actors,
            probe_runner=probe_runner,
        )
    author_label = AUTHOR_PREFIX + owner_identity
    family_label = AUTHOR_FAMILY_PREFIX + family
    label_metadata = {
        author_label: ("1d76db", f"PR authored by {owner_identity}"),
        family_label: ("1d76db", f"Author model family: {family}"),
    }
    labels = [author_label, family_label]
    if authority:
        review_label = REVIEW_PREFIX + authority
        label_metadata[review_label] = ("0e8a16", f"Authoritative review: {authority}")
        labels.insert(0, review_label)
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
    rollback_target: str | int = branch
    try:
        pr = gh_json(
            [
                "pr", "view", branch, "--json",
                "number,url,state,createdAt,headRefOid,labels",
            ]
        )
        if (
            not isinstance(pr, dict)
            or not isinstance(pr.get("number"), int)
            or not isinstance(pr.get("url"), str)
            or pr.get("state") != "OPEN"
        ):
            raise KernelError("created PR snapshot is malformed")
        rollback_target = int(pr["number"])
        created_at = _parse_time(str(pr.get("createdAt") or ""))
        if pr.get("headRefOid") != head:
            raise KernelError("created PR is not bound to the published head")
        if authority and _one_authority(pr) != authority:
            raise KernelError("created PR does not have exactly one assigned reviewer")
        if not authority and any(
            name.startswith(REVIEW_PREFIX) for name in label_names(pr)
        ):
            raise KernelError("low-risk PR unexpectedly has an authoritative reviewer")
        if reviewer_identity and _one_label_value(pr, REVIEWER_PREFIX) != reviewer_identity:
            raise KernelError("created PR does not identify the coding-agent reviewer")
        if reviewer_actor and _one_label_value(pr, REVIEWER_ACTOR_PREFIX) != reviewer_actor:
            raise KernelError("created PR does not bind the coding reviewer actor")
        require_current_owner(number, owner)
        set_status(
            number,
            "In Review",
            expected_current="In Progress",
            pre_mutation_check=lambda: require_current_owner(number, owner),
        )
        require_current_owner(number, owner, "In Review")
    except KernelError as post_create_error:
        try:
            run(
                [
                    "gh", "pr", "close", str(rollback_target), "--comment",
                    "Aru closed this PR because post-creation validation did not settle.",
                ]
            )
            closed = gh_json(
                ["pr", "view", str(rollback_target), "--json", "number,state"]
            )
            if not isinstance(closed, dict) or closed.get("state") != "CLOSED":
                raise KernelError("created PR rollback did not settle closed")
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original post-create failure: {post_create_error}"
            ) from rollback_error
        raise
    continuation = {
        "next_action": "merge-when-checks-pass",
        "retry_at": None,
    }
    if authority in EXTERNAL_REVIEWERS:
        delay = policy.timeout_seconds if external_states.get(authority) == AVAILABLE else min(policy.timeout_seconds, ACTIVITY_GRACE_SECONDS)
        continuation = {
            "next_action": "refresh-reviewer",
            "retry_at": (
                created_at + timedelta(seconds=delay)
            ).isoformat(),
        }
    elif authority in CODING_REVIEWERS:
        continuation["next_action"] = "await-authoritative-review"
    return {
        "pr": int(pr["number"]),
        "url": pr["url"],
        "head": head,
        "reviewer": authority,
        "reviewer_identity": reviewer_identity,
        "reviewer_actor": reviewer_actor,
        "review_required": risk_tier >= 2,
        "risk_tier": risk_tier,
        **continuation,
    }

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int)
    for name in ("--title", "--body", "--body-file", "--agent", "--author-family", "--author-github-login"):
        parser.add_argument(name)
    parser.add_argument("--refresh-reviewer", type=int, metavar="PR")
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
                "review policy: CodeRabbit-first "
                f"{', '.join(policy['external_reviewers']) or 'none'}; "
                "coding fallback "
                f"{', '.join(policy['coding_fallbacks']) or 'none'} "
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
                ),
            )
            result = refresh_assignment(
                args.refresh_reviewer,
                coding_unavailable_reason=args.coding_reviewer_unavailable,
            )
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
    elif args.refresh_reviewer:
        print(f"review authority: {result['authority']} ({result['reason']})")
    else:
        print(plain_output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
