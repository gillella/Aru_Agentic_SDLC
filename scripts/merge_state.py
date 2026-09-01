"""Shared GitHub state and issue-contract gates for governed merges."""

from __future__ import annotations

import re
from typing import Any

from common import (
    AGENT_PREFIX,
    REPOSITORY_AUTH,
    KernelError,
    acceptance_items,
    gh_json,
    gh_paginated,
    issue,
    label_names,
    parse_touches,
    path_allowed,
    repo_slug,
    run,
    set_status,
    status_of,
)

_MERGE_QUEUE_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      number
      headRefOid
      baseRefOid
      mergeQueue{id}
      mergeQueueEntry{id state}
      autoMergeRequest{enabledAt}
    }
  }
}
"""


def pull_request(number: int) -> dict[str, Any]:
    data = gh_json(
        [
            "pr", "view", str(number), "--json",
            (
                "number,title,body,state,isDraft,headRefOid,headRefName,baseRefName,"
                "baseRefOid,mergeable,mergeStateStatus,labels,statusCheckRollup,"
                "reviewDecision,author,url,mergedAt,mergeCommit"
                ",createdAt"
            ),
        ]
    )
    if not isinstance(data, dict) or data.get("number") != number:
        raise KernelError(f"pull request #{number} is unavailable")
    return data


def linked_issues(body: str) -> list[int]:
    pattern = r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\s*$"
    return [int(value) for value in re.findall(pattern, body or "")]


def pull_changed_paths(number: int) -> list[str]:
    records = gh_paginated(f"repos/{repo_slug()}/pulls/{number}/files?per_page=100")
    paths: list[str] = []
    for record in records:
        filename = record.get("filename") if isinstance(record, dict) else None
        if not isinstance(filename, str) or not filename:
            raise KernelError("pull request file inventory is malformed")
        paths.append(filename)
        previous = record.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise KernelError("pull request file inventory is malformed")
            paths.append(previous)
    unique = sorted(set(paths))
    if not unique:
        raise KernelError("pull request has no changed files")
    return unique


def merge_queue_snapshot(
    number: int, expected_head: str, expected_base: str | None = None
) -> dict[str, object]:
    owner, name = repo_slug().split("/", 1)
    data = gh_json(
        [
            "api", "graphql", "-f", f"query={_MERGE_QUEUE_QUERY}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"number={number}",
        ],
        auth=REPOSITORY_AUTH,
    )
    repository = (data.get("data") or {}).get("repository") if isinstance(data, dict) else None
    pr = repository.get("pullRequest") if isinstance(repository, dict) else None
    if (
        not isinstance(pr, dict)
        or pr.get("number") != number
        or pr.get("headRefOid") != expected_head
        or (expected_base is not None and pr.get("baseRefOid") != expected_base)
    ):
        raise KernelError("merge queue state is incomplete or stale")
    queue, entry, auto_merge = (
        pr.get("mergeQueue"), pr.get("mergeQueueEntry"), pr.get("autoMergeRequest")
    )
    if queue is not None and (
        not isinstance(queue, dict) or not isinstance(queue.get("id"), str)
    ):
        raise KernelError("merge queue state is malformed")
    if entry is not None and (
        not isinstance(entry, dict)
        or not isinstance(entry.get("id"), str)
        or entry.get("state") not in {"AWAITING_CHECKS", "LOCKED", "MERGEABLE", "QUEUED"}
    ):
        raise KernelError("merge queue entry is malformed")
    if auto_merge is not None and (
        not isinstance(auto_merge, dict)
        or not isinstance(auto_merge.get("enabledAt"), str)
    ):
        raise KernelError("auto-merge state is malformed")
    return {"configured": queue is not None, "entry": entry, "auto_merge": auto_merge}


def base_snapshot(pr: dict[str, Any]) -> str:
    base_sha = pr.get("baseRefOid")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise KernelError("base snapshot is incomplete")
    return base_sha


def issue_gate(
    numbers: list[int],
    changed_paths: list[str],
    *,
    allow_closed: bool = False,
    allow_done: bool = False,
) -> list[dict[str, object]]:
    if len(numbers) != 1:
        raise KernelError("PR body must contain exactly one closing issue directive")
    number = numbers[0]
    record = issue(number)
    allowed_states = {"OPEN", "CLOSED"} if allow_closed else {"OPEN"}
    if record.get("state") not in allowed_states:
        raise KernelError(f"issue #{number} has an invalid open/closed state")
    owners = [name for name in label_names(record) if name.startswith(AGENT_PREFIX)]
    if len(owners) != 1:
        raise KernelError(f"issue #{number} does not have one exclusive claimant")
    body = str(record.get("body") or "")
    items = acceptance_items(body)
    lifecycle = status_of(record)
    if lifecycle != "In Review" and not (allow_done and lifecycle == "Done"):
        raise KernelError(f"issue #{number} is not In Review")
    if not items or any(not done for done, _ in items):
        raise KernelError(f"issue #{number} has incomplete Acceptance Criteria")
    declared = parse_touches(body)
    violations = [path for path in changed_paths if not path_allowed(path, declared)]
    if violations:
        raise KernelError(
            "pull request changes paths outside the linked issue touches contract: "
            + ", ".join(violations)
        )
    return [{
        "issue": number,
        "criteria": len(items),
        "touches": declared,
        "claimant": owners[0][len(AGENT_PREFIX) :],
    }]


def close_out(
    numbers: list[int], changed_paths: list[str]
) -> list[dict[str, object]]:
    # This is deliberately inside close-out, after every other post-merge
    # network check. A queued or immediate merge must not mark an issue Done
    # from an earlier claimant, Acceptance Criteria, or touches snapshot.
    issue_gate(numbers, changed_paths, allow_closed=True, allow_done=True)
    for number in numbers:
        record = issue(number)
        lifecycle = status_of(record)
        if lifecycle == "In Review":
            set_status(number, "Done", expected_current="In Review")
        elif lifecycle != "Done":
            raise KernelError(f"issue #{number} changed before close-out")
        if record.get("state") != "CLOSED":
            run(["gh", "issue", "close", str(number), "--reason", "completed"])
        settled = issue(number)
        if status_of(settled) != "Done" or settled.get("state") != "CLOSED":
            raise KernelError(f"issue #{number} close-out did not settle")
    # Detect contract drift during the status/close mutations as well. GitHub
    # cannot make these separate issue and Project updates one transaction.
    return issue_gate(numbers, changed_paths, allow_closed=True, allow_done=True)
