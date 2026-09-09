#!/usr/bin/env python3
"""Select one existing work item from one authoritative GitHub snapshot.

This command is deliberately read-only. It resumes one authored pull request
before returning one contract-valid Ready issue. Claiming, Backlog promotion,
batch allocation, and project/epic orchestration belong to explicit operator
commands outside the minimal kernel.
"""

from __future__ import annotations

import argparse

from check_ci import ci_verdict
from claim_issue import safe_agent
from create_pr import reviewer_continuation
from common import (
    REPOSITORY_AUTH,
    KernelError,
    contract_errors,
    dependencies,
    gh_json,
    gh_paginated,
    json_print,
    label_names,
    repo_slug,
)
from fetch_pr_feedback import fetch_feedback
from merge_pr import evaluate

MAX_DEPENDENCY_REFERENCES = 100
_MERGED_PR_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    issue(number:$number){
      closedByPullRequestsReferences(first:100){
        nodes{number state headRefOid}
        pageInfo{hasNextPage}
      }
    }
  }
}
"""
_MERGE_STATE_STATUSES = {
    "BEHIND",
    "BLOCKED",
    "CLEAN",
    "DIRTY",
    "DRAFT",
    "HAS_HOOKS",
    "UNKNOWN",
    "UNSTABLE",
}


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


def claimed_issue(agent: str) -> dict | None:
    records = gh_paginated(
        f"repos/{repo_slug()}/issues?state=all&labels=agent%3A{agent}&per_page=100"
    )
    issues = []
    for record in records:
        if not isinstance(record, dict):
            raise KernelError("agent claim inventory is malformed or non-exclusive")
        if "pull_request" in record:
            continue
        labels = label_names(record)
        if "status:in-progress" in labels or "status:in-review" in labels:
            issues.append(record)
    if len(issues) > 1:
        raise KernelError("agent claim inventory is malformed or non-exclusive")
    if not issues:
        return None
    record = issues[0]
    labels = label_names(record)
    owners = [name for name in labels if name.startswith("agent:")]
    if owners != [f"agent:{agent}"]:
        raise KernelError("agent claim is not exclusive")
    return record


def merged_closing_pr(number: int) -> dict[str, object]:
    owner, name = repo_slug().split("/", 1)
    data = gh_json(
        [
            "api", "graphql", "-f", f"query={_MERGED_PR_QUERY}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"number={number}",
        ],
        auth=REPOSITORY_AUTH,
    )
    issue_node = ((data.get("data") or {}).get("repository") or {}).get("issue")
    connection = (issue_node or {}).get("closedByPullRequestsReferences")
    if (
        not isinstance(connection, dict)
        or not isinstance(connection.get("nodes"), list)
        or (connection.get("pageInfo") or {}).get("hasNextPage") is not False
    ):
        raise KernelError("merged closing pull-request inventory is incomplete")
    if any(not isinstance(node, dict) for node in connection["nodes"]):
        raise KernelError("merged closing pull-request inventory is malformed")
    matches = [node for node in connection["nodes"] if node.get("state") == "MERGED"]
    if len(matches) != 1:
        raise KernelError("issue does not have one merged closing pull request")
    pr = matches[0]
    if (
        not isinstance(pr.get("number"), int)
        or not isinstance(pr.get("headRefOid"), str)
        or len(pr["headRefOid"]) != 40
    ):
        raise KernelError("merged closing pull-request inventory is malformed")
    return {"pr": int(pr["number"]), "head": str(pr["headRefOid"])}


def _pre_dependency_category(labels: list[str]) -> str | None:
    if "needs-human" in labels:
        return "human_gated"
    if "type:epic" in labels:
        return "epics"
    return None


def _referenced_dependencies(records: list[dict]) -> list[int]:
    referenced: set[int] = set()
    for record in records:
        if _pre_dependency_category(label_names(record)) is not None:
            continue
        try:
            referenced.update(dependencies(str(record.get("body") or "")))
        except KernelError:
            continue
    return sorted(referenced)


def dependency_states(records: list[dict]) -> dict[int, str]:
    numbers = _referenced_dependencies(records)
    if len(numbers) > MAX_DEPENDENCY_REFERENCES:
        raise KernelError(
            f"Ready dependency inventory exceeds {MAX_DEPENDENCY_REFERENCES} references"
        )
    if not numbers:
        return {}

    owner, name = repo_slug().split("/", 1)
    fields = " ".join(
        f"issue_{number}:issue(number:{number}){{number state}}" for number in numbers
    )
    query = (
        f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{fields}}}}}"
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
        auth=REPOSITORY_AUTH,
    )
    if not isinstance(data, dict) or data.get("errors"):
        raise KernelError("Ready dependency inventory is incomplete")
    root = data.get("data")
    repository = root.get("repository") if isinstance(root, dict) else None
    if not isinstance(repository, dict):
        raise KernelError("Ready dependency inventory is incomplete")

    states: dict[int, str] = {}
    for number in numbers:
        alias = f"issue_{number}"
        if alias not in repository:
            raise KernelError("Ready dependency inventory is incomplete")
        record = repository[alias]
        if record is None:
            continue
        if (
            not isinstance(record, dict)
            or record.get("number") != number
            or record.get("state") not in {"OPEN", "CLOSED"}
        ):
            raise KernelError("Ready dependency inventory is malformed")
        states[number] = str(record["state"]).lower()
    return states


def has_review_comments(number: int) -> bool:
    records = gh_json(["api", f"repos/{repo_slug()}/pulls/{number}/comments?per_page=1"])
    if not isinstance(records, list):
        raise KernelError("GitHub returned malformed review comments")
    return bool(records)


def _pr_live_merge_state(number: int, head: str) -> str:
    record = gh_json(
        ["pr", "view", str(number), "--json", "number,headRefOid,state,mergeStateStatus"]
    )
    if (
        not isinstance(record, dict)
        or record.get("number") != number
        or record.get("headRefOid") != head
        or str(record.get("state") or "").upper() != "OPEN"
        or record.get("mergeStateStatus") not in _MERGE_STATE_STATUSES
    ):
        raise KernelError(f"GitHub returned malformed pull request reread for #{number}")
    return str(record["mergeStateStatus"])


def _open_pr_work(pr: dict) -> dict[str, object]:
    number = int(pr["number"])
    if has_review_comments(number):
        feedback = fetch_feedback(number)
        if feedback:
            return {"type": "feedback", "pr": number, "items": feedback}

    try:
        verification = ci_verdict(number)
    except KernelError as exc:
        return {"type": "blocked", "pr": number, "head": pr.get("headRefOid"),
                "verification": "unreadable", "ci_error": str(exc),
                "reason": "CI inspection is unreadable; merge is blocked"}
    return _verified_pr_work(pr, verification)


def _verified_pr_work(pr: dict, verification: dict) -> dict[str, object]:
    number = int(pr["number"])
    head = str(verification["head"])
    context = {"pr": number, "head": head}
    merge_state = pr.get("mergeStateStatus")
    if not isinstance(merge_state, str) or not merge_state:
        merge_state = _pr_live_merge_state(number, head)
    elif merge_state not in _MERGE_STATE_STATUSES:
        raise KernelError(f"GitHub returned malformed pull request snapshot for #{number}")
    if merge_state == "DIRTY":
        return {**context, "type": "conflict", "reason": "PR merge state is DIRTY"}
    if verification["state"] == "failure":
        return {**context, "type": "verification", "checks": verification["checks"]}
    if verification["state"] == "success":
        try:
            evaluate(number, head)
        except KernelError as exc:
            reason = str(exc)
            if reason == "PR merge state is DIRTY":
                return {**context, "type": "conflict", "reason": reason}
            result = {**context, "type": "wait", "reason": reason}
            if "requests changes" in reason or "requested changes" in reason:
                result["next_action"] = "address-review-feedback"
            elif (
                "exact-head verdict" in reason
                or "review authority" in reason
                or "review:<authority>" in reason
            ):
                result.update(reviewer_continuation(number))
            return result
        return {**context, "type": "merge"}
    return {**context, "type": "wait", "verification": verification["state"]}


def _ready_candidates(
    records: list[dict], issue_states: dict[int, str]
) -> tuple[list[tuple[int, int, dict]], list[str], dict[str, int]]:
    priorities = {f"priority:p{value}": value for value in range(4)}
    candidates: list[tuple[int, int, dict]] = []
    diagnostics: list[tuple[int, str]] = []
    classification = {
        "total_ready": len(records),
        "executable_ready": 0,
        "human_gated": 0,
        "epics": 0,
        "dependency_blocked": 0,
        "malformed": 0,
    }
    for record in records:
        number = int(record["number"])
        labels = label_names(record)
        category = _pre_dependency_category(labels)
        if category:
            classification[category] += 1
            continue
        try:
            dependency_numbers = dependencies(str(record.get("body") or ""))
        except KernelError as exc:
            classification["malformed"] += 1
            diagnostics.append((number, f"Ready issue #{number} is malformed: {exc}; skipped"))
            continue
        if any(issue_states.get(value) != "closed" for value in dependency_numbers):
            classification["dependency_blocked"] += 1
            continue
        priority_labels = [name for name in labels if name.startswith("priority:")]
        # The inventory endpoint is already constrained to open issues.  The
        # REST and GraphQL APIs disagree on state casing, so normalize before
        # applying the shared Ready contract.
        errors = contract_errors({**record, "state": str(record.get("state") or "OPEN").upper()})
        if len(priority_labels) > 1 or any(name not in priorities for name in priority_labels):
            errors.append("has contradictory or unsupported priority labels")
        if errors:
            classification["malformed"] += 1
            diagnostics.extend(
                (number, f"Ready issue #{number} {error}; skipped") for error in errors
            )
            continue
        priority = priorities[priority_labels[0]] if priority_labels else 2
        candidates.append((priority, number, record))
        classification["executable_ready"] += 1
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates, [message for _number, message in sorted(diagnostics)], classification


def _classification_summary(classification: dict[str, int]) -> str | None:
    if classification["total_ready"] == classification["executable_ready"]:
        return None
    return (
        f"Ready classification: total={classification['total_ready']}, "
        f"executable={classification['executable_ready']}, "
        f"human-gated={classification['human_gated']}, "
        f"epics={classification['epics']}, "
        f"dependency-blocked={classification['dependency_blocked']}, "
        f"malformed={classification['malformed']}"
    )


def _select_ready_issue(records: list[dict]) -> dict[str, object]:
    candidates, diagnostics, classification = _ready_candidates(
        records, dependency_states(records)
    )
    if candidates:
        _priority, number, record = candidates[0]
        result: dict[str, object] = {
            "type": "issue",
            "issue": number,
            "title": str(record["title"]),
        }
    else:
        result = {"type": "idle"}
        summary = _classification_summary(classification)
        if summary:
            result["ready_classification"] = classification
            diagnostics.insert(0, summary)
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


def select(agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    authored = authored_prs(agent)
    if authored:
        return _open_pr_work(authored[0])
    claimed = claimed_issue(agent)
    if claimed is not None:
        labels = label_names(claimed)
        if "status:in-review" in labels:
            merged = merged_closing_pr(int(claimed["number"]))
            return {
                "type": "finalize",
                "issue": int(claimed["number"]),
                **merged,
                "next_action": "finalize-queued-merge",
            }
        if str(claimed.get("state") or "").upper() != "OPEN":
            raise KernelError("In Progress claimed issue is not open")
        return {
            "type": "claimed_issue",
            "issue": int(claimed["number"]),
            "title": str(claimed["title"]),
            "next_action": "resume-implementation",
        }
    return _select_ready_issue(ready_issues())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", action="append", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if len(args.agent) != 1:
            raise KernelError("fetch_next_work accepts exactly one --agent")
        agent = args.agent[0]
        result = select(agent)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print({"agent": agent, "work": result})
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
