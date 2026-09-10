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
    StatusPreconditionError,
    acceptance_items,
    configured_reviewer_family,
    ensure_label,
    gh_json,
    issue,
    label_names,
    normalized_identity,
    repo_slug,
    run,
    same_github_actor,
    set_status,
    project_item_status,
    status_of,
)
from merge_state import linked_issues, pull_request

FULL_SHA = re.compile(r"[0-9a-f]{40}")
RECEIPT_MARKER = "aru-legacy-recovery:v1"
# Trust boundary for historical lineage. Evidence is a canonical co-author
# trailer in a commit GitHub verified and whose authenticated signer is the
# merged PR actor. Display names, free prose and current configuration are all
# operator-supplied text and establish nothing about historical work.
ATTESTED_FAMILY_EMAILS = {
    "noreply@anthropic.com": "claude-code",
    "noreply@openai.com": "openai-codex",
    "noreply@cursor.com": "xai-cursor",
    "noreply@google.com": "google-antigravity",
}
TRAILER_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)$")
TRAILER_EMAIL = re.compile(r"<([^>\s]+)>")


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


def attested_families(message: str) -> set[str]:
    """Families named by canonical co-author trailers in the real terminal block.

    Mirrors ``git interpret-trailers --parse`` without executing git: only the
    message's final paragraph counts, and only when every one of its lines is a
    ``Token: value`` pair. A co-author line quoted in prose or inside a fenced
    block is therefore not a trailer, exactly as git reports it, and any stray
    or folded line makes the whole block non-trailing, which fails closed.
    """
    paragraphs = re.split(r"\n[ \t]*\n", message.replace("\r\n", "\n").rstrip())
    lines = paragraphs[-1].splitlines() if len(paragraphs) > 1 else []
    matches = [TRAILER_LINE.match(line) for line in lines]
    if not matches or any(match is None for match in matches):
        return set()
    addresses = [
        found.group(1).lower()
        for match in matches
        if match.group(1).lower() == "co-authored-by"
        and (found := TRAILER_EMAIL.search(match.group(2)))
    ]
    return {ATTESTED_FAMILY_EMAILS[a] for a in addresses if a in ATTESTED_FAMILY_EMAILS}


def head_commit_evidence(head: str, actor: str) -> str:
    """The single family the verified merged head commit actually attests."""
    obj = gh_json(["api", f"repos/{repo_slug()}/commits/{head}"])
    commit = obj.get("commit") if isinstance(obj, dict) else None
    if not isinstance(obj, dict) or obj.get("sha") != head or not isinstance(commit, dict):
        raise KernelError("historical head commit evidence is unreadable")
    # GitHub verifies the committer's key, and documents that the attributed
    # author may differ, so author.login can never authenticate the signer.
    verification = commit.get("verification")
    if (not isinstance(verification, dict) or verification.get("verified") is not True
            or verification.get("reason") != "valid"):
        raise KernelError("historical head commit signature is not verified by GitHub")
    if not same_github_actor(str((obj.get("committer") or {}).get("login") or ""), actor):
        raise KernelError("the authenticated signer of the head commit is not the merged PR actor")
    families = attested_families(str(commit.get("message") or ""))
    if len(families) != 1:
        raise KernelError("historical head commit lacks exactly one canonical author-family "
                          f"attestation; found {sorted(families) or ['none']}")
    return families.pop()


def resolve_author(
    pr: dict[str, Any],
    record: dict[str, Any],
    declared_identity: str,
    declared_family: str,
    declared_actor: str = "",
) -> dict[str, str]:
    """Bind a declared identity to governed claim evidence and the real actor.

    The declaration never establishes lineage. It must equal the issue's
    recorded claimant and agree with the family the verified merged head commit
    attests; that attestation, not a Git display name or the current reviewer
    configuration, is the evidence of record.
    """
    identity = normalized_identity(declared_identity or "")
    family = normalized_identity(declared_family or "")
    if family not in CODING_REVIEWERS:
        raise KernelError("declared author family is not a canonical coding family")
    claimant = _one_claimant(record)
    if normalized_identity(claimant) != identity:
        raise KernelError("declared author does not match the linked issue's recorded claimant")
    actor = str((pr.get("author") or {}).get("login") or "").strip()
    if not actor:
        raise KernelError("merged PR has no readable GitHub author actor")
    if declared_actor and not same_github_actor(declared_actor, actor):
        raise KernelError("declared GitHub actor does not match the merged PR author")
    attested = head_commit_evidence(str(pr["headRefOid"]), actor)
    if attested != family:
        raise KernelError(
            f"declared author family {family!r} contradicts the attested historical "
            f"family {attested!r}"
        )
    configured = configured_reviewer_family(identity)
    if configured is not None and configured != attested:
        raise KernelError("configured identity family contradicts the attested historical family")
    return {
        "author": identity,
        "author_family": family,
        "author_actor": actor,
        "claimant": claimant,
    }


def agreed_status(record: dict[str, Any], issue_number: int) -> str | None:
    """The status both lifecycle authorities report, or a refusal on disagreement.

    Issue labels alone are not the board. A failed ``set_status`` rollback can
    leave the label ahead of the Project card, so every recovery path - preview,
    apply, replay and the no-op - reads the card before trusting the label.
    """
    labelled = status_of(record)
    try:
        card = project_item_status(issue_number)
    except KernelError as exc:
        raise KernelError(f"linked Project card is unreadable: {exc}") from exc
    if card != labelled:
        raise KernelError(
            f"issue labels report {labelled!r} but the Project card reports {card!r}; "
            "recovery refuses while the lifecycle authorities disagree"
        )
    return labelled


def status_transition(record: dict[str, Any], issue_number: int) -> dict[str, str] | None:
    current = agreed_status(record, issue_number)
    if current == "In Review":
        return None
    if current != "In Progress":
        raise KernelError(f"legacy recovery cannot move a {current!r} issue to In Review")
    return {"from": "In Progress", "to": "In Review"}


def snapshot(pr: dict[str, Any], record: dict[str, Any], issue_number: int) -> dict[str, Any]:
    """Exactly what every pre-write reread must still observe."""
    return {
        "head": pr.get("headRefOid"),
        "state": pr.get("state"),
        "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
        "actor": str((pr.get("author") or {}).get("login") or ""),
        "closes": linked_issues(str(pr.get("body") or "")),
        "issue_state": record.get("state"),
        "claimant": _one_claimant(record),
        "status": agreed_status(record, issue_number),
        "acceptance": acceptance_items(str(record.get("body") or "")),
        "author_labels": existing_author(pr),
    }


def _guard(number: int, issue_number: int, expected: dict[str, Any]) -> None:
    pr = pull_request(number)
    if linked_issues(str(pr.get("body") or "")) != [issue_number]:
        raise KernelError("observed closing issue directive drift; no further write")
    if snapshot(pr, issue(issue_number), issue_number) != expected:
        raise KernelError("observed recovery precondition drift; no further write")


def _write_author_metadata(number: int, identity: str, family: str, expected: dict[str, Any],
                           issue_number: int, receipt: dict[str, list[str]]) -> None:
    author_label, family_label = AUTHOR_PREFIX + identity, AUTHOR_FAMILY_PREFIX + family
    _guard(number, issue_number, expected)
    ensure_label(author_label, color="1d76db", description=f"PR authored by {identity}")
    ensure_label(family_label, color="1d76db", description=f"Author model family: {family}")
    _guard(number, issue_number, expected)
    # Recorded before the transport runs: a lost response is not evidence the
    # server rejected the edit, and an unreadable state is never zero mutation.
    receipt["attempted"].append("author-metadata")
    run(["gh", "pr", "edit", str(number), "--add-label", f"{author_label},{family_label}"])
    settled = gh_json(["pr", "view", str(number), "--json", "number,labels"])
    if not isinstance(settled, dict) or existing_author(settled) != (identity, family):
        raise KernelError("recovered author metadata did not settle")
    receipt["attempted"].remove("author-metadata")
    receipt["applied"].append("author-metadata")


def _apply(number: int, issue_number: int, resolved: dict[str, str], planned: list[str],
           expected: dict[str, Any]) -> dict[str, list[str]]:
    receipt: dict[str, list[str]] = {"applied": [], "attempted": [], "skipped": []}
    identity, family = resolved["author"], resolved["author_family"]
    try:
        if "author-metadata" in planned:
            _write_author_metadata(number, identity, family, expected, issue_number, receipt)
            expected = {**expected, "author_labels": (identity, family)}
        else:
            receipt["skipped"].append("author-metadata")
        if "status" in planned:
            settled = expected
            _guard(number, issue_number, settled)
            try:
                set_status(
                    issue_number,
                    "In Review",
                    expected_current="In Progress",
                    pre_mutation_check=lambda: _guard(number, issue_number, settled),
                )
            except StatusPreconditionError:
                raise  # zero-rollback precondition failure: the board did not move
            except KernelError:
                # set_status can fail after the label moved and rollback failed.
                receipt["attempted"].append("status")
                raise
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
        f"issue #{result['issue']}. Audit evidence only: not a review, an approval, "
        "acceptance of any criterion, or a lifecycle authority.\n\n"
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
    transition = status_transition(record, issue_number)
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
        receipt = _apply(number, issue_number, resolved, planned,
                         snapshot(pr, record, issue_number))
    except LegacyRecoveryError as exc:
        result.update(exc.receipt)
        result["action"] = (
            "partial" if exc.receipt["applied"] or exc.receipt["attempted"] else "refused"
        )
        raise LegacyRecoveryError(str(exc), result) from exc
    result.update(receipt)
    result["action"] = "recovered"
    try:
        _post_receipt(number, result)
    except KernelError as exc:
        result["receipt_comment"] = f"unavailable: {exc}"
        raise LegacyRecoveryError(
            f"recovery writes completed but the audit receipt comment failed: {exc}", result
        ) from exc
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


# Arguments that belong to a different public mode. Creation is the default mode
# and is identified by the absence of every mode flag below.
CREATION_ARGS = ("title", "body", "body_file")
MODE_FORBIDDEN = {
    "recover_legacy": CREATION_ARGS + ("coding_reviewer_unavailable",),
    "refresh_reviewer": CREATION_ARGS + ("issue", "agent", "author_family", "author_github_login"),
    "reviewer_status": CREATION_ARGS + ("issue", "author_family", "coding_reviewer_unavailable"),
}


def require_exclusive_mode(args: Any) -> None:
    """Refuse incompatible public modes before any read or write happens."""
    selected = [name for name in MODE_FORBIDDEN if getattr(args, name, None)]
    if len(selected) > 1:
        raise KernelError(
            f"create_pr.py modes are mutually exclusive; got {', '.join(sorted(selected))}"
        )
    if selected[:1] != ["recover_legacy"] and (args.expected_head or args.apply):
        raise KernelError("--expected-head and --apply require --recover-legacy")
    if selected and any(getattr(args, name, None) for name in MODE_FORBIDDEN[selected[0]]):
        flag = selected[0].replace("_", "-")
        raise KernelError(f"--{flag} cannot include another mode's arguments")


def recovery_failure(exc: LegacyRecoveryError, as_json: bool) -> str:
    """Operator-visible report of everything a failed recovery already wrote."""
    receipt = exc.receipt
    if as_json:
        return json.dumps({"error": str(exc), **receipt}, indent=2, sort_keys=True, default=str)
    return "\n".join([
        f"legacy recovery {receipt.get('action', 'refused')}: {exc}",
        f"  confirmed writes: {receipt.get('applied') or 'none'}",
        f"  attempted, outcome unknown: {receipt.get('attempted') or 'none'}",
        f"  not attempted: {receipt.get('skipped') or 'none'}",
    ])
