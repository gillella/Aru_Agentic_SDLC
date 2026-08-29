#!/usr/bin/env python3
"""Return one work item or a bounded batch from one inventory snapshot."""

from __future__ import annotations

import argparse
from pathlib import PurePosixPath

from check_ci import ci_verdict
from claim_issue import claim, safe_agent
from common import (
    KernelError,
    gh_json,
    gh_paginated,
    json_print,
    label_names,
    parse_touches,
    repo_slug,
)
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


def open_prs() -> list[dict]:
    return gh_paginated(f"repos/{repo_slug()}/pulls?state=open&per_page=100")


def ready_issues() -> list[dict]:
    records = gh_paginated(
        f"repos/{repo_slug()}/issues?state=open&labels=status%3Aready&per_page=100"
    )
    return [record for record in records if "pull_request" not in record]


def has_review_comments(number: int) -> bool:
    comments = gh_json(
        ["api", f"repos/{repo_slug()}/pulls/{number}/comments?per_page=1"]
    )
    if not isinstance(comments, list):
        raise KernelError("GitHub returned malformed review comments")
    return bool(comments)


def _open_pr_work(pr: dict) -> dict[str, object]:
    number = int(pr["number"])
    if has_review_comments(number):
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


def select(agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    authored = authored_prs(agent)
    if authored:
        return _open_pr_work(authored[0])

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


def _batch_agents(agents: list[str]) -> list[str]:
    validated = [safe_agent(agent) for agent in agents]
    if len(validated) < 2:
        raise KernelError("batch mode requires repeated --agent")
    if len(set(validated)) != len(validated):
        raise KernelError("batch agent ids must be distinct")
    return validated


def _batch_number(record: dict, kind: str) -> int:
    number = record.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise KernelError(f"{kind} number must be a positive integer")
    return number


def _prs_by_batch_author(prs: list[dict], agents: list[str]) -> dict[str, list[dict]]:
    # Batch checks stay isolated: authored_prs() keeps label search; select() skips touches.
    requested = set(agents)
    authored = {agent: [] for agent in agents}
    for pr in prs:
        number = _batch_number(pr, "Open PR")
        author_labels = [
            name for name in label_names(pr) if name.startswith("author:")
        ]
        if not author_labels:
            continue
        if len(author_labels) > 1:
            raise KernelError(
                f"Open PR #{number} has contradictory author labels"
            )
        matching = [name[7:] for name in author_labels if name[7:] in requested]
        if not matching:
            continue
        authored[matching[0]].append(pr)
    for agent in agents:
        authored[agent].sort(key=lambda item: item["number"])
    return authored


def _touch_rule(value: str) -> tuple[str, bool]:
    recursive = value.endswith("/**")
    raw = value[:-3].rstrip("/") if recursive else value
    return PurePosixPath(raw).as_posix(), recursive


def _touches_overlap(left: list[str], right: list[str]) -> bool:
    for left_value in left:
        left_path, left_recursive = _touch_rule(left_value)
        for right_value in right:
            right_path, right_recursive = _touch_rule(right_value)
            if left_path == right_path:
                return True
            if left_recursive and right_path.startswith(left_path + "/"):
                return True
            if right_recursive and left_path.startswith(right_path + "/"):
                return True
    return False


def _batch_ready_candidates(
    records: list[dict],
) -> tuple[list[tuple[int, int, dict, list[str]]], list[str]]:
    priorities = {f"priority:p{value}": value for value in range(4)}
    ready: list[tuple[int, int, dict, list[str]]] = []
    diagnostics: list[tuple[int, str]] = []
    for record in records:
        number = _batch_number(record, "Ready issue")
        labels = label_names(record)
        if "needs-human" in labels or "type:epic" in labels:
            continue
        priority_labels = [name for name in labels if name.startswith("priority:")]
        if len(priority_labels) > 1 or any(
            name not in priorities for name in priority_labels
        ):
            diagnostics.append(
                (
                    number,
                    f"Ready issue #{number} has contradictory or unsupported "
                    "priority labels; skipped",
                )
            )
            continue
        try:
            touches = parse_touches(str(record.get("body") or ""))
        except KernelError as exc:
            diagnostics.append(
                (number, f"Ready issue #{number} has invalid touches: {exc}; skipped")
            )
            continue
        priority = priorities[priority_labels[0]] if priority_labels else 2
        ready.append((priority, number, record, touches))
    ready.sort(key=lambda item: (item[0], item[1]))
    return ready, [message for _number, message in sorted(diagnostics)]


def select_batch(agents: list[str]) -> dict[str, object]:
    agents = _batch_agents(agents)
    prs_snapshot = open_prs()
    issues_snapshot = ready_issues()
    prs_by_author = _prs_by_batch_author(prs_snapshot, agents)
    lanes: list[dict[str, object]] = []
    free_lanes: list[dict[str, object]] = []

    for agent in agents:
        lane: dict[str, object] = {"agent": agent}
        if prs_by_author[agent]:
            lane["work"] = _open_pr_work(prs_by_author[agent][0])
        else:
            free_lanes.append(lane)
        lanes.append(lane)

    candidates, diagnostics = _batch_ready_candidates(issues_snapshot)
    reserved_paths: list[list[str]] = []
    candidate_index = 0
    for lane in free_lanes:
        while candidate_index < len(candidates):
            _priority, _number, record, touches = candidates[candidate_index]
            candidate_index += 1
            if any(_touches_overlap(touches, reserved) for reserved in reserved_paths):
                continue
            lane["work"] = {
                "type": "issue",
                "issue": _number,
                "title": record["title"],
            }
            reserved_paths.append(touches)
            break
        else:
            lane["work"] = {"type": "idle"}

    return {
        "schema": "aru.fetch-next-work.batch/v1",
        "lanes": lanes,
        "diagnostics": diagnostics,
        "claim_status": "not-requested",
    }


def _claim_batch(result: dict[str, object]) -> int:
    successful_claims = 0
    stopped = False
    for lane in result["lanes"]:
        work = lane["work"]
        if work["type"] != "issue":
            continue
        if stopped:
            work["claim"] = {
                "status": "skipped",
                "reason": "claim stopped after earlier failure",
            }
            continue
        try:
            work["claim"] = claim(int(work["issue"]), str(lane["agent"]))
        except KernelError as exc:
            work["claim"] = {"status": "failed", "error": str(exc)}
            stopped = True
        else:
            successful_claims += 1
    if stopped:
        result["claim_status"] = "partial" if successful_claims else "failed"
        return 1
    result["claim_status"] = "complete"
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", action="append", required=True)
    parser.add_argument("--claim", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    batch_mode = len(args.agent) > 1
    try:
        if batch_mode:
            agents = _batch_agents(args.agent)
            if not args.json:
                raise KernelError("batch mode requires --json")
            result = select_batch(agents)
            status = _claim_batch(result) if args.claim else 0
        else:
            agent = args.agent[0]
            result = select(agent)
            status = 0
        if not batch_mode and args.claim and result["type"] == "issue":
            result["claim"] = claim(int(result["issue"]), agent)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result if batch_mode else {"agent": agent, "work": result})
    else:
        print(result)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
