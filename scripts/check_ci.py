#!/usr/bin/env python3
"""Read required exact-current-head GitHub check state for one pull request."""

from __future__ import annotations

import argparse
import re

from common import KernelError, gh_json, json_print, repo_slug

DEFAULT_REQUIRED_CHECK = "aru-governed-pr"
GITHUB_ACTIONS_APP_ID = 15368


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
    if conclusion == "SUCCESS":
        return "success"
    return "failure"


def exact_head_check_runs(head: str) -> list[dict]:
    response = gh_json(
        ["api", f"repos/{repo_slug()}/commits/{head}/check-runs?per_page=100&filter=latest"]
    )
    total = response.get("total_count") if isinstance(response, dict) else None
    records = response.get("check_runs") if isinstance(response, dict) else None
    if (
        not isinstance(total, int)
        or not isinstance(records, list)
        or total != len(records)
        or any(not isinstance(record, dict) for record in records)
    ):
        raise KernelError("exact-head GitHub check-run inventory is incomplete")
    return records


def commit_verdict(head: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise KernelError("exact-head GitHub check state is incomplete")
    records = exact_head_check_runs(head)
    names = [DEFAULT_REQUIRED_CHECK]
    by_name: dict[str, list[dict]] = {}
    for record in records:
        by_name.setdefault(check_name(record), []).append(record)
    states: list[str] = []
    for name in names:
        matches = by_name.get(name, [])
        if len(matches) > 1:
            raise KernelError(f"required GitHub check is ambiguous: {name}")
        if matches:
            app = matches[0].get("app")
            if not isinstance(app, dict) or app.get("id") != GITHUB_ACTIONS_APP_ID:
                raise KernelError(f"required GitHub check has an untrusted source: {name}")
            if matches[0].get("head_sha") != head:
                raise KernelError(f"required GitHub check is not bound to the exact head: {name}")
        states.append("pending" if not matches else check_state(matches[0]))
    state = "failure" if "failure" in states else "pending" if "pending" in states else "success"
    return {"head": head, "state": state, "checks": names}


def ci_verdict(number: int) -> dict[str, object]:
    pr = gh_json(["pr", "view", str(number), "--json", "number,headRefOid"])
    head = pr.get("headRefOid")
    if (
        pr.get("number") != number
        or not isinstance(head, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", head)
    ):
        raise KernelError("exact-head GitHub check state is incomplete")
    result = commit_verdict(head)
    return {"pr": number, **result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = ci_verdict(args.pr)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        print(f"PR #{args.pr} {result['head']}: {result['state']}")
    return 0 if result["state"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
