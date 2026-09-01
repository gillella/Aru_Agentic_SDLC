#!/usr/bin/env python3
"""Return one work item or a bounded batch from one inventory snapshot."""

from __future__ import annotations

import argparse
import re
from pathlib import PurePosixPath

import triage_backlog
from check_ci import ci_verdict
from claim_issue import claim, safe_agent
from common import (
    AGENT_PREFIX,
    AUTHOR_PREFIX,
    REPOSITORY_AUTH,
    KernelError,
    StatusPreconditionError,
    contract_errors,
    dependencies,
    gh_json,
    gh_paginated,
    issue,
    json_print,
    label_names,
    parse_touches,
    project_item_status,
    repo_slug,
)
from fetch_pr_feedback import fetch_feedback
from merge_pr import evaluate

MAX_DEPENDENCY_REFERENCES = 100
_RECOVERY_CANDIDATE_PATTERN = r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\s*$"
_MERGE_STATE_STATUSES = {
    "BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"
}


class _RecoveryDriftError(KernelError):
    pass


def authored_prs(agent: str) -> list[dict]:
    query = ["pr", "list", "--state", "open", "--search", f"label:author:{agent}", "--limit", "100"]
    data = gh_json([*query, "--json", "number,title,headRefOid,labels,isDraft,mergeStateStatus"])
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


def backlog_issues() -> list[dict]:
    query = ["issue", "list", "--state", "open", "--limit", "200", "--label", "status:backlog"]
    records = gh_json([*query, "--json", "number,title,body,state,labels,assignees,url"])
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or not isinstance(record.get("number"), int)
        or isinstance(record.get("number"), bool)
        or int(record["number"]) <= 0
        or not isinstance(record.get("title"), str)
        or not record["title"]
        for record in records
    ):
        raise KernelError("GitHub returned malformed Backlog issue inventory")
    return sorted(records, key=lambda item: int(item["number"]))


def _pre_dependency_category(labels: list[str]) -> str | None:
    if "needs-human" in labels:
        return "human_gated"
    if "type:epic" in labels:
        return "epics"
    return None


def dependency_states(records: list[dict]) -> dict[int, str]:
    return _dependency_states(records, inventory_name="Ready")


def backlog_dependency_states(records: list[dict]) -> dict[int, str]:
    return _dependency_states(records, inventory_name="Backlog")


def _dependency_states(records: list[dict], *, inventory_name: str) -> dict[int, str]:
    numbers = sorted(
        {
            number
            for record in records
            if _pre_dependency_category(label_names(record)) is None
            for number in dependencies(str(record.get("body") or ""))
        }
    )
    if len(numbers) > MAX_DEPENDENCY_REFERENCES:
        raise KernelError(
            f"{inventory_name} dependency inventory exceeds {MAX_DEPENDENCY_REFERENCES} references"
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
        raise KernelError(f"{inventory_name} dependency inventory is incomplete")
    root = data.get("data")
    repository = root.get("repository") if isinstance(root, dict) else None
    if not isinstance(repository, dict):
        raise KernelError(f"{inventory_name} dependency inventory is incomplete")

    states: dict[int, str] = {}
    for number in numbers:
        alias = f"issue_{number}"
        if alias not in repository:
            raise KernelError(f"{inventory_name} dependency inventory is incomplete")
        record = repository[alias]
        if record is None:
            continue
        if not isinstance(record, dict):
            raise KernelError(f"{inventory_name} dependency inventory is malformed")
        state = record.get("state")
        if record.get("number") != number or state not in {"OPEN", "CLOSED"}:
            raise KernelError(f"{inventory_name} dependency inventory is malformed")
        states[number] = state.lower()
    return states


def has_review_comments(number: int) -> bool:
    comments = gh_json(["api", f"repos/{repo_slug()}/pulls/{number}/comments?per_page=1"])
    if not isinstance(comments, list):
        raise KernelError("GitHub returned malformed review comments")
    return bool(comments)


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
    ci = ci_verdict(number)
    head = str(ci["head"])
    merge_state = pr.get("mergeStateStatus")
    if not isinstance(merge_state, str) or not merge_state:
        merge_state = _pr_live_merge_state(number, head)
    elif merge_state not in _MERGE_STATE_STATUSES:
        raise KernelError(f"GitHub returned malformed pull request snapshot for #{number}")
    if merge_state == "DIRTY":
        return {
            "type": "conflict",
            "pr": number,
            "head": head,
            "reason": "PR merge state is DIRTY",
        }
    if ci["state"] == "failure":
        return {"type": "ci", "pr": number, "head": head, "checks": ci["checks"]}
    reviews = [name for name in label_names(pr) if name.startswith("review:")]
    if ci["state"] == "success" and len(reviews) == 1:
        try:
            evaluate(number, head)
        except KernelError as exc:
            if str(exc) == "PR merge state is DIRTY":
                return {
                    "type": "conflict",
                    "pr": number,
                    "head": head,
                    "reason": str(exc),
                }
            return {"type": "wait", "pr": number, "head": head, "reason": str(exc)}
        return {"type": "merge", "pr": number, "head": head}
    return {"type": "wait", "pr": number, "head": head, "ci": ci["state"]}


def select(agent: str) -> dict[str, object]:
    agent = safe_agent(agent)
    authored = authored_prs(agent)
    if authored:
        return _open_pr_work(authored[0])

    issues_snapshot = ready_issues()
    result = _select_ready_issue(issues_snapshot)
    if result["type"] != "idle":
        return result
    if issues_snapshot:
        return result
    recovered = _recover_single_issue()
    if recovered is not None:
        return recovered
    return result


def _select_ready_issue(records: list[dict]) -> dict[str, object]:
    candidates, diagnostics, classification = _ready_candidates(records, dependency_states(records))
    if candidates:
        record = candidates[0][2]
        result: dict[str, object] = {
            "type": "issue",
            "issue": int(record["number"]),
            "title": record["title"],
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


def _extra_backlog_errors(record: dict) -> list[str]:
    labels = label_names(record)
    errors: list[str] = []
    if [name for name in labels if name.startswith("status:")] != ["status:backlog"]:
        errors.append("issue must have exactly one status:backlog label")
    if any(name.startswith(AGENT_PREFIX) for name in labels):
        errors.append("issue is already claimed")
    return errors


def _backlog_pre_dependency_errors(record: dict) -> list[str]:
    errors = contract_errors(record)
    labels = label_names(record)
    if "needs-human" in labels:
        errors.append("needs-human issues cannot enter Ready")
    if "type:epic" in labels:
        errors.append("type:epic issues cannot enter Ready")
    priority_labels = [name for name in labels if name.startswith("priority:")]
    priorities = {f"priority:p{value}" for value in range(4)}
    if len(priority_labels) > 1 or any(name not in priorities for name in priority_labels):
        errors.append("issue may have at most one supported priority:p0..p3 label")
    return errors


def _backlog_dependency_records(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if not _backlog_pre_dependency_errors(record) and not _extra_backlog_errors(record)
    ]


def _recoverable_backlog(
    records: list[dict],
) -> tuple[list[tuple[int, dict]], dict[int, list[str]]]:
    candidates, rejected = triage_backlog.backlog_candidates(
        records,
        extra_errors=_extra_backlog_errors,
        issue_states=backlog_dependency_states(_backlog_dependency_records(records)),
    )
    authoritative: list[tuple[int, dict]] = []
    for priority, record in candidates:
        number = int(record["number"])
        try:
            project_status = project_item_status(number)
        except KernelError as exc:
            if str(exc) != f"issue #{number} is not a member of the linked Project Board":
                raise
            rejected[number] = ["issue is not a member of the linked Project Board"]
            continue
        if project_status != "Backlog":
            detail = "is unset" if project_status is None else f"is {project_status!r}, expected 'Backlog'"
            rejected[number] = [f"Project card status {detail}"]
            continue
        authoritative.append((priority, record))
    return authoritative, rejected


def _recovery_issue_record(number: int) -> dict:
    record = gh_json(["api", f"repos/{repo_slug()}/issues/{number}"])
    if not isinstance(record, dict):
        raise KernelError(f"GitHub returned malformed recovery issue reread for #{number}")
    state = str(record.get("state") or "")
    if (
        record.get("number") != number
        or not isinstance(record.get("title"), str)
        or not record["title"]
        or not isinstance(record.get("body"), str)
        or "pull_request" in record
        or state.upper() not in {"OPEN", "CLOSED"}
    ):
        raise KernelError(f"GitHub returned malformed recovery issue reread for #{number}")
    return record


def _live_recovery_errors(
    record: dict,
    *,
    active_reserved_paths: list[list[str]] | None = None,
    selected_reserved_paths: list[list[str]] | None = None,
) -> list[str]:
    errors: list[str] = []
    if str(record.get("state") or "").upper() != "OPEN":
        errors.append("issue is not open")
    errors.extend(
        triage_backlog.evaluate_with_states(
            record,
            backlog_dependency_states(_backlog_dependency_records([record])),
        )
    )
    errors.extend(_extra_backlog_errors(record))
    if active_reserved_paths is not None or selected_reserved_paths is not None:
        touches = parse_touches(str(record.get("body") or ""))
        conflict = _touches_conflict_error(
            touches,
            active_reserved_paths=active_reserved_paths,
            selected_reserved_paths=selected_reserved_paths,
        )
        if conflict is not None:
            errors.append(conflict)
    return errors


def _promote_recovery_candidate(
    record: dict,
    *,
    active_reserved_paths: list[list[str]] | None = None,
    selected_reserved_paths: list[list[str]] | None = None,
) -> tuple[dict, list[str]]:
    number = int(record["number"])
    live_record = record
    live_touches = parse_touches(str(record.get("body") or ""))

    def _pre_mutation_check() -> None:
        nonlocal live_record, live_touches
        live_record = _recovery_issue_record(number)
        live_touches = parse_touches(str(live_record.get("body") or ""))
        live_errors = _live_recovery_errors(
            live_record,
            active_reserved_paths=active_reserved_paths,
            selected_reserved_paths=selected_reserved_paths,
        )
        if live_errors:
            raise _RecoveryDriftError("; ".join(live_errors))

    try:
        triage_backlog.promote_issue(number, pre_mutation_check=_pre_mutation_check)
    except StatusPreconditionError as exc:
        if "expected 'Backlog'" in str(exc):
            raise _RecoveryDriftError(str(exc)) from exc
        raise
    return live_record, live_touches


def _recover_single_issue() -> dict[str, object] | None:
    candidates, rejected = _recoverable_backlog(backlog_issues())
    diagnostics = ["Ready idle; evaluated Backlog once"]
    if rejected:
        diagnostics.extend(_backlog_diagnostics(rejected))
    for _priority, record in candidates:
        number = int(record["number"])
        try:
            live_record, _live_touches = _promote_recovery_candidate(record)
        except _RecoveryDriftError as exc:
            diagnostics.extend(
                f"Backlog issue #{number} {error}; skipped" for error in str(exc).split("; ")
            )
            continue
        diagnostics.append(f"Promoted Backlog issue #{number} to Ready")
        return {
            "type": "issue",
            "issue": number,
            "title": str(live_record["title"]),
            "diagnostics": diagnostics,
        }
    return {"type": "idle", "diagnostics": diagnostics}


def _backlog_diagnostics(rejected: dict[int, list[str]]) -> list[str]:
    messages: list[str] = []
    for number in sorted(rejected):
        messages.extend(f"Backlog issue #{number} {error}; skipped" for error in rejected[number])
    return messages


def _batch_agents(agents: list[str]) -> list[str]:
    validated = [safe_agent(agent) for agent in agents]
    if len(validated) < 2:
        raise KernelError("batch mode requires repeated --agent")
    if len(set(validated)) != len(validated):
        raise KernelError("batch agent ids must be distinct")
    return validated


def _linked_issue_numbers(body: str) -> list[int]:
    return sorted({int(value) for value in re.findall(_RECOVERY_CANDIDATE_PATTERN, body or "")})


def _governed_open_prs(prs: list[dict]) -> dict[str, dict]:
    governed: dict[str, dict] = {}
    for pr in prs:
        number = _batch_number(pr, "Open PR")
        author_labels = [name for name in label_names(pr) if name.startswith(AUTHOR_PREFIX)]
        if not author_labels:
            continue
        if len(author_labels) > 1:
            raise KernelError(f"Open PR #{number} has contradictory author labels")
        author = safe_agent(author_labels[0][len(AUTHOR_PREFIX) :])
        if author in governed:
            raise KernelError(f"Open PRs for author '{author}' are ambiguous")
        governed[author] = pr
    return governed


def _governed_reserved_paths(governed_prs: dict[str, dict]) -> list[list[str]]:
    reserved: list[list[str]] = []
    for pr in governed_prs.values():
        number = int(pr["number"])
        linked = _linked_issue_numbers(str(pr.get("body") or ""))
        if len(linked) != 1:
            if not linked:
                raise KernelError(f"Open PR #{number} has no linked issue")
            raise KernelError(f"Open PR #{number} has multiple linked issues")
        linked_issue = issue(linked[0])
        reserved.append(parse_touches(str(linked_issue.get("body") or "")))
    return reserved


def _batch_number(record: dict, kind: str) -> int:
    number = record.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise KernelError(f"{kind} number must be a positive integer")
    return number


def _prs_by_batch_author(prs: list[dict], agents: list[str]) -> dict[str, list[dict]]:
    governed = _governed_open_prs(prs)
    authored: dict[str, list[dict]] = {agent: [] for agent in agents}
    for agent, pr in governed.items():
        if agent in authored:
            authored[agent].append(pr)
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


def _touches_conflict_error(
    touches: list[str],
    *,
    active_reserved_paths: list[list[str]] | None = None,
    selected_reserved_paths: list[list[str]] | None = None,
) -> str | None:
    if active_reserved_paths and any(
        _touches_overlap(touches, reserved) for reserved in active_reserved_paths
    ):
        return "touches conflict with active lane work"
    if selected_reserved_paths and any(
        _touches_overlap(touches, reserved) for reserved in selected_reserved_paths
    ):
        return "touches conflict with earlier selected recovery candidate"
    return None


def _ready_candidates(
    records: list[dict],
    issue_states: dict[int, str],
    *,
    batch: bool = False,
) -> tuple[list[tuple[int, int, dict, list[str]]], list[str], dict[str, int]]:
    priorities = {f"priority:p{value}": value for value in range(4)}
    ready: list[tuple[int, int, dict, list[str]]] = []
    diagnostics: list[tuple[int, str]] = []
    classification = {
        "total_ready": len(records), "executable_ready": 0, "human_gated": 0,
        "epics": 0, "dependency_blocked": 0, "malformed": 0,
    }
    for record in records:
        number = _batch_number(record, "Ready issue") if batch else int(record["number"])
        labels = label_names(record)
        category = _pre_dependency_category(labels)
        if category:
            classification[category] += 1
            continue
        dependency_numbers = dependencies(str(record.get("body") or ""))
        if any(issue_states.get(value) != "closed" for value in dependency_numbers):
            classification["dependency_blocked"] += 1
            continue
        priority_labels = [name for name in labels if name.startswith("priority:")]
        if len(priority_labels) > 1 or any(name not in priorities for name in priority_labels):
            classification["malformed"] += 1
            diagnostics.append(
                (
                    number,
                    f"Ready issue #{number} has contradictory or unsupported "
                    "priority labels; skipped",
                )
            )
            continue
        touches: list[str] = []
        if batch:
            try:
                touches = parse_touches(str(record.get("body") or ""))
            except KernelError as exc:
                classification["malformed"] += 1
                diagnostics.append(
                    (
                        number,
                        f"Ready issue #{number} has invalid touches: {exc}; skipped",
                    )
                )
                continue
        priority = priorities[priority_labels[0]] if priority_labels else 2
        ready.append((priority, number, record, touches))
        classification["executable_ready"] += 1
    ready.sort(key=lambda item: (item[0], item[1]))
    return (
        ready,
        [message for _number, message in sorted(diagnostics)],
        classification,
    )


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


def _batch_ready_candidates(
    records: list[dict],
    issue_states: dict[int, str] | None = None,
) -> tuple[list[tuple[int, int, dict, list[str]]], list[str], dict[str, int]]:
    ready, diagnostics, classification = _ready_candidates(
        records,
        dependency_states(records) if issue_states is None else issue_states,
        batch=True,
    )
    summary = _classification_summary(classification)
    if summary:
        diagnostics.insert(0, summary)
    return ready, diagnostics, classification


def select_batch(agents: list[str]) -> dict[str, object]:
    agents = _batch_agents(agents)
    prs_snapshot = open_prs()
    governed_prs = _governed_open_prs(prs_snapshot)
    lanes: list[dict[str, object]] = []
    free_lanes: list[dict[str, object]] = []

    for agent in agents:
        lane: dict[str, object] = {"agent": agent}
        if agent in governed_prs:
            lane["work"] = _open_pr_work(governed_prs[agent])
        else:
            free_lanes.append(lane)
        lanes.append(lane)

    diagnostics: list[str] = []
    classification: dict[str, int] | None = None

    if free_lanes:
        issues_snapshot = ready_issues()
        candidates, diagnostics, classification = _batch_ready_candidates(
            issues_snapshot,
            dependency_states(issues_snapshot),
        )
        if not candidates and not issues_snapshot:
            active_reserved = _governed_reserved_paths(governed_prs)
            candidates, diagnostics, classification = _recover_batch_candidates(
                len(free_lanes), active_reserved
            )
            reserved_paths = list(active_reserved)
        elif candidates:
            reserved_paths = _governed_reserved_paths(governed_prs)
        else:
            reserved_paths = []

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

    result: dict[str, object] = {
        "schema": "aru.fetch-next-work.batch/v1",
        "lanes": lanes,
        "diagnostics": diagnostics,
        "claim_status": "not-requested",
    }
    if classification and _classification_summary(classification):
        result["ready_classification"] = classification
    return result


def _recover_batch_candidates(
    lane_count: int,
    reserved_paths: list[list[str]],
) -> tuple[list[tuple[int, int, dict, list[str]]], list[str], dict[str, int]]:
    candidates, rejected = _recoverable_backlog(backlog_issues())
    diagnostics = ["Ready idle; evaluated Backlog once"]
    if rejected:
        diagnostics.extend(_backlog_diagnostics(rejected))
    if lane_count <= 0 or not candidates:
        return [], diagnostics, {
            "total_ready": 0, "executable_ready": 0, "human_gated": 0,
            "epics": 0, "dependency_blocked": 0, "malformed": 0,
        }
    selected: list[tuple[int, int, dict, list[str]]] = []
    selected_reserved_paths: list[list[str]] = []
    for priority, record in candidates:
        if len(selected) >= lane_count:
            break
        number = int(record["number"])
        touches = parse_touches(str(record.get("body") or ""))
        conflict = _touches_conflict_error(
            touches,
            active_reserved_paths=reserved_paths,
            selected_reserved_paths=selected_reserved_paths,
        )
        if conflict is not None:
            diagnostics.append(f"Backlog issue #{number} {conflict}; skipped")
            continue
        try:
            live_record, live_touches = _promote_recovery_candidate(
                record,
                active_reserved_paths=reserved_paths,
                selected_reserved_paths=selected_reserved_paths,
            )
        except _RecoveryDriftError as exc:
            diagnostics.extend(
                f"Backlog issue #{number} {error}; skipped" for error in str(exc).split("; ")
            )
            continue
        selected.append((priority, number, live_record, live_touches))
        selected_reserved_paths.append(live_touches)

    if selected:
        count = len(selected)
        noun = "candidate" if count == 1 else "candidates"
        diagnostics.append(f"Ready snapshot empty; recovered {count} Backlog {noun}")
        diagnostics.extend(
            f"Promoted Backlog issue #{number} to Ready"
            for _priority, number, _record, _touches in selected
        )
    return selected, diagnostics, {
        "total_ready": 0, "executable_ready": 0, "human_gated": 0,
        "epics": 0, "dependency_blocked": 0, "malformed": 0,
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
