"""Bounded operator recovery of legacy merged-PR author provenance.

The default is a read-only preview. Mutation requires an explicit apply plus
the exact full historical head, and restores only the truthful author identity
and family a confirmed direct merge already earned, plus the single CLOSED
``In Progress`` -> CLOSED ``In Review`` lifecycle step the finalizer requires.

It never approves, ticks acceptance, edits an issue body, removes a claim,
reopens or recreates history, or marks anything Done. Reviewer selection stays
exclusively in ``create_pr.py --refresh-reviewer``. Its receipt is audit
evidence, not a second lifecycle authority.
"""

from __future__ import annotations

import json
import re
from typing import Any

from check_ci import finalization_verdict
from common import (
    AGENT_PREFIX,
    AUTHOR_FAMILY_PREFIX,
    AUTHOR_PREFIX,
    CODING_REVIEWERS,
    KernelError,
    acceptance_items,
    agent_family,
    ensure_label,
    gh_json,
    issue,
    label_names,
    normalized_identity,
    repo_slug,
    run,
    same_github_actor,
    set_status,
    status_of,
)
from merge_state import linked_issues, pull_request

FULL_SHA = re.compile(r"[0-9a-f]{40}")
RECEIPT_MARKER = "aru-legacy-recovery:v1"
# Historical authorship hints, used only to refuse a declaration the merged
# head contradicts. They never select or invent a family on their own.
FAMILY_EVIDENCE = {
    "claude-code": ("claude-code", "claude", "anthropic"),
    "openai-codex": ("openai-codex", "codex", "openai"),
    "xai-cursor": ("xai-cursor", "cursor", "xai"),
    "google-antigravity": ("google-antigravity", "antigravity", "gemini"),
}


class LegacyRecoveryError(KernelError):
    """A refusal that carries the receipt of every write already applied."""

    def __init__(self, message: str, receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.receipt = receipt


def label_values(record: dict[str, Any], prefix: str) -> list[str]:
    """Every value carried by ``prefix`` labels on an issue or PR record."""
    return [name[len(prefix) :] for name in label_names(record) if name.startswith(prefix)]


def one_label_value(record: dict[str, Any], prefix: str) -> str:
    """The single ``prefix`` value a governed record must carry, or a refusal."""
    values = label_values(record, prefix)
    if len(values) != 1:
        raise KernelError(f"PR must have exactly one {prefix} identity label")
    return values[0]


def _one_claimant(record: dict[str, Any]) -> str:
    owners = label_values(record, AGENT_PREFIX)
    if len(owners) != 1:
        raise KernelError("linked issue does not have one exclusive claimant")
    return owners[0]


def existing_author(pr: dict[str, Any]) -> tuple[str | None, str | None]:
    identities = label_values(pr, AUTHOR_PREFIX)
    families = label_values(pr, AUTHOR_FAMILY_PREFIX)
    if len(identities) > 1 or len(families) > 1:
        raise KernelError("merged PR has contradictory author metadata")
    return (identities[0] if identities else None, families[0] if families else None)


def merged_pull_request(number: int, expected_head: str) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(expected_head or ""):
        raise KernelError("recovery requires the exact full 40-character historical head")
    pr = pull_request(number)
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise KernelError("legacy recovery only applies to a confirmed merged PR")
    if pr.get("headRefOid") != expected_head:
        raise KernelError("expected head does not match the merged PR head")
    commit = pr.get("mergeCommit")
    if not isinstance(commit, dict) or not FULL_SHA.fullmatch(str(commit.get("oid") or "")):
        raise KernelError("merged PR is missing a readable merge commit")
    return pr


def linked_closed_issue(pr: dict[str, Any], expected_issue: int) -> dict[str, Any]:
    numbers = linked_issues(str(pr.get("body") or ""))
    if len(numbers) != 1:
        raise KernelError("merged PR body must contain exactly one closing issue directive")
    if numbers[0] != expected_issue:
        raise KernelError("merged PR does not close the expected issue")
    record = issue(expected_issue)
    if record.get("state") != "CLOSED":
        raise KernelError("legacy recovery only applies to a closed linked issue")
    return record


def observed_families(text: str) -> set[str]:
    lowered = text.lower()
    return {
        family
        for family, hints in FAMILY_EVIDENCE.items()
        if any(hint in lowered for hint in hints)
    }


def head_commit_evidence(head: str) -> set[str]:
    """Families the merged head commit itself names, for refusal only."""
    obj = gh_json(["api", f"repos/{repo_slug()}/commits/{head}"])
    commit = obj.get("commit") if isinstance(obj, dict) else None
    if not isinstance(obj, dict) or obj.get("sha") != head or not isinstance(commit, dict):
        raise KernelError("historical head commit evidence is unreadable")
    parts = [str(commit.get("message") or "")]
    for side in ("author", "committer"):
        person = commit.get(side)
        if not isinstance(person, dict) or not str(person.get("name") or "").strip():
            raise KernelError("historical head commit authorship is incomplete")
        parts.extend([str(person.get("name")), str(person.get("email") or "")])
    return observed_families("\n".join(parts))


def resolve_author(
    pr: dict[str, Any],
    record: dict[str, Any],
    declared_identity: str,
    declared_family: str,
    declared_actor: str = "",
) -> dict[str, str]:
    """Bind a declared identity to governed claim evidence and the real actor.

    A Git author name never selects the family on its own: the declaration must
    equal the issue's recorded claimant, resolve to a known coding family, and
    stay uncontradicted by the merged head commit.
    """
    identity = normalized_identity(declared_identity or "")
    family = normalized_identity(declared_family or "")
    if family not in CODING_REVIEWERS or agent_family(identity) != family:
        raise KernelError("declared author family is unknown or not canonical for that identity")
    claimant = _one_claimant(record)
    if normalized_identity(claimant) != identity:
        raise KernelError("declared author does not match the linked issue's recorded claimant")
    actor = str((pr.get("author") or {}).get("login") or "").strip()
    if not actor:
        raise KernelError("merged PR has no readable GitHub author actor")
    if declared_actor and not same_github_actor(declared_actor, actor):
        raise KernelError("declared GitHub actor does not match the merged PR author")
    observed = head_commit_evidence(str(pr["headRefOid"]))
    if observed != {family}:
        raise KernelError(
            "historical head authorship evidence is missing or contradicts the declared family "
            f"{family!r}; observed {sorted(observed) or ['none']}"
        )
    return {
        "author": identity,
        "author_family": family,
        "author_actor": actor,
        "claimant": claimant,
    }


def status_transition(record: dict[str, Any]) -> dict[str, str] | None:
    current = status_of(record)
    if current == "In Review":
        return None
    if current != "In Progress":
        raise KernelError(f"legacy recovery cannot move a {current!r} issue to In Review")
    return {"from": "In Progress", "to": "In Review"}


def snapshot(pr: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Exactly what every pre-write reread must still observe."""
    return {
        "head": pr.get("headRefOid"),
        "state": pr.get("state"),
        "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
        "actor": str((pr.get("author") or {}).get("login") or ""),
        "issue_state": record.get("state"),
        "claimant": _one_claimant(record),
        "status": status_of(record),
        "acceptance": acceptance_items(str(record.get("body") or "")),
        "author_labels": existing_author(pr),
    }


def _guard(number: int, issue_number: int, expected: dict[str, Any]) -> None:
    if snapshot(pull_request(number), issue(issue_number)) != expected:
        raise KernelError("observed recovery precondition drift; no further write")


def _write_author_metadata(number: int, identity: str, family: str, expected: dict[str, Any],
                           issue_number: int) -> None:
    author_label, family_label = AUTHOR_PREFIX + identity, AUTHOR_FAMILY_PREFIX + family
    _guard(number, issue_number, expected)
    ensure_label(author_label, color="1d76db", description=f"PR authored by {identity}")
    ensure_label(family_label, color="1d76db", description=f"Author model family: {family}")
    _guard(number, issue_number, expected)
    run(["gh", "pr", "edit", str(number), "--add-label", f"{author_label},{family_label}"])
    settled = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if not isinstance(settled, dict) or existing_author(settled) != (identity, family):
        raise KernelError("recovered author metadata did not settle")


def _apply(number: int, issue_number: int, resolved: dict[str, str], planned: list[str],
           expected: dict[str, Any]) -> dict[str, list[str]]:
    receipt: dict[str, list[str]] = {"applied": [], "skipped": []}
    identity, family = resolved["author"], resolved["author_family"]
    try:
        if "author-metadata" in planned:
            _write_author_metadata(number, identity, family, expected, issue_number)
            receipt["applied"].append("author-metadata")
            expected = {**expected, "author_labels": (identity, family)}
        else:
            receipt["skipped"].append("author-metadata")
        if "status" in planned:
            settled = expected
            _guard(number, issue_number, settled)
            set_status(
                issue_number,
                "In Review",
                expected_current="In Progress",
                pre_mutation_check=lambda: _guard(number, issue_number, settled),
            )
            receipt["applied"].append("status")
        else:
            receipt["skipped"].append("status")
    except KernelError as exc:
        raise LegacyRecoveryError(str(exc), receipt) from exc
    return receipt


def _post_receipt(number: int, result: dict[str, Any]) -> None:
    body = (
        "## Aru legacy provenance recovery\n\n"
        f"Recovered historical author metadata for merged head `{result['head']}` and linked "
        f"issue #{result['issue']}. This is audit evidence only: it is not a review, an "
        "approval, acceptance of any criterion, or a lifecycle authority.\n\n"
        f"<!-- {RECEIPT_MARKER} {json.dumps(result, sort_keys=True, default=str)} -->"
    )
    run(["gh", "pr", "comment", str(number), "--body", body])


def recover_legacy_provenance(
    number: int,
    *,
    issue_number: int,
    expected_head: str,
    agent: str,
    author_family: str,
    author_actor: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    pr = merged_pull_request(number, expected_head)
    record = linked_closed_issue(pr, issue_number)
    verdict = finalization_verdict(pr)
    if verdict.get("head") != expected_head or verdict.get("state") != "success":
        raise KernelError("merged provenance or historical governed CI is not proven")
    resolved = resolve_author(pr, record, agent, author_family, author_actor)
    transition = status_transition(record)
    target = (resolved["author"], resolved["author_family"])
    current = existing_author(pr)
    if current != (None, None) and current != target:
        raise KernelError("existing author metadata conflicts with the recovered provenance")
    planned = [name for name, needed in
               (("author-metadata", current != target), ("status", bool(transition))) if needed]
    items = acceptance_items(str(record.get("body") or ""))
    result: dict[str, Any] = {
        "pr": number,
        "issue": issue_number,
        "head": expected_head,
        "merge_commit": pr["mergeCommit"]["oid"],
        "ci": verdict["state"],
        **resolved,
        "status_transition": transition,
        "acceptance": {
            "total": len(items),
            "incomplete": sum(1 for done, _ in items if not done),
        },
        "planned": planned,
        "mode": "apply" if apply else "preview",
        "applied": [],
        "skipped": [],
    }
    if not apply:
        result["action"] = "preview"
        return result
    if not planned:
        result["action"] = "already-recovered"
        return result
    try:
        receipt = _apply(number, issue_number, resolved, planned, snapshot(pr, record))
    except LegacyRecoveryError as exc:
        result.update(exc.receipt)
        result["action"] = "partial" if exc.receipt["applied"] else "refused"
        raise LegacyRecoveryError(str(exc), result) from exc
    result.update(receipt)
    result["action"] = "recovered"
    _post_receipt(number, result)
    return result


def recover_from_args(args: Any) -> dict[str, Any]:
    """Bind ``create_pr.py``'s parsed operator arguments to one recovery call."""
    if args.issue is None:
        raise KernelError("legacy recovery requires the linked --issue")
    return recover_legacy_provenance(
        args.recover_legacy,
        issue_number=args.issue,
        expected_head=args.expected_head or "",
        agent=args.agent or "",
        author_family=args.author_family or "",
        author_actor=args.author_github_login or "",
        apply=args.apply,
    )
