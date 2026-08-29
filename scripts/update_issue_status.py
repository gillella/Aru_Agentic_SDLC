#!/usr/bin/env python3
"""Move one issue between the five kernel statuses."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from common import (
    REPOSITORY_AUTH,
    KernelError,
    STATUSES,
    StatusPreconditionError,
    board_edit,
    ensure_label,
    gh_json,
    issue,
    json_print,
    label_names,
    project_item_status,
    repo_slug,
    run,
    set_status,
    status_label,
    status_of,
    unresolved_dependencies,
)

MAX_EPIC_CHILDREN = 50
EPIC_CLOSE_POLICY_MANUAL = "manual"
EPIC_CLOSE_POLICY_CHILDREN_ONLY = "children-only"


_EPIC_POLICY_LINE_RE = re.compile(r"(?i)^epic-close-policy:\s*(\S+)\s*$")
_DEPENDS_ON_LINE_RE = re.compile(r"(?i)^depends-on:\s*#\d+\s*$")
_CHILD_LINE_RE = re.compile(r"^-\s*#(\d+)\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")


def _is_indented_code(raw: str) -> bool:
    return raw[:4] == "    " or raw[:1] == "\t"


def _scan_child_issue_sections(body: str) -> list[list[str]]:
    """Collect the body lines under each top-level ``## Child Issues`` heading.

    Fenced code (``` / ~~~, including never-closed fences), blockquotes, and
    indented code can neither open a section nor contribute lines to one, so a
    ``## Child Issues`` example embedded in prose can never authorise mechanical
    closure.
    """
    sections: list[list[str]] = []
    current: list[str] | None = None
    fence: str | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        fence_hit = _FENCE_RE.match(stripped)
        if fence is not None:
            if current is not None:
                current.append(raw)
            if (
                fence_hit is not None
                and stripped == fence_hit.group(1)
                and fence_hit.group(1)[0] == fence[0]
                and len(fence_hit.group(1)) >= len(fence)
            ):
                fence = None
            continue
        if fence_hit is not None:
            fence = fence_hit.group(1)
            if current is not None:
                current.append(raw)
            continue
        heading = (
            None
            if stripped.startswith(">") or _is_indented_code(raw)
            else _ATX_HEADING_RE.match(raw)
        )
        if heading is not None and len(heading.group(1)) == 2:
            if heading.group(2).strip().lower() == "child issues":
                current = []
                sections.append(current)
            else:
                current = None
        elif current is not None:
            current.append(raw)
    return sections


def _child_issues_section(body: str) -> str:
    sections = _scan_child_issue_sections(body or "")
    if not sections:
        raise KernelError("epic must contain a ## Child Issues section")
    if len(sections) > 1:
        raise KernelError("epic contains more than one ## Child Issues section")
    return "\n".join(sections[0])


def _parse_child_section_elements(section: str) -> tuple[list[int], list[str]]:
    numbers: list[int] = []
    policies: list[str] = []
    trailer_started = False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            if numbers:
                trailer_started = True
            continue
        if trailer_started:
            if stripped.startswith("-"):
                raise KernelError("## Child Issues contains an interrupted child list")
            policy_match = _EPIC_POLICY_LINE_RE.fullmatch(stripped)
            if policy_match:
                policies.append(policy_match.group(1))
                continue
            if _DEPENDS_ON_LINE_RE.fullmatch(stripped):
                continue
            raise KernelError("## Child Issues contains unexpected trailer content")
        if not stripped.startswith("-"):
            raise KernelError("## Child Issues contains unexpected content")
        child_match = _CHILD_LINE_RE.fullmatch(stripped)
        if not child_match:
            raise KernelError("## Child Issues contains a malformed child reference")
        numbers.append(int(child_match.group(1)))
    return numbers, policies


def epic_close_policy(body: str) -> str:
    section = _child_issues_section(body)
    _numbers, policies = _parse_child_section_elements(section)
    if not policies:
        raise KernelError("epic-close-policy is missing")
    if len(policies) > 1:
        raise KernelError("epic-close-policy is ambiguous")
    return policies[0]


def parse_child_issues(body: str) -> list[int]:
    section = _child_issues_section(body)
    numbers, _policies = _parse_child_section_elements(section)
    if not numbers:
        raise KernelError("## Child Issues must list at least one child issue")
    if len(numbers) != len(set(numbers)):
        raise KernelError("## Child Issues contains duplicate child references")
    if len(numbers) > MAX_EPIC_CHILDREN:
        raise KernelError(f"## Child Issues exceeds the bounded child limit ({MAX_EPIC_CHILDREN})")
    return sorted(numbers)


def _child_snapshot_labels(number: int, node: dict[str, Any]) -> list[dict[str, str]]:
    labels_connection = node.get("labels")
    if not isinstance(labels_connection, dict):
        raise KernelError(f"child #{number} label inventory is malformed")
    page_info = labels_connection.get("pageInfo")
    if not isinstance(page_info, dict) or page_info.get("hasNextPage") is not False:
        raise KernelError(f"child #{number} label inventory is truncated")
    label_nodes = labels_connection.get("nodes")
    if not isinstance(label_nodes, list):
        raise KernelError(f"child #{number} label inventory is malformed")
    labels: list[dict[str, str]] = []
    for item in label_nodes:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise KernelError(f"child #{number} label inventory is malformed")
        labels.append({"name": item["name"]})
    return labels


def _child_snapshot_from_node(number: int, node: Any) -> dict[str, Any] | None:
    if node is None:
        return None
    if not isinstance(node, dict):
        raise KernelError(f"child #{number} snapshot is malformed")
    if node.get("number") != number:
        raise KernelError(f"child #{number} snapshot number does not match the request")
    state = node.get("state")
    if state not in ("OPEN", "CLOSED"):
        raise KernelError(f"child #{number} has an unsupported state")
    repo = node.get("repository") if isinstance(node.get("repository"), dict) else {}
    return {
        "number": number,
        "state": state,
        "labels": _child_snapshot_labels(number, node),
        "repository": repo.get("nameWithOwner"),
    }


def child_issue_snapshots(
    numbers: list[int],
    *,
    cwd: str | Path | None = None,
) -> dict[int, dict[str, Any]]:
    if not numbers or len(numbers) > MAX_EPIC_CHILDREN:
        raise KernelError(f"child snapshot exceeds the bounded child limit ({MAX_EPIC_CHILDREN})")
    owner, name = repo_slug(cwd).split("/", 1)
    fields = "\n".join(
        f"i{n}: issue(number: {n}) {{ number state repository {{ nameWithOwner }} "
        f"labels(first: 20) {{ nodes {{ name }} pageInfo {{ hasNextPage }} }} }}"
        for n in numbers
    )
    query = (
        f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
        f"{{ {fields} }} }}"
    )
    data = gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
        ],
        cwd=cwd,
        auth=REPOSITORY_AUTH,
    )
    if not isinstance(data, dict) or data.get("errors"):
        raise KernelError("child issue snapshot returned a GraphQL error")
    root = data.get("data")
    if not isinstance(root, dict) or "repository" not in root:
        raise KernelError("child issue snapshot is unavailable")
    repository = root.get("repository")
    if not isinstance(repository, dict):
        raise KernelError("child issue snapshot repository is unavailable")
    snapshots: dict[int, dict[str, Any]] = {}
    for number in numbers:
        snapshot = _child_snapshot_from_node(number, repository.get(f"i{number}"))
        if snapshot is not None:
            snapshots[number] = snapshot
    return snapshots


def _child_reconcile_blockers(
    number: int,
    snapshot: dict[str, Any],
    *,
    repository: str,
) -> list[str]:
    blockers: list[str] = []
    if snapshot.get("repository") != repository:
        blockers.append(f"child #{number} is not in this repository")
        return blockers
    if snapshot.get("state") != "CLOSED":
        blockers.append(f"child #{number} is not closed")
        return blockers
    try:
        child_status = status_of(snapshot)
    except KernelError as exc:
        blockers.append(f"child #{number}: {exc}")
        return blockers
    if child_status != "Done":
        blockers.append(f"child #{number} is not status:done")
    return blockers


def _child_reconcile_evidence(
    child_numbers: list[int],
    *,
    cwd: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    children: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    try:
        snapshots = child_issue_snapshots(child_numbers, cwd=cwd)
        repository = repo_slug(cwd)
        for child_number in child_numbers:
            snapshot = snapshots.get(child_number)
            if snapshot is None:
                blockers.append(f"child #{child_number} is missing from this repository")
                continue
            child_blockers = _child_reconcile_blockers(
                child_number,
                snapshot,
                repository=repository,
            )
            blockers.extend(child_blockers)
            child_status = None
            if not child_blockers:
                try:
                    child_status = status_of(snapshot)
                except KernelError as exc:
                    blockers.append(f"child #{child_number}: {exc}")
            children[str(child_number)] = {
                "number": child_number,
                "state": snapshot.get("state"),
                "status": child_status,
                "repository": snapshot.get("repository"),
            }
    except KernelError as exc:
        blockers.append(str(exc))
    return children, blockers


def _epic_policy_and_children(
    body: str,
    *,
    cwd: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    blockers: list[str] = []
    children: dict[str, dict[str, Any]] = {}
    policy: str | None = None
    try:
        policy = epic_close_policy(body)
    except KernelError as exc:
        blockers.append(str(exc))
    else:
        if policy == EPIC_CLOSE_POLICY_MANUAL:
            blockers.append("epic-close-policy: manual prevents mechanical closure")
        elif policy != EPIC_CLOSE_POLICY_CHILDREN_ONLY:
            blockers.append(f"unsupported epic-close-policy: {policy}")

    child_numbers: list[int] = []
    try:
        child_numbers = parse_child_issues(body)
    except KernelError as exc:
        blockers.append(str(exc))

    if child_numbers and policy == EPIC_CLOSE_POLICY_CHILDREN_ONLY:
        children, child_blockers = _child_reconcile_evidence(child_numbers, cwd=cwd)
        blockers.extend(child_blockers)
    return children, blockers


def _epic_status_evidence(
    number: int,
    record: dict[str, Any],
    *,
    cwd: str | Path | None = None,
) -> tuple[str | None, str | None, list[str]]:
    blockers: list[str] = []
    epic_status = None
    try:
        epic_status = status_of(record)
    except KernelError as exc:
        blockers.append(str(exc))

    epic_project_status = None
    try:
        epic_project_status = project_item_status(number, cwd=cwd)
    except KernelError as exc:
        blockers.append(str(exc))

    if epic_status is None:
        if not any(
            "contradictory status labels" in b or "unsupported status label" in b
            for b in blockers
        ):
            blockers.append("epic status label is missing")
    elif epic_status != "Backlog":
        blockers.append(f"epic status label is not Backlog ({epic_status})")

    if epic_project_status is None:
        if not any(
            "Project Board" in b or "GraphQL error" in b or "Project identity" in b
            for b in blockers
        ):
            blockers.append("epic linked Project card Status is unset")
    elif epic_project_status != "Backlog":
        blockers.append(f"epic linked Project card status is not Backlog ({epic_project_status})")

    if (
        epic_status is not None
        and epic_project_status is not None
        and epic_status != epic_project_status
    ):
        blockers.append(
            f"epic issue status ({epic_status}) and linked Project card status "
            f"({epic_project_status}) disagree"
        )
    return epic_status, epic_project_status, blockers


def epic_reconcile_evidence(
    number: int,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    record = issue(number, cwd=cwd)
    labels = label_names(record)
    body = str(record.get("body") or "")
    blockers: list[str] = []

    if "type:epic" not in labels:
        blockers.append("issue is not type:epic")
    if "needs-human" in labels:
        blockers.append("needs-human prevents mechanical epic closure")
    if record.get("state") != "OPEN":
        blockers.append("epic is not open")

    for dependency in unresolved_dependencies(record, cwd=cwd):
        blockers.append(f"open depends-on: #{dependency}")

    children, policy_blockers = _epic_policy_and_children(body, cwd=cwd)
    blockers.extend(policy_blockers)

    epic_status, epic_project_status, status_blockers = _epic_status_evidence(
        number, record, cwd=cwd
    )
    blockers.extend(status_blockers)

    return {
        "issue": number,
        "closable": not blockers,
        "blocked": bool(blockers),
        "blockers": blockers,
        "children": children,
        "status": epic_status,
        "project_status": epic_project_status,
        "state": record.get("state"),
    }


def _rollback_reopen_if_closed(
    number: int,
    record: dict[str, Any] | None,
    *,
    cwd: str | Path | None = None,
    errors: list[KernelError],
) -> None:
    if record is not None and str(record.get("state") or "") == "CLOSED":
        try:
            run(["gh", "issue", "reopen", str(number)], cwd=cwd)
        except KernelError as err:
            errors.append(err)


def _rollback_issue_label(
    number: int,
    record: dict[str, Any] | None,
    *,
    cwd: str | Path | None = None,
    errors: list[KernelError],
) -> None:
    if record is None:
        return
    current_status: str | None = None
    try:
        current_status = status_of(record)
    except KernelError as err:
        errors.append(err)
    if current_status == "Backlog":
        return
    try:
        target = status_label("Backlog")
        ensure_label(target, color="1d76db", description="Board status: Backlog", cwd=cwd)
        args = ["gh", "issue", "edit", str(number), "--add-label", target]
        for name in label_names(record):
            if name.startswith("status:") and name != target:
                args.extend(["--remove-label", name])
        run(args, cwd=cwd)
    except KernelError as err:
        errors.append(err)


def _rollback_project_card(
    number: int,
    *,
    cwd: str | Path | None = None,
    errors: list[KernelError],
) -> None:
    current_project_status: str | None = None
    try:
        current_project_status = project_item_status(number, cwd=cwd)
    except KernelError as err:
        errors.append(err)
    if current_project_status != "Backlog":
        try:
            edit = board_edit(number, "Backlog", cwd=cwd)
            run(["gh", *edit], cwd=cwd)
        except KernelError as err:
            errors.append(err)


def _rollback_settled(
    number: int,
    *,
    cwd: str | Path | None = None,
    errors: list[KernelError],
) -> bool:
    try:
        final_record = issue(number, cwd=cwd)
        settled_state = str(final_record.get("state") or "")
        settled_status = status_of(final_record)
        settled_project_status = project_item_status(number, cwd=cwd)
        return (
            settled_state == "OPEN"
            and settled_status == "Backlog"
            and settled_project_status == "Backlog"
        )
    except KernelError as readback_error:
        errors.append(readback_error)
        return False


def _rollback_epic_reconciliation(
    number: int,
    *,
    cwd: str | Path | None = None,
    original: KernelError,
) -> None:
    errors: list[KernelError] = []
    record: dict[str, Any] | None = None
    try:
        record = issue(number, cwd=cwd)
    except KernelError as err:
        errors.append(err)

    _rollback_reopen_if_closed(number, record, cwd=cwd, errors=errors)
    _rollback_issue_label(number, record, cwd=cwd, errors=errors)
    _rollback_project_card(number, cwd=cwd, errors=errors)

    if not _rollback_settled(number, cwd=cwd, errors=errors):
        details = f"{'; '.join(str(e) for e in errors)}; " if errors else ""
        raise KernelError(
            f"{details}epic reconciliation rollback did not settle at the pre-transaction "
            f"issue state/status and linked Project card status; original reconciliation failure: {original}"
        )


def _closure_invariant_drift(
    record: dict[str, Any],
    evidence: dict[str, Any],
    *,
    cwd: str | Path | None = None,
) -> list[str]:
    labels = label_names(record)
    body = str(record.get("body") or "")
    drift: list[str] = []
    if "type:epic" not in labels:
        drift.append("issue is no longer type:epic")
    if "needs-human" in labels:
        drift.append("needs-human was added during reconciliation")
    try:
        if epic_close_policy(body) != EPIC_CLOSE_POLICY_CHILDREN_ONLY:
            drift.append("epic-close-policy changed from children-only")
    except KernelError as exc:
        drift.append(f"epic-close-policy drift: {exc}")
    try:
        expected_children = sorted(int(key) for key in evidence["children"])
        if parse_child_issues(body) != expected_children:
            drift.append("## Child Issues roster changed")
    except KernelError as exc:
        drift.append(f"## Child Issues drift: {exc}")
    if unresolved_dependencies(record, cwd=cwd):
        drift.append("new open depends-on discovered")
    return drift


def _verify_closure_invariants(
    number: int,
    evidence: dict[str, Any],
    *,
    cwd: str | Path | None = None,
) -> tuple[str | None, str, str | None]:
    """Recollect the epic closure invariants after status+close.

    The parent is now expected CLOSED / Done on both the issue and the linked
    Project card, but the close-policy, ``type:epic`` label, human gate,
    dependency set, and child roster must be unchanged from the pre-mutation
    evidence. Any drift is surfaced so the caller can run authoritative rollback.
    """
    record = issue(number, cwd=cwd)
    after_state = str(record.get("state") or "")
    after_status = status_of(record)
    after_project_status = project_item_status(number, cwd=cwd)
    if after_state != "CLOSED" or after_status != "Done" or after_project_status != "Done":
        raise KernelError(
            "epic reconciliation did not settle at Done and closed on the issue "
            "and linked Project card "
            f"({after_state}/{after_status}/{after_project_status})"
        )
    drift = _closure_invariant_drift(record, evidence, cwd=cwd)
    if drift:
        raise KernelError(
            "epic reconciliation evidence drifted after settlement: " + "; ".join(drift)
        )
    return after_status, after_state, after_project_status


def apply_epic_reconciliation(
    number: int,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    evidence = epic_reconcile_evidence(number, cwd=cwd)
    if evidence["blocked"]:
        raise KernelError("; ".join(evidence["blockers"]))

    def _pre_mutation() -> None:
        """Recollect the bounded evidence immediately before the first mutation."""
        fresh = epic_reconcile_evidence(number, cwd=cwd)
        if not fresh["closable"] or fresh != evidence:
            reasons = fresh["blockers"] or ["epic evidence changed before apply"]
            raise StatusPreconditionError(
                f"epic #{number} evidence drifted before apply: {'; '.join(reasons)}"
            )

    try:
        set_status(
            number,
            "Done",
            expected_current="Backlog",
            pre_mutation_check=_pre_mutation,
            cwd=cwd,
        )
        run(["gh", "issue", "close", str(number), "--reason", "completed"], cwd=cwd)
        after_status, after_state, after_project_status = _verify_closure_invariants(
            number, evidence, cwd=cwd
        )
    except StatusPreconditionError:
        raise
    except KernelError as error:
        _rollback_epic_reconciliation(
            number,
            cwd=cwd,
            original=error,
        )
        raise
    return {
        "issue": number,
        "applied": True,
        "before": evidence["status"],
        "after": after_status,
        "state": after_state,
        "project_status": after_project_status,
        "children": evidence["children"],
    }


def update(number: int, status: str) -> dict[str, object]:
    before = status_of(issue(number))
    set_status(number, status)
    after = status_of(issue(number))
    if after != status:
        raise KernelError(f"status transition did not settle at {status}")
    return {"issue": number, "before": before, "after": after}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument(
        "--reconcile-epic",
        action="store_true",
        help="Check or apply bounded epic reconciliation for one parent epic",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run epic reconciliation without mutation",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.reconcile_epic == bool(args.status):
        parser.error("specify exactly one of --status or --reconcile-epic")
    if args.check and not args.reconcile_epic:
        parser.error("--check requires --reconcile-epic")
    try:
        if args.reconcile_epic:
            result = (
                epic_reconcile_evidence(args.issue)
                if args.check
                else apply_epic_reconciliation(args.issue)
            )
        else:
            result = update(args.issue, args.status)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json or args.reconcile_epic:
        json_print(result)
    else:
        print(f"#{result['issue']}: {result['before'] or 'Unspecified'} -> {result['after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
