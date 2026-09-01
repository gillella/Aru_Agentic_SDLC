#!/usr/bin/env python3
"""Read exact-current-head focused local verification state for one pull request."""

from __future__ import annotations

import argparse
import time

from common import KernelError, gh_json, json_print
from local_verification import local_verification_status


def check_name(record: dict) -> str:
    name = record.get("name") or record.get("context")
    if not isinstance(name, str) or not name:
        raise KernelError("CI returned a nameless check")
    return name


def check_state(record: dict) -> str:
    status = str(record.get("status") or "").upper()
    conclusion = str(record.get("conclusion") or record.get("state") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion in {"PENDING", "EXPECTED", ""}:
        return "pending"
    if conclusion in {"SUCCESS", "NEUTRAL"}:
        return "success"
    return "failure"


def ci_verdict(number: int) -> dict[str, object]:
    pr = gh_json(["pr", "view", str(number), "--json", "number,headRefOid,body"])
    head = pr.get("headRefOid")
    body = pr.get("body")
    if not isinstance(head, str) or len(head) != 40:
        raise KernelError("local verification state is incomplete")
    result = local_verification_status(body, head)
    return {"pr": number, "head": head, "state": result["state"], "checks": result["checks"]}


def wait_for_ci(number: int, timeout: int, interval: int) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        result = ci_verdict(number)
        if result["state"] != "pending" or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            wait_for_ci(args.pr, args.timeout, args.interval)
            if args.wait
            else ci_verdict(args.pr)
        )
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print(f"PR #{args.pr} {result['head']}: {result['state']}")
    return 0 if result["state"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
