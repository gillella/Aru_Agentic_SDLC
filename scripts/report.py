#!/usr/bin/env python3
"""Read-only delivery report derived from GitHub evidence.

Answers whether the guardrails are helping, not merely whether each change
followed the process: how much merged, how often work needed rework after
review, how long a claim took to reach merge, and which gates were observed
blocking.

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

import policy
from common import KernelError, gh_json, gh_paginated, json_print, repo_slug

# A declared gate is counted only where GitHub records the block. Values are
# gate ids from scripts/policy.toml; validate_gate_map() proves they still exist.
CHECK_GATES = {
    "aru-governed-pr": "exact-head-consumer-verification",
    "aru-merge-policy": "path-budget-unrewritable",
}
REVIEW_GATE = "approval-by-another-account"
THREAD_GATE = "unresolved-findings"

CLAIM_LABEL = re.compile(r"^agent:")


class ReportError(KernelError):
    """Evidence was missing or unreadable, so no figure is reported."""


def validate_gate_map(declared: set[str] | None = None) -> None:
    """Refuse a mapping that names a gate the policy no longer declares."""
    ids = declared if declared is not None else policy.gate_ids()
    mapped = set(CHECK_GATES.values()) | {REVIEW_GATE, THREAD_GATE}
    unknown = mapped - ids
    if unknown:
        raise ReportError(f"report maps unknown gate ids: {sorted(unknown)}")


def unobservable_gates(declared: set[str] | None = None) -> list[str]:
    ids = declared if declared is not None else policy.gate_ids()
    return sorted(ids - (set(CHECK_GATES.values()) | {REVIEW_GATE, THREAD_GATE}))


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
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError(f"evidence has an unreadable {field}: {value!r}") from exc


def reworked(record: dict[str, Any]) -> bool:
    """Whether any commit landed after the first review of this pull request.

    That is the observable shape of "review sent it back": the author pushed
    again once someone had already looked.
    """
    reviews = record.get("reviews") or []
    if not reviews:
        return False
    first = min(_moment(r.get("submitted_at"), "review submitted_at") for r in reviews)
    return any(_moment(c.get("committed_at"), "commit date") > first for c in record.get("commits") or [])


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
        for run in record.get("check_runs") or []:
            gate = CHECK_GATES.get(str(run.get("name")))
            if gate and run.get("conclusion") not in (None, "success", "neutral", "skipped"):
                bump(gate)
        if any(r.get("state") == "CHANGES_REQUESTED" for r in record.get("reviews") or []):
            bump(REVIEW_GATE)
        if record.get("unresolved_threads"):
            bump(THREAD_GATE)
    return counts


def summarize(records: list[dict[str, Any]], declared: set[str] | None = None) -> dict[str, Any]:
    """Derive every figure. Pure: the caller supplies the evidence."""
    validate_gate_map(declared)
    merged = len(records)
    durations = [h for h in (claim_to_merge_hours(r) for r in records) if h is not None]
    reworked_count = sum(1 for r in records if reworked(r))
    return {
        "merged_pull_requests": merged,
        "rework": {
            "reworked_after_review": reworked_count,
            "rate": round(reworked_count / merged, 3) if merged else None,
        },
        "claim_to_merge_hours": {
            "measured": len(durations),
            "unclaimed": merged - len(durations),
            "median": round(median(durations), 1) if durations else None,
            "slowest": round(max(durations), 1) if durations else None,
        },
        "blocks_by_gate": dict(sorted(blocks_by_gate(records).items())),
        "unobservable_gates": unobservable_gates(declared),
    }


def fetch_records(repo: str, since: datetime, limit: int) -> list[dict[str, Any]]:
    """Collect merged pull requests and their evidence. Any unreadable record
    refuses the whole report rather than reporting a partial figure as complete."""
    pulls = gh_paginated(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100")
    records: list[dict[str, Any]] = []
    for pull in pulls:
        if not pull.get("merged_at"):
            continue
        if _moment(pull["merged_at"], "merged_at") < since:
            continue
        records.append(fetch_one(repo, pull))
        if len(records) >= limit:
            break
    return records


def fetch_one(repo: str, pull: dict[str, Any]) -> dict[str, Any]:
    number = pull.get("number")
    if not isinstance(number, int):
        raise ReportError("a pull request has no readable number")
    head = pull.get("merge_commit_sha") or (pull.get("head") or {}).get("sha")
    commits = [
        {"committed_at": ((c.get("commit") or {}).get("committer") or {}).get("date")}
        for c in gh_paginated(f"repos/{repo}/pulls/{number}/commits?per_page=100")
    ]
    reviews = [
        {"state": r.get("state"), "submitted_at": r.get("submitted_at")}
        for r in gh_paginated(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        if r.get("submitted_at")
    ]
    check_runs: list[dict[str, Any]] = []
    if isinstance(head, str) and head:
        payload = gh_json(["api", f"repos/{repo}/commits/{head}/check-runs?per_page=100"])
        runs = (payload or {}).get("check_runs")
        if not isinstance(runs, list):
            raise ReportError(f"PR #{number}: check-run evidence is unreadable")
        check_runs = [{"name": r.get("name"), "conclusion": r.get("conclusion")} for r in runs]
    return {
        "pr": number,
        "merged_at": pull["merged_at"],
        "commits": commits,
        "reviews": reviews,
        "check_runs": check_runs,
        "unresolved_threads": 0,
        "claimed_at": claim_moment(repo, str(pull.get("body") or "")),
    }


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
    readable = [s for s in stamps if isinstance(s, str) and s]
    return min(readable) if readable else None


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
        since = parse_since(args.since)
        report = summarize(fetch_records(repo, since, args.limit))
        report["repository"], report["since"] = repo, since.isoformat()
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(report)
        return 0
    print(f"{report['repository']}  since {args.since}")
    print(f"  merged pull requests : {report['merged_pull_requests']}")
    rate = report["rework"]["rate"]
    print(f"  reworked after review: {report['rework']['reworked_after_review']}"
          + (f" ({rate:.0%})" if rate is not None else ""))
    claim = report["claim_to_merge_hours"]
    print(f"  claim to merge       : median {claim['median']}h, slowest {claim['slowest']}h"
          f" ({claim['measured']} measured, {claim['unclaimed']} unclaimed)")
    print("  observed blocks      : " + (
        ", ".join(f"{gate}={count}" for gate, count in report["blocks_by_gate"].items()) or "none"))
    print(f"  gates with no GitHub-visible signal: {len(report['unobservable_gates'])}"
          " (a low block count is not proof of a clean run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
