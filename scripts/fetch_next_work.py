#!/usr/bin/env python3
"""Return one issue, feedback item, merge item, or wait state."""

from __future__ import annotations

import argparse

from check_ci import ci_verdict
from claim_issue import claim, safe_agent
from common import KernelError, gh_json, json_print, label_names, list_issues
from fetch_pr_feedback import fetch_feedback
from merge_pr import evaluate


def authored_prs(agent: str) -> list[dict]:
    data = gh_json(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            f"label:author:{agent}",
            "--limit",
            "100",
            "--json",
            "number,title,headRefOid,labels,isDraft,mergeStateStatus",
        ]
    )
    if not isinstance(data, list):
        raise KernelError("GitHub returned malformed pull-request inventory")
    return sorted(data, key=lambda item: int(item["number"]))


def select(agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    for pr in authored_prs(agent):
        number = int(pr["number"])
        feedback = fetch_feedback(number)
        if feedback:
            return {"type": "feedback", "pr": number, "items": feedback}
        ci = ci_verdict(number)
        if ci["state"] == "failure":
            return {"type": "ci", "pr": number, "head": ci["head"], "checks": ci["checks"]}
        reviews = [name for name in label_names(pr) if name.startswith("review:")]
        if ci["state"] == "success" and len(reviews) == 1:
            try:
                evaluate(number, str(ci["head"]))
            except KernelError as exc:
                return {"type": "wait", "pr": number, "head": ci["head"], "reason": str(exc)}
            return {"type": "merge", "pr": number, "head": ci["head"]}
        return {"type": "wait", "pr": number, "head": ci["head"], "ci": ci["state"]}

    ready = sorted(list_issues(label="status:ready"), key=lambda item: int(item["number"]))
    if ready:
        record = ready[0]
        return {"type": "issue", "issue": int(record["number"]), "title": record["title"]}
    return {"type": "idle"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--claim", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = select(args.agent)
        if args.claim and result["type"] == "issue":
            result["claim"] = claim(int(result["issue"]), args.agent)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print({"agent": args.agent, "work": result})
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
