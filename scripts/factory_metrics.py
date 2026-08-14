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
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import run_cmd
from merge_pr import linked_issues


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


def _label_value(labels: List[Any], prefix: str) -> str:
    """Return a governed label suffix, or the explicit unavailable marker."""
    for label in labels:
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        if name.lower().startswith(prefix.lower()):
            return name.split(":", 1)[1]
    return "unavailable"


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
            elif isinstance(chunk, dict) and isinstance(chunk.get("workflow_runs"), list):
                items.extend(chunk["workflow_runs"])
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
        if not ev.get("status"):
            continue
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


def fetch_github_telemetry(
    window_days: int = 30,
    include_closed_details: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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
            "issue_numbers": linked_issues(pr.get("body") or ""),
            "agent": _label_value(pr.get("labels") or [], "author:"),
            "family": _label_value(pr.get("labels") or [], "family:"),
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

        claims: List[Tuple[str, str]] = []
        active_agents: set[str] = set()
        finalized_agents: set[str] = set()
        in_progress_at: Optional[str] = None
        done_at: Optional[str] = None
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
                    if status_val.lower() == "in-progress" and not in_progress_at:
                        in_progress_at = ev.get("created_at")
                    if status_val.lower() == "done":
                        done_at = ev.get("created_at")
                        finalized_agents = set(active_agents)
                elif lbl_name.lower().startswith("agent:"):
                    agent = lbl_name.split(":", 1)[1]
                    claims.append((agent, ev.get("created_at") or ""))
                    active_agents.add(agent)
            elif ev_name == "unlabeled":
                lbl_name = (ev.get("label") or {}).get("name", "")
                if lbl_name.lower().startswith("agent:"):
                    active_agents.discard(lbl_name.split(":", 1)[1])
            elif ev_name == "closed" and active_agents:
                finalized_agents = set(active_agents)

        closed_at = issue.get("closed_at")
        parsed_closed_at = parse_iso(closed_at or "")
        if include_closed_details and parsed_closed_at and parsed_closed_at >= cutoff:
            issue_events.append({
                "event_type": "closed_issue",
                "issue_id": num,
                "title": issue.get("title") or "",
                "closed_at": closed_at,
                "claim_started_at": in_progress_at or (claims[0][1] if claims else None),
                "done_at": done_at or closed_at,
                "agents": sorted(finalized_agents or active_agents),
                "issue_type": _label_value(issue.get("labels") or [], "type:"),
            })

    return issue_events, pr_list


def load_local_usage(path: Optional[str]) -> List[Dict[str, Any]]:
    """Load explicit CLI usage records from a JSON array/object or JSONL file."""
    if not path:
        return []
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read local usage file: {exc}") from exc
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("records", [parsed])
    except json.JSONDecodeError:
        try:
            parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local usage file is not valid JSON or JSONL: {exc}") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise RuntimeError("Local usage must be a JSON list/object or JSONL object stream.")
    if any(
        isinstance(item.get("issue_number"), bool)
        or not isinstance(item.get("issue_number"), int)
        or item["issue_number"] <= 0
        for item in parsed
    ):
        raise RuntimeError("Every local usage record must have a positive integer issue_number.")
    return parsed


def _number(record: Dict[str, Any], key: str) -> Optional[float]:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _measurement(records: List[Dict[str, Any]], key: str, unit: str) -> Dict[str, Any]:
    values = [value for record in records if (value := _number(record, key)) is not None]
    return {
        "availability": "measured" if values else "unavailable",
        "value": round(sum(values), 6) if values else None,
        "unit": unit,
    }


def _token_measurement(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    values: List[float] = []
    for record in records:
        total = _number(record, "total_tokens")
        if total is None:
            input_tokens = _number(record, "input_tokens")
            output_tokens = _number(record, "output_tokens")
            total = input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None
        if total is not None:
            values.append(total)
    return {
        "availability": "measured" if values else "unavailable",
        "value": int(sum(values)) if values else None,
        "unit": "tokens",
    }


def _usage_breakdown(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for record in records:
        key = (str(record.get("agent") or "unavailable"), str(record.get("family") or "unavailable"))
        groups.setdefault(key, []).append(record)
    return [
        {
            "agent": agent,
            "family": family,
            "tokens": _token_measurement(items),
            "cost_usd": _measurement(items, "cost_usd", "USD"),
            "human_oversight_minutes": _measurement(items, "human_oversight_minutes", "minutes"),
            "infrastructure_cost_usd": _measurement(items, "infrastructure_cost_usd", "USD"),
        }
        for (agent, family), items in sorted(groups.items())
    ]


def fetch_ci_runs(window_days: int) -> List[Dict[str, Any]]:
    runs = fetch_paginated_gh_api("repos/{owner}/{repo}/actions/runs?event=pull_request&per_page=100")
    if runs is None:
        raise RuntimeError("Failed to fetch GitHub Actions runs.")
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    return [
        run for run in runs
        if (created_at := parse_iso(run.get("created_at") or "")) and created_at >= cutoff
    ]


def _outlier_threshold(values: List[float]) -> Optional[float]:
    """A robust expensive-tail threshold; at least three measured records are required."""
    if len(values) < 3:
        return None
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    return median + 4.4478 * mad if mad else median * 3


def _mark_outliers(records: List[Dict[str, Any]]) -> None:
    selectors = {
        "cost_usd": lambda row: row["cost_usd"]["value"],
        "tokens": lambda row: row["tokens"]["value"],
        "cycle_time_hours": lambda row: row["cycle_time_hours"],
        "review_rounds": lambda row: row["review_rounds"],
        "ci_runs": lambda row: row["ci_runs"],
    }
    for label, getter in selectors.items():
        measured = [float(value) for row in records if (value := getter(row)) is not None]
        threshold = _outlier_threshold(measured)
        if threshold is None:
            continue
        for row in records:
            value = getter(row)
            if value is not None and float(value) > threshold:
                row["outlier_reasons"].append(label)
    for row in records:
        row["outlier"] = bool(row["outlier_reasons"])


def _group_summary(records: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in records:
        groups.setdefault(str(row.get(key) or "unavailable"), []).append(row)
    result: Dict[str, Any] = {}
    for name, rows in sorted(groups.items()):
        cycles = [row["cycle_time_hours"] for row in rows if row["cycle_time_hours"] is not None]
        costs = [row["cost_usd"]["value"] for row in rows if row["cost_usd"]["value"] is not None]
        result[name] = {
            "closed_issues": len(rows),
            "average_cycle_time_hours": round(statistics.mean(cycles), 2) if cycles else None,
            "measured_cost_usd": round(sum(costs), 6) if costs else None,
        }
    return result


def build_closed_issue_metrics(
    issue_events: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    ci_runs: List[Dict[str, Any]],
    usage_records: List[Dict[str, Any]],
    window_days: int,
) -> Dict[str, Any]:
    """Build per-closed-issue records from GitHub lifecycle facts and measured local usage."""
    ci_by_pr: Dict[int, int] = {}
    for run in ci_runs:
        for pr in run.get("pull_requests") or []:
            number = pr.get("number")
            if isinstance(number, int):
                ci_by_pr[number] = ci_by_pr.get(number, 0) + 1

    records: List[Dict[str, Any]] = []
    for issue in (event for event in issue_events if event.get("event_type") == "closed_issue"):
        number = issue["issue_id"]
        linked = [pr for pr in prs if number in (pr.get("issue_numbers") or []) and pr.get("merged")]
        local = [record for record in usage_records if record.get("issue_number") == number]
        agents = {value for value in issue.get("agents") or [] if value}
        agents.update(pr["agent"] for pr in linked if pr.get("agent") not in (None, "unavailable"))
        agents.update(str(item["agent"]) for item in local if item.get("agent"))
        families = {pr["family"] for pr in linked if pr.get("family") not in (None, "unavailable")}
        families.update(str(item["family"]) for item in local if item.get("family"))
        start = parse_iso(issue.get("claim_started_at") or "")
        done = parse_iso(issue.get("done_at") or "")
        cycle = round((done - start).total_seconds() / 3600, 4) if start and done and done >= start else None
        records.append({
            "issue_number": number,
            "title": issue.get("title") or "",
            "closed_at": issue.get("closed_at"),
            "issue_type": issue.get("issue_type") or "unavailable",
            "agent": next(iter(agents)) if len(agents) == 1 else ("multiple" if agents else "unavailable"),
            "family": next(iter(families)) if len(families) == 1 else ("multiple" if families else "unavailable"),
            "cycle_time_hours": cycle,
            "cycle_time_availability": "measured" if cycle is not None else "unavailable",
            "review_rounds": sum(int(pr.get("rework_rounds") or 0) for pr in linked),
            "ci_runs": sum(ci_by_pr.get(int(pr["pr_number"]), 0) for pr in linked),
            "tokens": _token_measurement(local),
            "cost_usd": _measurement(local, "cost_usd", "USD"),
            "human_oversight_minutes": _measurement(local, "human_oversight_minutes", "minutes"),
            "infrastructure_cost_usd": _measurement(local, "infrastructure_cost_usd", "USD"),
            "usage_by_agent": _usage_breakdown(local),
            "outlier": False,
            "outlier_reasons": [],
        })

    records.sort(key=lambda row: row["issue_number"])
    _mark_outliers(records)
    measured_costs = [row["cost_usd"]["value"] for row in records if row["cost_usd"]["value"] is not None]
    return {
        "schema_version": 1,
        "window_days": window_days,
        "closed_issue_count": len(records),
        "cost_per_closed_issue": {
            "availability": "measured" if measured_costs else "unavailable",
            "average_usd_measured": round(statistics.mean(measured_costs), 6) if measured_costs else None,
            "measured_issue_count": len(measured_costs),
            "unavailable_issue_count": len(records) - len(measured_costs),
        },
        "outlier_issue_numbers": [row["issue_number"] for row in records if row["outlier"]],
        "by_agent": _group_summary(records, "agent"),
        "by_family": _group_summary(records, "family"),
        "by_issue_type": _group_summary(records, "issue_type"),
        "issues": records,
    }


def _collect_current_repo_metrics(window_days: int, usage_file: Optional[str]) -> Dict[str, Any]:
    issue_events, prs = fetch_github_telemetry(window_days, include_closed_details=True)
    dwell = calculate_dwell_times(issue_events)
    rework = calculate_rework_rounds(prs)
    closed = build_closed_issue_metrics(issue_events, prs, fetch_ci_runs(window_days), load_local_usage(usage_file), window_days)
    return {"dwell_time": dwell, "rework_yield": rework, "closed_issues": closed}


def collect_factory_metrics(
    window_days: int = 30,
    usage_file: Optional[str] = None,
    repo_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect metrics from ``repo_dir`` while preserving the caller's cwd."""
    if repo_dir is None:
        return _collect_current_repo_metrics(window_days, usage_file)
    original = os.getcwd()
    target = os.path.abspath(repo_dir)
    try:
        os.chdir(target)
        return _collect_current_repo_metrics(window_days, usage_file)
    except OSError as exc:
        raise RuntimeError(f"Could not collect metrics from repository '{target}': {exc}") from exc
    finally:
        os.chdir(original)


def format_closed_issue_report(data: Dict[str, Any]) -> str:
    cost = data["cost_per_closed_issue"]
    cost_text = (
        f"${cost['average_usd_measured']:.6f} across {cost['measured_issue_count']} measured issue(s)"
        if cost["availability"] == "measured" else "unavailable (no measured local CLI cost data)"
    )
    return "\n".join([
        "", "--- Cost & Cycle Time per Closed Issue ---",
        f"  · Window: {data['window_days']} day(s)",
        f"  · Closed issues: {data['closed_issue_count']}",
        f"  · Cost per closed issue: {cost_text}",
        f"  · Issues with unavailable cost: {cost['unavailable_issue_count']}",
        f"  · Outlier issues: {data['outlier_issue_numbers'] or 'none'}",
    ])


def main():
    parser = argparse.ArgumentParser(description="Report factory telemetry: constraint dwell time, rework rounds, and first-pass yield.")
    parser.add_argument("--json", action="store_true", help="Output telemetry metrics in JSON format")
    parser.add_argument("--window-days", type=int, default=30, help="Window size in days for metrics calculation")
    parser.add_argument("--usage-file", help="Optional measured local CLI usage JSON/JSONL; missing data remains unavailable")
    args = parser.parse_args()

    try:
        if args.window_days <= 0:
            raise RuntimeError("--window-days must be positive.")
        result = collect_factory_metrics(args.window_days, args.usage_file)
    except RuntimeError as e:
        sys.stderr.write(f"[ERROR] Telemetry extraction aborted: {e}\n")
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_text_report(result["dwell_time"], result["rework_yield"]))
        print(format_closed_issue_report(result["closed_issues"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
