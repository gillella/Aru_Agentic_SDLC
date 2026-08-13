#!/usr/bin/env python3
"""
factory_metrics.py - Reconstructs factory performance metrics from GitHub state:
  1. Constraint Dwell Time (median time in hours per board status)
  2. Review / Rework Rounds per PR (distribution & statistics)
  3. First-Pass Yield (percentage of PRs merged with 0 rework rounds)

Outputs plain text or JSON. No database or external service required.
"""

import argparse
import json
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from common import run_gh_json


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


def calculate_dwell_times(issue_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes dwell time per status for each issue and medians per status.
    issue_events is a list of event objects: {"status": str, "timestamp": str}
    """
    dwell_per_status: Dict[str, List[float]] = {}

    for i in range(len(issue_events) - 1):
        curr = issue_events[i]
        nxt = issue_events[i + 1]
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
    Computes rework rounds (number of requested changes / review iterations) per PR.
    pr_reviews is a list of PR objects:
      {"pr_number": int, "rework_rounds": int, "merged": bool}
    """
    rounds = [pr.get("rework_rounds", 0) for pr in pr_reviews]
    total_prs = len(pr_reviews)
    first_pass_count = sum(1 for pr in pr_reviews if pr.get("rework_rounds", 0) == 0 and pr.get("merged", False))

    first_pass_yield = round((first_pass_count / total_prs * 100.0), 2) if total_prs > 0 else 0.0
    median_rework = round(statistics.median(rounds), 2) if rounds else 0.0
    max_rework = max(rounds) if rounds else 0

    return {
        "total_prs": total_prs,
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
        f"  · Total PRs Evaluated : {rework_data.get('total_prs', 0)}",
        f"  · First-Pass Yield    : {rework_data.get('first_pass_yield_percent', 0.0):.2f}% ({rework_data.get('first_pass_count', 0)} merged with 0 rework)",
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
    """Fetches issue status history and PR review data from GitHub API."""
    prs_data = run_gh_json(["gh", "api", "repos/{owner}/{repo}/pulls?state=all&per_page=100"]) or []
    pr_list = []
    for pr in prs_data:
        pr_num = pr.get("number")
        merged = pr.get("merged_at") is not None
        reviews = run_gh_json(["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_num}/reviews"]) or []
        changes_requested = sum(1 for r in reviews if r.get("state") == "CHANGES_REQUESTED")
        pr_list.append({
            "pr_number": pr_num,
            "merged": merged,
            "rework_rounds": changes_requested,
        })
    return [], pr_list


def main():
    parser = argparse.ArgumentParser(description="Report factory telemetry: constraint dwell time, rework rounds, and first-pass yield.")
    parser.add_argument("--json", action="store_true", help="Output telemetry metrics in JSON format")
    parser.add_argument("--window-days", type=int, default=30, help="Window size in days for metrics calculation")
    args = parser.parse_args()

    issue_events, pr_reviews = fetch_github_telemetry(window_days=args.window_days)
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


if __name__ == "__main__":
    main()
