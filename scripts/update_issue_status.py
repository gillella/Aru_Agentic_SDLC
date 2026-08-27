#!/usr/bin/env python3
"""Move one issue between the five kernel statuses."""

from __future__ import annotations

import argparse

from common import KernelError, STATUSES, issue, set_status, status_of


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
    parser.add_argument("--status", choices=STATUSES, required=True)
    args = parser.parse_args()
    try:
        result = update(args.issue, args.status)
    except KernelError as exc:
        parser.error(str(exc))
    print(f"#{result['issue']}: {result['before'] or 'Unspecified'} -> {result['after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
