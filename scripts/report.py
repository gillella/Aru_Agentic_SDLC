#!/usr/bin/env python3
"""Read-only delivery report derived from GitHub evidence.

Describes a bounded merged-PR sample and the evidence visible on each final
head. It is not a history of gate attempts or a measure of defect rates.

Reads only. It writes nothing, creates no file, and keeps no state between runs:
every figure is re-derived from GitHub, which stays the single source of truth.

One honest limit, reported in the output. A `merge_pr.py` refusal raises in the
operator's terminal and leaves no GitHub record, because the Kernel deliberately
has no telemetry or ledger. Only gates with a GitHub-visible signal can be
counted; `unobservable_gates` names the declared gates that cannot be, so a low
block count is never mistaken for a clean run.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import check_ci
import fetch_pr_feedback as feedback
import policy
from common import REPOSITORY_AUTH, KernelError, gh_json, gh_paginated, json_print, repo_slug

# A declared gate is counted only where GitHub records the block. Values are
# gate ids from scripts/policy.toml; validate_gate_map() proves they still exist.
CHECK_GATES = {
    "aru-governed-pr": "exact-head-consumer-verification",
}
THREAD_GATE = "unresolved-findings"

CLAIM_LABEL = re.compile(r"^agent:")


class ReportError(KernelError):
    """Evidence was missing or unreadable, so no figure is reported."""


def validate_gate_map(declared: set[str] | None = None) -> None:
    """Refuse a mapping that names a gate the policy no longer declares."""
    ids = declared if declared is not None else policy.gate_ids()
    mapped = set(CHECK_GATES.values()) | {THREAD_GATE}
    unknown = mapped - ids
    if unknown:
        raise ReportError(f"report maps unknown gate ids: {sorted(unknown)}")


def unobservable_gates(declared: set[str] | None = None) -> list[str]:
    ids = declared if declared is not None else policy.gate_ids()
    return sorted(ids - (set(CHECK_GATES.values()) | {THREAD_GATE}))


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    match = re.fullmatch(r"(\d+)([dwh])", value.strip())
    if not match:
        raise ReportError("--since must look like 30d, 6w or 48h")
    amount, unit = int(match.group(1)), match.group(2)
    if amount < 1:
        raise ReportError("--since must be a positive window")
    delta = {"d": timedelta(days=amount), "w": timedelta(weeks=amount), "h": timedelta(hours=amount)}[unit]
    return (now or datetime.now(timezone.utc)) - delta


def _moment(value: object, field: str) -> datetime:
    """Parse a GitHub timestamp. An unreadable one refuses; it never becomes now()."""
    if not isinstance(value, str) or not value:
        raise ReportError(f"evidence is missing {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        return parsed
    except ValueError as exc:
        raise ReportError(f"evidence has an unreadable {field}: {value!r}") from exc


def post_first_review_commit(record: dict[str, Any]) -> bool:
    """Commit timestamp after first submitted review: activity proxy only."""
    reviews = record["reviews"]
    if not reviews:
        return False
    first = min(_moment(r.get("submitted_at"), "review submitted_at") for r in reviews)
    return any(_moment(c.get("committed_at"), "commit date") > first for c in record["commits"])


reworked = post_first_review_commit  # compatibility for Python callers


def claim_to_merge_hours(record: dict[str, Any]) -> float | None:
    """Hours from the exclusive claim to the merge, or None when unclaimed.

    Unclaimed is a real and legitimate state here (work shipped outside the
    ceremony), so it is excluded from the distribution rather than counted zero.
    """
    claimed = record.get("claimed_at")
    if claimed is None:
        return None
    return (_moment(record.get("merged_at"), "merged_at") - _moment(claimed, "claimed_at")).total_seconds() / 3600.0


def blocks_by_gate(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}

    def bump(gate: str) -> None:
        counts[gate] = counts.get(gate, 0) + 1

    for record in records:
        blocked: set[str] = set()
        for run in record["check_runs"]:
            gate = CHECK_GATES.get(str(run.get("name")))
            if gate and run.get("conclusion") != "success":
                blocked.add(gate)
        if record["unresolved_threads"] or record.get("unresolved_summaries", 0):
            blocked.add(THREAD_GATE)
        for gate in blocked:
            bump(gate)
    return counts


def summarize(records: list[dict[str, Any]], declared: set[str] | None = None) -> dict[str, Any]:
    """Derive every figure. Pure: the caller supplies the evidence."""
    validate_gate_map(declared)
    merged = len(records)
    durations = [h for h in (claim_to_merge_hours(r) for r in records) if h is not None]
    activity_count = sum(1 for r in records if post_first_review_commit(r))
    return {
        "merged_pull_requests": merged,
        "post_first_review_commit_activity": {
            "pull_requests": activity_count,
            "share": round(activity_count / merged, 3) if merged else None,
        },
        "review_events": {"changes_requested": sum(
            1 for r in records for review in r["reviews"] if review["state"] == "CHANGES_REQUESTED"
        )},
        "current_unresolved_findings": {
            "threads": sum(r["unresolved_threads"] for r in records),
            "blocking_summaries": sum(r.get("unresolved_summaries", 0) for r in records),
        },
        "review_threads": {
            "unresolved": sum(r["unresolved_threads"] for r in records),
            "unresolved_outdated": sum(r.get("unresolved_outdated_threads", 0) for r in records),
            "resolved": sum(r.get("resolved_threads", 0) for r in records),
        },
        "check_evidence": [
            {"pr": r["pr"], "final_head": r["head"], "runs": r["check_runs"]}
            for r in records if "head" in r
        ],
        "claim_to_merge_hours": {
            "measured": len(durations),
            "unclaimed": merged - len(durations),
            "median": round(median(durations), 1) if durations else None,
            "slowest": round(max(durations), 1) if durations else None,
        },
        "blocks_by_gate": dict(sorted(blocks_by_gate(records).items())),
        "unobservable_gates": unobservable_gates(declared),
    }


def fetch_sample(repo: str, since: datetime, limit: int, *,
                 until: datetime | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Select newest merged PRs by merge time from a complete closed inventory."""
    until = until or datetime.now(timezone.utc)
    pulls = gh_paginated(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100")
    eligible: list[dict[str, Any]] = []
    seen: set[int] = set()
    for pull in pulls:
        number = pull.get("number")
        if type(number) is not int or number in seen or "merged_at" not in pull:
            raise ReportError("closed pull-request inventory is malformed or duplicated")
        seen.add(number)
        if pull["merged_at"] is None:
            continue
        if since <= _moment(pull["merged_at"], "merged_at") <= until:
            eligible.append(pull)
    eligible.sort(key=lambda p: (_moment(p["merged_at"], "merged_at"), p["number"]), reverse=True)
    return [fetch_one(repo, pull) for pull in eligible[:limit]], len(eligible) > limit


def fetch_records(repo: str, since: datetime, limit: int) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need sampled records."""
    return fetch_sample(repo, since, limit)[0]


def fetch_one(repo: str, pull: dict[str, Any]) -> dict[str, Any]:
    number = pull.get("number")
    if not isinstance(number, int):
        raise ReportError("a pull request has no readable number")
    # The governed contexts run on the pull request head, never on the merge
    # commit GitHub writes onto the base branch. Preferring merge_commit_sha
    # reported zero check runs for every merged pull request -- precisely the
    # population this report exists to measure.
    head_data, base_data = pull.get("head"), pull.get("base")
    head = head_data.get("sha") if isinstance(head_data, dict) else None
    head_repo = head_data.get("repo") if isinstance(head_data, dict) else None
    base_repo = base_data.get("repo") if isinstance(base_data, dict) else None
    if (not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head)
            or not isinstance(base_data, dict)
            or not isinstance(head_repo, dict) or head_repo.get("full_name") != repo
            or not isinstance(base_repo, dict) or base_repo.get("full_name") != repo
            or not isinstance(head_data.get("ref"), str) or not head_data["ref"]
            or not isinstance(base_data.get("ref"), str) or not base_data["ref"]):
        raise ReportError(f"PR #{number}: final head or branch provenance is missing")
    _moment(pull.get("created_at"), "created_at")
    _moment(pull.get("merged_at"), "merged_at")
    commits = []
    for commit in gh_paginated(f"repos/{repo}/pulls/{number}/commits?per_page=100"):
        detail = commit.get("commit")
        committer = detail.get("committer") if isinstance(detail, dict) else None
        stamp = committer.get("date") if isinstance(committer, dict) else None
        _moment(stamp, "commit date")
        commits.append({"committed_at": stamp})
    if not commits:
        raise ReportError(f"PR #{number}: commit evidence is missing")
    reviews = []
    for review in gh_paginated(f"repos/{repo}/pulls/{number}/reviews?per_page=100"):
        state = review.get("state")
        if state not in feedback.STATES:
            raise ReportError(f"PR #{number}: review event is malformed")
        if state == "PENDING":
            continue
        _moment(review.get("submitted_at"), "review submitted_at")
        reviews.append({"state": state, "submitted_at": review["submitted_at"]})
    check_runs = governed_checks(repo, pull)
    threads, summaries, outdated, resolved = review_findings(repo, number, head)
    return {
        "pr": number,
        "head": head,
        "merged_at": pull["merged_at"],
        "commits": commits,
        "reviews": reviews,
        "check_runs": check_runs,
        "unresolved_threads": threads,
        "unresolved_summaries": summaries,
        "unresolved_outdated_threads": outdated,
        "resolved_threads": resolved,
        "claimed_at": claim_moment(repo, str(pull.get("body") or "")),
    }


def _object_inventory(endpoint: str, key: str, subject: str) -> list[dict[str, Any]]:
    """Read every page of a GitHub object connection and verify total_count."""
    pages = gh_json(["api", "--paginate", "--slurp", endpoint])
    if not isinstance(pages, list) or not pages:
        raise ReportError(f"{subject} pagination is unreadable")
    records: list[dict[str, Any]] = []
    total: int | None = None
    seen_ids: set[int] = set()
    for page in pages:
        if (not isinstance(page, dict) or type(page.get("total_count")) is not int
                or page["total_count"] < 0
                or not isinstance(page.get(key), list)
                or any(not isinstance(item, dict) for item in page[key])):
            raise ReportError(f"{subject} inventory is malformed")
        if total is not None and page["total_count"] != total:
            raise ReportError(f"{subject} inventory changed during pagination")
        total = page["total_count"]
        for item in page[key]:
            identity = item.get("id")
            if not check_ci._positive(identity) or identity in seen_ids:
                raise ReportError(f"{subject} inventory has missing or duplicate IDs")
            seen_ids.add(identity)
        records.extend(page[key])
    if len(records) != total:
        raise ReportError(f"{subject} inventory is truncated")
    return records


def governed_checks(repo: str, pull: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate final-head governed Actions runs using the canonical CI bindings.

    Each Actions run contributes its latest attempt once. Multiple runs for the
    same PR are retained, but blocks_by_gate counts affected PRs, not attempts.
    """
    head = pull["head"]["sha"]
    number = pull["number"]
    pr = {"number": number, "headRefOid": head,
          "headRefName": pull["head"]["ref"], "baseRefName": pull["base"]["ref"],
          "createdAt": pull["created_at"]}
    endpoint = f"repos/{repo}/actions/workflows/governed-pr.yml"
    workflow = gh_json(["api", endpoint])
    if (not isinstance(workflow, dict) or not check_ci._positive(workflow.get("id"))
            or workflow.get("path") != ".github/workflows/governed-pr.yml"
            or workflow.get("state") != "active"):
        raise ReportError(f"PR #{number}: governed workflow identity is unavailable")
    runs = _object_inventory(f"{endpoint}/runs?head_sha={head}&per_page=100",
                             "workflow_runs", f"PR #{number} workflow-run")
    checks = _object_inventory(f"repos/{repo}/commits/{head}/check-runs?per_page=100&filter=latest",
                               "check_runs", f"PR #{number} check-run")
    by_run: dict[int, dict[str, Any]] = {}
    for check in checks:
        if check_ci.check_name(check) != check_ci.DEFAULT_REQUIRED_CHECK:
            continue
        run_id = check_ci._check_binding(check, head, repo)
        if run_id in by_run:
            raise ReportError(f"PR #{number}: duplicate governed check run")
        by_run[run_id] = check
    seen_runs: set[int] = set()
    seen_suites: set[int] = set()
    observations: list[dict[str, Any]] = []
    merged_at = _moment(pull["merged_at"], "merged_at")
    for run in runs:
        if run.get("workflow_id") != workflow["id"]:
            raise ReportError(f"PR #{number}: workflow-run identity conflicts")
        current = check_ci._run_binding(run, pr, repo, merged_at)
        if run["id"] in seen_runs or run["check_suite_id"] in seen_suites:
            raise ReportError(f"PR #{number}: duplicate governed workflow run")
        seen_runs.add(run["id"])
        seen_suites.add(run["check_suite_id"])
        check = by_run.get(run["id"])
        state = check_ci._bound_state(run, check)
        if current:
            observations.append(_verified_observation(number, head, pr, run, check, state, merged_at))
    if set(by_run) - seen_runs:
        raise ReportError(f"PR #{number}: governed check has no workflow run")
    if not observations:
        raise ReportError(f"PR #{number}: no verified final-head governed run")
    return observations


def _verified_observation(number: int, head: str, pr: dict[str, Any], run: dict[str, Any],
                          check: dict[str, Any] | None, state: str, merged_at: datetime) -> dict[str, Any]:
    if check is None or state == "pending":
        raise ReportError(f"PR #{number}: final-head governed run is incomplete")
    if not (_moment(pr["createdAt"], "created_at")
            <= _moment(run["created_at"], "run created_at")
            <= _moment(run.get("run_started_at"), "run_started_at")
            <= _moment(check.get("started_at"), "check started_at")
            <= _moment(check.get("completed_at"), "check completed_at")
            <= _moment(run.get("updated_at"), "run updated_at") <= merged_at):
        raise ReportError(f"PR #{number}: governed run is outside its pre-merge lifetime")
    return {"name": check_ci.DEFAULT_REQUIRED_CHECK, "conclusion": state,
            "run_id": run["id"], "run_attempt": run["run_attempt"], "head": head,
            "workflow_id": run["workflow_id"], "check_id": check["id"],
            "check_suite_id": run["check_suite_id"], "event": run["event"],
            "app": check["app"]["slug"], "app_id": check["app"]["id"]}


THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$after){
        nodes{id isResolved isOutdated comments(first:100){nodes{pullRequestReview{databaseId}} pageInfo{hasNextPage}}}
        pageInfo{hasNextPage endCursor}
      }
    }
  }
}
"""


def _thread_review_ids(thread: object, number: int, seen_threads: set[str]) -> set[int] | None:
    if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
            or not thread["id"] or thread["id"] in seen_threads
            or type(thread.get("isResolved")) is not bool
            or type(thread.get("isOutdated")) is not bool):
        raise ReportError(f"PR #{number}: review-thread evidence is malformed")
    seen_threads.add(thread["id"])
    comments = thread.get("comments")
    page_info = comments.get("pageInfo") if isinstance(comments, dict) else None
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if (not isinstance(page_info, dict) or type(page_info.get("hasNextPage")) is not bool
            or page_info["hasNextPage"] or not isinstance(nodes, list) or not nodes):
        raise ReportError(f"PR #{number}: review comments are missing or truncated")
    if thread["isResolved"]:
        return None
    review_ids: set[int] = set()
    for comment in nodes:
        review = comment.get("pullRequestReview") if isinstance(comment, dict) else None
        review_id = review.get("databaseId") if isinstance(review, dict) else None
        if review_id is not None:
            if type(review_id) is not int or review_id <= 0:
                raise ReportError(f"PR #{number}: review-comment identity is malformed")
            review_ids.add(review_id)
    return review_ids


def _thread_inventory(repo: str, number: int) -> tuple[int, int, int, set[int]]:
    """Count unresolved threads, including outdated ones, and their review IDs."""
    if repo.count("/") != 1:
        raise ReportError(f"repository {repo!r} is not owner/name")
    owner, name = repo.split("/", 1)
    cursor: str | None = None
    open_threads = 0
    outdated_threads = 0
    resolved_threads = 0
    review_ids: set[int] = set()
    seen_cursors: set[str] = set()
    seen_threads: set[str] = set()
    while True:
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}",
                "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args.extend(["-F", f"after={cursor}"])
        data = gh_json(args, auth=REPOSITORY_AUTH)
        if not isinstance(data, dict) or data.get("errors"):
            raise ReportError(f"PR #{number}: review-thread evidence is unreadable")
        root = data.get("data")
        repository = root.get("repository") if isinstance(root, dict) else None
        pull = repository.get("pullRequest") if isinstance(repository, dict) else None
        connection = (pull or {}).get("reviewThreads")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise ReportError(f"PR #{number}: review-thread evidence is unreadable")
        for thread in connection["nodes"]:
            ids = _thread_review_ids(thread, number, seen_threads)
            if ids is None:
                resolved_threads += 1
            else:
                open_threads += 1
                outdated_threads += int(thread["isOutdated"])
                review_ids.update(ids)
        page = connection.get("pageInfo")
        if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
            raise ReportError(f"PR #{number}: review-thread pagination is incomplete")
        if not page["hasNextPage"]:
            return open_threads, outdated_threads, resolved_threads, review_ids
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ReportError(f"PR #{number}: review-thread pagination is broken")
        seen_cursors.add(cursor)


def unresolved_threads(repo: str, number: int) -> int:
    return _thread_inventory(repo, number)[0]


def review_findings(repo: str, number: int, head: str) -> tuple[int, int, int, int]:
    """Use the feedback helper's severity and explicit-resolution rules."""
    threads, outdated, resolved, linked_reviews = _thread_inventory(repo, number)
    owner, name = repo.split("/", 1)
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[int] = set()
    identity: tuple[str, str] | None = None
    reviews: list[dict[str, Any]] = []
    while True:
        args = ["api", "graphql", "-f", f"query={feedback.SUMMARY_QUERY}",
                "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
        if cursor:
            args.extend(["-F", f"after={cursor}"])
        data = gh_json(args, auth=REPOSITORY_AUTH)
        if not isinstance(data, dict) or data.get("errors"):
            raise ReportError(f"PR #{number}: review-summary evidence is unreadable")
        root = data.get("data")
        repository = root.get("repository") if isinstance(root, dict) else None
        pr = repository.get("pullRequest") if isinstance(repository, dict) else None
        connection = pr.get("reviews") if isinstance(pr, dict) else None
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise ReportError(f"PR #{number}: review-summary inventory is incomplete")
        if pr.get("headRefOid") != head:
            raise ReportError(f"PR #{number}: review-summary head changed")
        current = (head, feedback._identity(pr.get("author")))
        if identity is not None and identity != current:
            raise ReportError(f"PR #{number}: review-summary identity changed")
        identity = current
        for node in connection["nodes"]:
            review = feedback._submitted_review(node)
            if review is None:
                continue
            if review["databaseId"] in seen_ids:
                raise ReportError(f"PR #{number}: duplicate review summary")
            seen_ids.add(review["databaseId"])
            reviews.append(review)
        cursor = feedback._next_cursor(connection, seen_cursors)
        if cursor is None:
            break
    unresolved = sum(
        1 for review in reviews
        if review["databaseId"] not in linked_reviews
        and feedback.BLOCKING_LABEL.search(review["body"])
        and not any(feedback._resolves(candidate, review, head, identity[1]) for candidate in reviews)
    )
    return threads, unresolved, outdated, resolved


def claim_moment(repo: str, body: str) -> str | None:
    """When the closing issue was claimed, from its label timeline."""
    match = re.search(r"(?i)\bcloses\s+#(\d+)\b", body)
    if not match:
        return None
    events = gh_paginated(f"repos/{repo}/issues/{match.group(1)}/timeline?per_page=100")
    stamps = [
        e.get("created_at")
        for e in events
        if e.get("event") == "labeled" and CLAIM_LABEL.match(str((e.get("label") or {}).get("name", "")))
    ]
    for stamp in stamps:
        _moment(stamp, "claim timestamp")
    return min(stamps) if stamps else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", help="OWNER/REPO; defaults to this checkout's remote")
    parser.add_argument("--since", default="30d", help="window such as 30d, 6w, 48h (default 30d)")
    parser.add_argument("--limit", type=int, default=50, help="most recent merged PRs to read (default 50)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.limit < 1:
            raise ReportError("--limit must be positive")
        repo = args.repo or repo_slug()
        observed_at = datetime.now(timezone.utc)
        since = parse_since(args.since, now=observed_at)
        records, truncated = fetch_sample(repo, since, args.limit, until=observed_at)
        report = summarize(records)
        report["repository"], report["since"] = repo, since.isoformat()
        report["sample"] = {"population": "merged pull requests", "window_start": since.isoformat(),
                            "window_end": observed_at.isoformat(), "observed_at": datetime.now(timezone.utc).isoformat(),
                            "requested_limit": args.limit, "truncated": truncated}
        report["check_scope"] = "verified final-head governed Actions runs; unique affected PRs; latest attempt per run"
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(report)
        return 0
    print(f"{report['repository']}  since {args.since}")
    print(f"  merged pull requests : {report['merged_pull_requests']}")
    sample = report["sample"]
    print(f"  merged-only window   : {sample['window_start']} to {sample['window_end']}")
    print(f"  observed at          : {sample['observed_at']}; limit {sample['requested_limit']}; truncated {sample['truncated']}")
    activity = report["post_first_review_commit_activity"]
    print(f"  post-review activity : {activity['pull_requests']} PRs with a commit after first review"
          " (activity proxy, not a defect rate)")
    findings = report["current_unresolved_findings"]
    print(f"  current findings     : {findings['threads']} unresolved threads,"
          f" {findings['blocking_summaries']} blocking review summaries")
    threads = report["review_threads"]
    print(f"  thread detail        : {threads['unresolved_outdated']} unresolved outdated,"
          f" {threads['resolved']} resolved")
    print(f"  review history       : {report['review_events']['changes_requested']} CHANGES_REQUESTED events"
          " (not proof of a merge refusal)")
    claim = report["claim_to_merge_hours"]
    print(f"  claim to merge       : median {claim['median']}h, slowest {claim['slowest']}h"
          f" ({claim['measured']} measured, {claim['unclaimed']} unclaimed)")
    print("  observed blocks      : " + (
        ", ".join(f"{gate}={count}" for gate, count in report["blocks_by_gate"].items()) or "none"))
    print("  check scope          : " + report["check_scope"])
    print(f"  gates with no GitHub-visible signal: {len(report['unobservable_gates'])}"
          " (a low block count is not proof of a clean run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
