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
    records = gh_json(["label", "list", "--limit", "100", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("reviewer registration labels are unavailable")
    names = {str(item.get("name") or "") for item in records}
    return {
        service: AVAILABLE if REVIEW_PREFIX + service in names else UNAVAILABLE
        for service in EXTERNAL_REVIEWERS
    }


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
    rotation_key: int,
    runner: ProbeRunner = _default_probe,
) -> tuple[str, str] | None:
    author_identity = normalized_identity(author_identity)
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
            if identities:
                return family, identities[rotation_key % len(identities)]
            continue

        command_names = {
            "openai-codex": "codex",
            "xai-cursor": "cursor-agent",
            "google-antigravity": "agy",
        }
        identity = family
        executable = _command(command_names[family])
        if identity == author_identity or executable is None:
            continue
        arguments = {
            "openai-codex": [executable, "exec", "--skip-git-repo-check", PROBE_PROMPT],
            "xai-cursor": [executable, "-p", PROBE_PROMPT],
            "google-antigravity": [executable, "-p", PROBE_PROMPT],
        }[family]
        if _probe_ok(runner(arguments)):
            return family, identity
    return None


def choose_initial_reviewer(
    number: int,
    author_identity: str,
    author_family: str,
    *,
    external_states: dict[str, str] | None = None,
    probe_runner: ProbeRunner = _default_probe,
) -> tuple[str, str | None]:
    external = initial_external(external_states or registered_external_states())
    if external:
        return external, None
    coding = probe_coding_reviewer(
        author_identity=author_identity,
        author_family=author_family,
        rotation_key=number,
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


def _external_activity_state(
    records: list[dict[str, Any]], service: str
) -> tuple[bool, str | None]:
    activity = False
    for record in records:
        if not isinstance(record, dict):
            raise KernelError("external reviewer evidence is malformed")
        if not _trusted_external_actor(record, service):
            continue
        activity = True
        if UNAVAILABLE_RE.search(str(record.get("body") or "")):
            return True, UNAVAILABLE
        if str(record.get("state") or "").upper() in {"APPROVED", "CHANGES_REQUESTED"}:
            return True, AVAILABLE
    return activity, None


def external_state(
    pr: dict[str, Any],
    service: str,
    *,
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> str:
    activity, evidence_state = _external_activity_state([*reviews, *comments], service)
    if evidence_state:
        return evidence_state

    checks = pr.get("statusCheckRollup")
    if not isinstance(checks, list):
        raise KernelError("external review check state is incomplete")
    matching = [item for item in checks if isinstance(item, dict) and _check_service(item, service)]
    if len(matching) > 1:
        raise KernelError("external reviewer returned ambiguous checks")
    if matching:
        state = str(matching[0].get("conclusion") or matching[0].get("state") or "").upper()
        status = str(matching[0].get("status") or "").upper()
        if state == "SUCCESS":
            return AVAILABLE
        if state in {"ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "SKIPPED"}:
            return UNAVAILABLE
        if state in {"FAILURE", "NEUTRAL"}:
            return AVAILABLE
        if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"}:
            return PENDING
    return PENDING if activity or not matching else PENDING


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise KernelError("review assignment timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise KernelError("review assignment timestamp has no timezone")
    return parsed


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
    if authorities[0] in CODING_REVIEWERS and len(reviewer_identities) != 1:
        raise KernelError("coding authority requires exactly one reviewer identity")
    if authorities[0] in EXTERNAL_REVIEWERS and reviewer_identities:
        raise KernelError("external authority conflicts with coding reviewer identity")
    return authorities[0]


def _one_label_value(pr: dict[str, Any], prefix: str) -> str:
    values = [name[len(prefix) :] for name in label_names(pr) if name.startswith(prefix)]
    if len(values) != 1:
        raise KernelError(f"PR must have exactly one {prefix} identity label")
    return values[0]


def replace_authority(
    number: int,
    pr: dict[str, Any],
    authority: str,
    reviewer_identity: str,
) -> None:
    review_label = REVIEW_PREFIX + authority
    reviewer_label = REVIEWER_PREFIX + reviewer_identity
    ensure_label(review_label, color="5319e7", description=f"Coding review: {authority}")
    ensure_label(reviewer_label, color="5319e7", description=f"Assigned reviewer: {reviewer_identity}")
    retained = [
        name
        for name in label_names(pr)
        if not name.startswith(REVIEW_PREFIX) and not name.startswith(REVIEWER_PREFIX)
    ]
    arguments = ["api", "--method", "PUT", f"repos/{repo_slug()}/issues/{number}/labels"]
    for name in [*retained, review_label, reviewer_label]:
        arguments.extend(["-f", f"labels[]={name}"])
    gh_json(arguments)


def refresh_assignment(
    number: int,
    *,
    now: datetime | None = None,
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
        return {"pr": number, "authority": authority, "action": "retained", "reason": "coding-agent-assigned"}

    slug = repo_slug()
    reviews = gh_paginated(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    comments = gh_paginated(f"repos/{slug}/issues/{number}/comments?per_page=100")
    state = external_state(pr, authority, reviews=reviews, comments=comments)
    observed_at = now or datetime.now(timezone.utc)
    assigned_at = _parse_time(str(pr.get("createdAt") or ""))
    age_seconds = (observed_at - assigned_at).total_seconds()
    if age_seconds < 0:
        raise KernelError("review observation predates assignment")
    if state == AVAILABLE:
        return {"pr": number, "authority": authority, "action": "retained", "reason": "external-available"}
    if state == PENDING and age_seconds < EXTERNAL_TIMEOUT_SECONDS:
        return {
            "pr": number,
            "authority": authority,
            "action": "retained",
            "reason": "external-pending",
            "remaining_seconds": EXTERNAL_TIMEOUT_SECONDS - int(age_seconds),
        }

    author_identity = _one_label_value(pr, AUTHOR_PREFIX)
    author_family = _one_label_value(pr, AUTHOR_FAMILY_PREFIX)
    coding = probe_coding_reviewer(
        author_identity=author_identity,
        author_family=author_family,
        rotation_key=number,
        runner=probe_runner,
    )
    if coding is None:
        raise KernelError("no distinct coding-agent reviewer has available capacity")
    coding_family, reviewer_identity = coding
    reason = "external-unavailable" if state == UNAVAILABLE else "external-pending-15m"
    replace_authority(number, pr, coding_family, reviewer_identity)
    status = {
        "head": pr.get("headRefOid"),
        "observed_at": observed_at.isoformat(),
        "previous_authority": authority,
        "reason": reason,
        "reviewer": reviewer_identity,
        "reviewer_family": coding_family,
    }
    body = (
        "## Aru authoritative reviewer fallback\n\n"
        f"Assigned `{reviewer_identity}` ({coding_family}) because `{authority}` was "
        f"{reason.replace('-', ' ')}.\n\n"
        f"<!-- aru-review-assignment:v1 {json.dumps(status, sort_keys=True)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])
    updated = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if _one_authority(updated) != coding_family:
        raise KernelError("review authority replacement was not confirmed")
    if _one_label_value(updated, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("reviewer identity replacement was not confirmed")
    return {
        "pr": number,
        "authority": coding_family,
        "reviewer": reviewer_identity,
        "action": "fallback",
        "reason": reason,
    }


def create(
    number: int,
    title: str,
    body: str,
    agent: str | None = None,
    *,
    author_family: str | None = None,
    external_states: dict[str, str] | None = None,
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
    authority, reviewer_identity = choose_initial_reviewer(
        number,
        owner_identity,
        family,
        external_states=external_states,
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
        label_metadata[reviewer_label] = ("5319e7", f"Assigned reviewer: {reviewer_identity}")
        labels.append(reviewer_label)
    for label, (color, description) in label_metadata.items():
        ensure_label(label, color=color, description=description)
    final_body = body.rstrip() + f"\n\nCloses #{number}\n"
    arguments = ["gh", "pr", "create", "--title", title, "--body", final_body]
    for label in labels:
        arguments.extend(["--label", label])
    run(arguments)
    pr = gh_json(["pr", "view", branch, "--json", "number,url,headRefOid,labels"])
    if pr.get("headRefOid") != head:
        raise KernelError("created PR is not bound to the published head")
    if _one_authority(pr) != authority:
        raise KernelError("created PR does not have exactly one assigned reviewer")
    if reviewer_identity and _one_label_value(pr, REVIEWER_PREFIX) != reviewer_identity:
        raise KernelError("created PR does not identify the coding-agent reviewer")
    set_status(number, "In Review")
    return {
        "pr": int(pr["number"]),
        "url": pr["url"],
        "head": head,
        "reviewer": authority,
        "reviewer_identity": reviewer_identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int)
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--agent")
    parser.add_argument("--author-family")
    parser.add_argument("--refresh-reviewer", type=int, metavar="PR")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.refresh_reviewer:
            if any((args.issue, args.title, args.body, args.agent, args.author_family)):
                raise KernelError("review refresh cannot include PR creation arguments")
            result = refresh_assignment(args.refresh_reviewer)
        else:
            if args.issue is None or args.title is None or args.body is None:
                raise KernelError("--issue, --title, and --body are required for PR creation")
            result = create(
                args.issue,
                args.title,
                args.body,
                args.agent,
                author_family=args.author_family,
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
