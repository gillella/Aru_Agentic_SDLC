#!/usr/bin/env python3
"""
fetch_next_issue.py - Evaluates open issues, parses dependency chains, and identifies
the next actionable issue in progressive order. Also detects independent issues suitable for parallel execution.
"""

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional
from common import get_current_branch, list_open_issues, run_gh_json


def parse_dependencies(body: str) -> List[int]:
    """Parses 'depends-on: #12, #14' pattern from issue body."""
    if not body:
        return []
    match = re.search(r"depends-on\s*:\s*([^\n]+)", body, re.IGNORECASE)
    if not match:
        return []
    raw = match.group(1)
    deps = re.findall(r"#(\d+)", raw)
    return [int(d) for d in deps]


def is_parallel_eligible(body: str, labels: List[Dict[str, Any]]) -> bool:
    """Checks if issue is flagged as parallel-eligible."""
    label_names = [l.get("name", "").lower() for l in labels]
    if "parallel-eligible" in label_names or "independent" in label_names:
        return True
    if body and "parallel-eligible: true" in body.lower():
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Fetch next actionable issue in progressive order.")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")
    args = parser.parse_args()

    current_branch = get_current_branch()
    open_issues = list_open_issues()

    # Session resume check: check if current branch matches issue-XX
    in_flight_issue = None
    match = re.search(r"issue-(\d+)", current_branch, re.IGNORECASE)
    if match:
        in_flight_issue = int(match.group(1))

    unblocked_issues = []
    parallel_issues = []

    open_issue_numbers = {i["number"] for i in open_issues}

    for issue in open_issues:
        num = issue["number"]
        body = issue.get("body", "") or ""
        labels = issue.get("labels", [])
        deps = parse_dependencies(body)

        # Check if any dependency is still open (unresolved)
        unresolved_deps = [d for d in deps if d in open_issue_numbers]

        if not unresolved_deps:
            unblocked_issues.append(issue)
            if is_parallel_eligible(body, labels):
                parallel_issues.append(issue)

    # Determine next issue in progressive order (lowest issue ID unblocked)
    unblocked_issues.sort(key=lambda x: x["number"])
    next_issue = unblocked_issues[0] if unblocked_issues else None

    res = {
        "session_branch": current_branch,
        "resumable_in_flight_issue": in_flight_issue,
        "next_progressive_issue": next_issue["number"] if next_issue else None,
        "next_issue_title": next_issue["title"] if next_issue else None,
        "total_open_issues": len(open_issues),
        "total_unblocked_issues": len(unblocked_issues),
        "parallel_eligible_issues": [i["number"] for i in parallel_issues],
    }

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("=== Aru_Agentic_SDLC: Issue Dependency Evaluation ===")
        if in_flight_issue:
            print(f"🔄 Active Session Branch: '{current_branch}' detected for Issue #{in_flight_issue}.")
        if next_issue:
            print(f"🎯 Next Actionable Issue (Progressive Order): #{next_issue['number']} - {next_issue['title']}")
        else:
            print("✨ No unblocked issues available to claim.")
        if parallel_issues:
            print(f"⚡ Independent Issues Eligible for Parallel Agents: {[i['number'] for i in parallel_issues]}")


if __name__ == "__main__":
    main()
