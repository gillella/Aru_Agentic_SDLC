#!/usr/bin/env python3
"""Promote complete and unblocked Backlog issues to Ready."""

from __future__ import annotations

import argparse

from common import (
    KernelError,
    contract_errors,
    json_print,
    label_names,
    list_issues,
    set_status,
    unresolved_dependencies,
)


def evaluate(record: dict) -> list[str]:
    errors = contract_errors(record)
    priorities = {f"priority:p{value}" for value in range(4)}
    priority_labels = [name for name in label_names(record) if name in priorities]
    if len(priority_labels) != 1:
        errors.append(
            "issue must have exactly one priority:p0..p3 label; "
            f"found {len(priority_labels)}"
        )
    blocked = unresolved_dependencies(record)
    if blocked:
        errors.append("open dependencies: " + ", ".join(f"#{number}" for number in blocked))
    return errors


def triage(*, promote_all: bool = False) -> dict[str, object]:
    candidates = sorted(
        list_issues(label="status:backlog"),
        key=lambda item: int(item["number"]),
    )
    promoted: list[int] = []
    rejected: dict[int, list[str]] = {}
    for record in candidates:
        number = int(record["number"])
        errors = evaluate(record)
        if errors:
            rejected[number] = errors
            continue
        set_status(number, "Ready")
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
