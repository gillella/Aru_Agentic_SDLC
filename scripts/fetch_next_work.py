#!/usr/bin/env python3
"""Return one issue, feedback item, merge item, or wait state."""

from __future__ import annotations

import argparse

from check_ci import ci_verdict
from claim_issue import claim, safe_agent
from common import KernelError, gh_json, gh_paginated, json_print, label_names, repo_slug
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


def ready_issues() -> list[dict]:
    records = gh_paginated(
        f"repos/{repo_slug()}/issues?state=open&labels=status%3Aready&per_page=100"
    )
    return [record for record in records if "pull_request" not in record]


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

    priorities = {f"priority:p{value}": value for value in range(4)}
    ready: list[tuple[int, int, dict]] = []
    diagnostics: list[str] = []
    for record in ready_issues():
        labels = label_names(record)
        if "needs-human" in labels or "type:epic" in labels:
            continue
        number = int(record["number"])
        priority_labels = [name for name in labels if name.startswith("priority:")]
        if len(priority_labels) > 1 or any(
            name not in priorities for name in priority_labels
        ):
            diagnostics.append(
                f"Ready issue #{number} has contradictory or unsupported "
                "priority labels; skipped"
            )
            continue
        priority = (
            priorities[priority_labels[0]]
            if priority_labels
            else priorities["priority:p2"]
        )
        ready.append((priority, number, record))
    ready.sort(key=lambda item: (item[0], item[1]))
    if ready:
        record = ready[0][2]
        result: dict[str, object] = {
            "type": "issue",
            "issue": int(record["number"]),
            "title": record["title"],
        }
    else:
        result = {"type": "idle"}
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


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
