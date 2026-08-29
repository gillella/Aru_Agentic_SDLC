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


def epic_close_policy(body: str) -> str:
    policies = re.findall(r"(?im)^\s*epic-close-policy:\s*(\S+)\s*$", body or "")
    if not policies:
        raise KernelError("epic-close-policy is missing")
    if len(policies) > 1:
        raise KernelError("epic-close-policy is ambiguous")
    return policies[0]


def parse_child_issues(body: str) -> list[int]:
    sections = re.findall(
        r"(?ims)^##\s+Child Issues\s*$\n(.*?)(?=^##\s+|\Z)",
        body or "",
    )
    if not sections:
        raise KernelError("epic must contain a ## Child Issues section")
    if len(sections) > 1:
        raise KernelError("epic contains more than one ## Child Issues section")
    numbers: list[int] = []
    for line in sections[0].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("-"):
            break
        child = re.fullmatch(r"-\s*#(\d+)\s*", stripped)
        if not child:
            raise KernelError("## Child Issues contains a malformed child reference")
        numbers.append(int(child.group(1)))
    if not numbers:
        raise KernelError("## Child Issues must list at least one child issue")
    if len(numbers) != len(set(numbers)):
        raise KernelError("## Child Issues contains duplicate child references")
    if len(numbers) > MAX_EPIC_CHILDREN:
        raise KernelError(
            f"## Child Issues exceeds the bounded child limit ({MAX_EPIC_CHILDREN})"
        )
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
        raise KernelError(
            f"child snapshot exceeds the bounded child limit ({MAX_EPIC_CHILDREN})"
        )
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


def epic_reconcile_evidence(
    number: int,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    record = issue(number, cwd=cwd)
    labels = label_names(record)
    body = str(record.get("body") or "")
    blockers: list[str] = []
    children: dict[str, dict[str, Any]] = {}

    if "type:epic" not in labels:
        blockers.append("issue is not type:epic")
    if "needs-human" in labels:
        blockers.append("needs-human prevents mechanical epic closure")
    if record.get("state") == "CLOSED":
        blockers.append("epic is already closed")

    for dependency in unresolved_dependencies(record, cwd=cwd):
        blockers.append(f"open depends-on: #{dependency}")

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

    try:
        epic_status = status_of(record)
    except KernelError as exc:
        blockers.append(str(exc))
        epic_status = None

    return {
        "issue": number,
        "closable": not blockers,
        "blocked": bool(blockers),
        "blockers": blockers,
        "children": children,
        "status": epic_status,
        "state": record.get("state"),
    }


def _rollback_epic_reconciliation(
    number: int,
    *,
    before_status: str | None,
    before_state: str,
    closed_issue: bool,
    cwd: str | Path | None = None,
    original: KernelError,
) -> None:
    try:
        if closed_issue and before_state != "CLOSED":
            run(["gh", "issue", "reopen", str(number)], cwd=cwd)
        if before_status != "Done":
            if before_status:
                set_status(number, before_status, cwd=cwd)
            else:
                run(
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(number),
                        "--remove-label",
                        status_label("Done"),
                    ],
                    cwd=cwd,
                )
        # A rollback that "ran" its commands but never settled would silently
        # strand the epic between states, so read every value back.
        record = issue(number, cwd=cwd)
        settled_state = str(record.get("state") or "")
        settled_status = status_of(record)
        settled_project_status = project_item_status(number, cwd=cwd)
        if (
            settled_state != before_state
            or settled_status != before_status
            or settled_project_status != before_status
        ):
            raise KernelError(
                "epic reconciliation rollback did not settle at the pre-transaction "
                "issue state/status and linked Project card status"
            )
    except KernelError as rollback_error:
        raise KernelError(
            f"{rollback_error}; original reconciliation failure: {original}"
        ) from rollback_error


def apply_epic_reconciliation(
    number: int,
    *,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    evidence = epic_reconcile_evidence(number, cwd=cwd)
    if evidence["blocked"]:
        raise KernelError("; ".join(evidence["blockers"]))

    before_status = evidence["status"]
    before_state = str(evidence["state"] or "")
    closed_issue = False
    try:
        set_status(number, "Done", cwd=cwd)
        if before_state != "CLOSED":
            run(["gh", "issue", "close", str(number), "--reason", "completed"], cwd=cwd)
            closed_issue = True
        final = issue(number, cwd=cwd)
        after_status = status_of(final)
        after_state = str(final.get("state") or "")
        after_project_status = project_item_status(number, cwd=cwd)
        if (
            after_status != "Done"
            or after_state != "CLOSED"
            or after_project_status != "Done"
        ):
            raise KernelError(
                "epic reconciliation did not settle at Done and closed on the issue "
                "and linked Project card"
            )
    except KernelError as error:
        _rollback_epic_reconciliation(
            number,
            before_status=before_status,
            before_state=before_state,
            closed_issue=closed_issue,
            cwd=cwd,
            original=error,
        )
        raise
    return {
        "issue": number,
        "applied": True,
        "before": before_status,
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
