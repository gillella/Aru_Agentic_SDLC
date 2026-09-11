#!/usr/bin/env python3
"""Promote complete and unblocked Backlog issues to Ready."""

from __future__ import annotations

import argparse
from typing import Callable

from common import (
    KernelError,
    contract_errors,
    dependencies,
    json_print,
    label_names,
    list_issues,
    set_status,
    unresolved_dependencies,
)


def _priority(record: dict) -> int:
    priorities = {f"priority:p{value}": value for value in range(4)}
    labels = label_names(record)
    priority_labels = [name for name in labels if name.startswith("priority:")]
    if len(priority_labels) > 1 or any(
        name not in priorities for name in priority_labels
    ):
        raise KernelError(
            "issue may have at most one supported priority:p0..p3 label"
        )
    return priorities[priority_labels[0]] if priority_labels else 2


def evaluate(record: dict) -> list[str]:
    return evaluate_with_states(record)


def evaluate_with_states(
    record: dict,
    issue_states: dict[int, str] | None = None,
) -> list[str]:
    errors = contract_errors(record)
    labels = label_names(record)
    if "needs-human" in labels:
        errors.append("needs-human issues cannot enter Ready")
    if "type:epic" in labels:
        errors.append("type:epic issues cannot enter Ready")
    try:
        _priority(record)
    except KernelError as exc:
        errors.append(str(exc))
    dependency_syntax_invalid = any(
        error.startswith("depends-on declarations ") for error in errors
    )
    blocked = [] if dependency_syntax_invalid else (
        [
            number
            for number in unresolved_dependencies(record)
        ]
        if issue_states is None
        else [
            number
            for number in unresolved_dependencies_from_states(record, issue_states)
        ]
    )
    if blocked:
        errors.append("open dependencies: " + ", ".join(f"#{number}" for number in blocked))
    return errors


def unresolved_dependencies_from_states(
    record: dict,
    issue_states: dict[int, str],
) -> list[int]:
    return [
        number
        for number in dependencies(str(record.get("body") or ""))
        if issue_states.get(number) != "closed"
    ]


def backlog_candidates(
    records: list[dict] | None = None,
    *,
    extra_errors: Callable[[dict], list[str]] | None = None,
    issue_states: dict[int, str] | None = None,
) -> tuple[list[tuple[int, dict]], dict[int, list[str]]]:
    snapshot = list_issues(label="status:backlog") if records is None else records
    candidates: list[tuple[int, dict]] = []
    rejected: dict[int, list[str]] = {}
    for record in snapshot:
        number = int(record["number"])
        errors = (
            evaluate(record)
            if issue_states is None
            else evaluate_with_states(record, issue_states)
        )
        if extra_errors is not None:
            errors.extend(extra_errors(record))
        if errors:
            rejected[number] = errors
            continue
        candidates.append((_priority(record), record))
    candidates.sort(key=lambda item: (item[0], int(item[1]["number"])))
    return candidates, rejected


def promote_issue(
    number: int,
    *,
    pre_mutation_check: Callable[[], None] | None = None,
    record: dict | None = None,
) -> None:
    try:
        set_status(
            number,
            "Ready",
            expected_current="Backlog",
            pre_mutation_check=pre_mutation_check,
        )
    except TypeError as exc:
        traceback = exc.__traceback__
        if traceback is not None and traceback.tb_next is None:
            raise KernelError(
                "transactional set_status API is required for Backlog promotion"
            ) from exc
        raise


def triage(*, promote_all: bool = False) -> dict[str, object]:
    candidates, rejected = backlog_candidates()
    promoted: list[int] = []
    for _priority, record in candidates:
        number = int(record["number"])
        promote_issue(number, record=record)  # pin exactly the body that was just evaluated
        promoted.append(number)
        if not promote_all:
            break
    return {"promoted": promoted, "rejected": rejected}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = triage(promote_all=args.all)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print("promoted:", ", ".join(f"#{n}" for n in result["promoted"]) or "none")
        for number, errors in result["rejected"].items():
            print(f"#{number}: " + "; ".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
