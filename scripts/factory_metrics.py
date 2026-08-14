#!/usr/bin/env python3
"""
factory_metrics.py - Reconstructs factory performance metrics from GitHub state:
  1. Constraint Dwell Time (median time in hours per board status)
  2. Review / Rework Rounds per PR (distribution & statistics)
  3. First-Pass Yield (percentage of completed/merged PRs with 0 rework rounds)

Outputs plain text or JSON. No database or external service required.
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from common import run_cmd


def parse_iso(ts_str: str) -> Optional[datetime]:
    """Parses ISO 8601 timestamp string into timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def fetch_paginated_gh_api(endpoint: str) -> Optional[List[Dict[str, Any]]]:
    """Fetches paginated JSON array stream from gh api."""
    cmd = ["gh", "api", "--paginate", endpoint]
    code, stdout, stderr = run_cmd(cmd, check=False)
    if code != 0 or not stdout.strip():
        if code != 0:
            sys.stderr.write(f"[ERROR] GitHub API request failed: {stderr}\n")
        return None

    raw = stdout.strip()
    # gh api --paginate can return concatenated JSON arrays like: `[...][...]`
    items: List[Dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(raw):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos >= len(raw):
            break
        try:
            chunk, idx = decoder.raw_decode(raw[pos:])
            if isinstance(chunk, list):
                items.extend(chunk)
            elif isinstance(chunk, dict):
                items.append(chunk)
            pos += idx
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[ERROR] Failed to decode API response JSON: {e}\n")
            return None
    return items


def calculate_dwell_times(issue_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes dwell time per status across issues and medians per status.
    issue_events is a list of event objects:
      {"issue_id": Optional[int], "status": str, "timestamp": str}
    """
    dwell_per_status: Dict[str, List[float]] = {}
    by_issue: Dict[Any, List[Dict[str, Any]]] = {}

    for ev in issue_events:
        iid = ev.get("issue_id", 0)
        by_issue.setdefault(iid, []).append(ev)

    for iid, events in by_issue.items():
        sorted_evs = sorted(events, key=lambda e: parse_iso(e.get("timestamp") or "") or datetime.min.replace(tzinfo=timezone.utc))
        for i in range(len(sorted_evs) - 1):
            curr = sorted_evs[i]
            nxt = sorted_evs[i + 1]
            status = curr.get("status")
            t1 = parse_iso(curr.get("timestamp") or "")
            t2 = parse_iso(nxt.get("timestamp") or "")
            if status and t1 and t2 and t2 >= t1:
                duration_hours = (t2 - t1).total_seconds() / 3600.0
                dwell_per_status.setdefault(status, []).append(duration_hours)

    medians: Dict[str, float] = {}
    for status, times in dwell_per_status.items():
        medians[status] = round(statistics.median(times), 2) if times else 0.0

    return {
        "raw_hours_per_status": dwell_per_status,
        "median_hours_per_status": medians,
    }


def calculate_rework_rounds(pr_reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes rework rounds (number of requested changes / review iterations) per PR,
    restricting first-pass yield to completed (merged) outcomes.
    pr_reviews is a list of PR objects:
      {"pr_number": int, "rework_rounds": int, "merged": bool}
    """
    total_evaluated = len(pr_reviews)
    merged_prs = [pr for pr in pr_reviews if pr.get("merged", False)]
    total_merged = len(merged_prs)

    rounds = [pr.get("rework_rounds", 0) for pr in merged_prs]
    first_pass_count = sum(1 for pr in merged_prs if pr.get("rework_rounds", 0) == 0)

    first_pass_yield = round((first_pass_count / total_merged * 100.0), 2) if total_merged > 0 else 0.0
    median_rework = round(statistics.median(rounds), 2) if rounds else 0.0
    max_rework = max(rounds) if rounds else 0

    return {
        "total_prs_evaluated": total_evaluated,
        "total_merged_prs": total_merged,
        "first_pass_count": first_pass_count,
        "first_pass_yield_percent": first_pass_yield,
        "median_rework_rounds": median_rework,
        "max_rework_rounds": max_rework,
        "rework_distribution": {r: rounds.count(r) for r in sorted(set(rounds))} if rounds else {},
    }


def format_text_report(dwell_data: Dict[str, Any], rework_data: Dict[str, Any]) -> str:
    """Formats telemetry metrics into readable plain text report."""
    lines = [
        "=== Aru_Agentic_SDLC Factory Telemetry ===",
        "",
        "--- Constraint Dwell Time (Median Hours per Board Status) ---",
    ]
    medians = dwell_data.get("median_hours_per_status", {})
    if not medians:
        lines.append("  (no dwell data recorded)")
    else:
        for status, hours in medians.items():
            lines.append(f"  · {status:<15}: {hours:.2f} hours")

    lines.extend([
        "",
        "--- Review Rework & Yield ---",
        f"  · Total PRs Evaluated : {rework_data.get('total_prs_evaluated', 0)}",
        f"  · Total Merged PRs    : {rework_data.get('total_merged_prs', 0)}",
        f"  · First-Pass Yield    : {rework_data.get('first_pass_yield_percent', 0.0):.2f}% ({rework_data.get('first_pass_count', 0)} of {rework_data.get('total_merged_prs', 0)} merged with 0 rework)",
        f"  · Median Rework Rounds: {rework_data.get('median_rework_rounds', 0.0):.2f}",
        f"  · Max Rework Rounds   : {rework_data.get('max_rework_rounds', 0)}",
    ])

    dist = rework_data.get("rework_distribution", {})
    if dist:
        lines.append("  · Rework Distribution:")
        for r_count, num_prs in dist.items():
            lines.append(f"      - {r_count} rework round(s): {num_prs} PR(s)")

    return "\n".join(lines)


def fetch_github_telemetry(window_days: int = 30) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetches issue status history and PR review data from GitHub API respecting window_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    prs_data = fetch_paginated_gh_api("repos/{owner}/{repo}/pulls?state=all&per_page=100")
    if prs_data is None:
        raise RuntimeError("Failed to fetch pull requests from GitHub API.")

    pr_list = []
    for pr in prs_data:
        created_at = parse_iso(pr.get("created_at") or "")
        merged_at = parse_iso(pr.get("merged_at") or "")
        updated_at = parse_iso(pr.get("updated_at") or "")
        latest_ts = merged_at or updated_at or created_at
        if latest_ts and latest_ts < cutoff:
            continue

        pr_num = pr.get("number")
        merged = merged_at is not None

        reviews = fetch_paginated_gh_api(f"repos/{{owner}}/{{repo}}/pulls/{pr_num}/reviews")
        if reviews is None:
            raise RuntimeError(f"Failed to fetch reviews for PR #{pr_num}.")

        comments = fetch_paginated_gh_api(f"repos/{{owner}}/{{repo}}/pulls/{pr_num}/comments")
        if comments is None:
            raise RuntimeError(f"Failed to fetch review comments for PR #{pr_num}.")

        review_ids_with_rework = {r.get("id") for r in reviews if r.get("state") == "CHANGES_REQUESTED" and r.get("id")}
        for c in comments:
            # Root review comments indicate a finding submitted in that review cycle
            if not c.get("in_reply_to_id") and c.get("pull_request_review_id"):
                review_ids_with_rework.add(c["pull_request_review_id"])

        rework_rounds = len(review_ids_with_rework)

        pr_list.append({
            "pr_number": pr_num,
            "merged": merged,
            "rework_rounds": rework_rounds,
        })

    issues_data = fetch_paginated_gh_api("repos/{owner}/{repo}/issues?state=all&per_page=100")
    if issues_data is None:
        raise RuntimeError("Failed to fetch issues from GitHub API.")

    issue_events = []
    for issue in issues_data:
        # Ignore PRs returned in issues endpoint
        if issue.get("pull_request"):
            continue
        created_at = parse_iso(issue.get("created_at") or "")
        updated_at = parse_iso(issue.get("updated_at") or "")
        latest_ts = updated_at or created_at
        if latest_ts and latest_ts < cutoff:
            continue

        num = issue.get("number")
        events = fetch_paginated_gh_api(f"repos/{{owner}}/{{repo}}/issues/{num}/timeline")
        if events is None:
            raise RuntimeError(f"Failed to fetch timeline for Issue #{num}.")

        for ev in events:
            ev_name = ev.get("event")
            if ev_name == "labeled":
                lbl_name = (ev.get("label") or {}).get("name", "")
                if lbl_name.lower().startswith("status:"):
                    status_val = lbl_name[7:].strip()
                    issue_events.append({
                        "issue_id": num,
                        "status": status_val,
                        "timestamp": ev.get("created_at"),
                    })

    return issue_events, pr_list


def main():
    parser = argparse.ArgumentParser(description="Report factory telemetry: constraint dwell time, rework rounds, and first-pass yield.")
    parser.add_argument("--json", action="store_true", help="Output telemetry metrics in JSON format")
    parser.add_argument("--window-days", type=int, default=30, help="Window size in days for metrics calculation")
    args = parser.parse_args()

    try:
        issue_events, pr_reviews = fetch_github_telemetry(window_days=args.window_days)
    except RuntimeError as e:
        sys.stderr.write(f"[ERROR] Telemetry extraction aborted: {e}\n")
        return 1

    dwell_data = calculate_dwell_times(issue_events)
    rework_data = calculate_rework_rounds(pr_reviews)

    if args.json:
        result = {
            "dwell_time": dwell_data,
            "rework_yield": rework_data,
        }
        print(json.dumps(result, indent=2))
    else:
        print(format_text_report(dwell_data, rework_data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
