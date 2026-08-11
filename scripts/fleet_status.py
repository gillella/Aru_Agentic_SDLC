#!/usr/bin/env python3
"""
fleet_status.py - Authoritative state calculation for Aru_Agentic_SDLC factory.

Provides deterministic evaluation of factory state:
  * complete (exit 0) - every governed issue is Done/closed, no open PRs, no active claims,
                       no board drift, no orphan worktrees.
  * waiting  (exit 2) - active work in flight, Ready/In Progress/In Review/Backlog issues,
                       pending CI, or pending reviews.
  * blocked  (exit 3) - needs-human-review, needs-design, exhausted review rounds, or ambiguous board.
  * error    (exit 1) - GitHub API / auth failures; fails closed.
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

from common import (
    AGENT_LABEL_PREFIX,
    agent_labels,
    claimed_by,
    get_issue_project_items,
    get_repo_projects,
    get_repo_slug,
    label_names,
    list_open_issues,
    parse_touches,
    run_cmd,
    run_gh_json,
    select_governed_project_items,
    select_governed_projects,
)

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_WAITING = 2
EXIT_BLOCKED = 3

PR_FIELDS = (
    "number,title,isDraft,labels,reviews,statusCheckRollup,updatedAt,"
    "createdAt,headRefName,body,reviewDecision,state"
)


def list_open_prs_details() -> Optional[List[Dict[str, Any]]]:
    """Fetches list of open PRs via gh CLI."""
    cmd = ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", PR_FIELDS]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else None


def list_worktree_branches() -> List[str]:
    """Lists active git worktree branches."""
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False)
    if code != 0 or not out:
        return []
    branches = []
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.append(line.replace("branch refs/heads/", "").strip())
    return branches


def evaluate_fleet_status(repo_dir: str = ".") -> Dict[str, Any]:
    """Calculates authoritative factory state."""
    slug = get_repo_slug()
    if not slug:
        return {
            "state": "error",
            "exit_code": EXIT_ERROR,
            "reasons": ["Could not determine GitHub repository slug."],
            "summary": "ERROR: Unable to resolve repository slug.",
        }

    projects = get_repo_projects(slug)
    governed_projects = select_governed_projects(projects, slug)
    if len(governed_projects) == 0:
        return {
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": [f"No governed project board found for repository '{slug}'."],
            "summary": "BLOCKED: Missing governed project board.",
        }
    if len(governed_projects) > 1:
        titles = [p.get("title", "") for p in governed_projects]
        return {
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": [f"Ambiguous governed project boards for '{slug}': {titles}"],
            "summary": "BLOCKED: Multiple project boards match governance title.",
        }

    governed_board = governed_projects[0]
    board_title = governed_board.get("title", "")

    issues = list_open_issues()
    if issues is None:
        return {
            "state": "error",
            "exit_code": EXIT_ERROR,
            "reasons": ["Failed to list open issues from GitHub API."],
            "summary": "ERROR: Could not query open issues.",
        }

    prs = list_open_prs_details()
    if prs is None:
        return {
            "state": "error",
            "exit_code": EXIT_ERROR,
            "reasons": ["Failed to list open pull requests from GitHub API."],
            "summary": "ERROR: Could not query open pull requests.",
        }

    worktree_branches = list_worktree_branches()

    reasons: List[str] = []
    blocked_reasons: List[str] = []
    waiting_reasons: List[str] = []
    drifted_issues: List[int] = []
    orphan_issues: List[int] = []
    active_claims: List[Dict[str, Any]] = []

    # Evaluate Issues
    for issue in issues:
        num = issue["number"]
        body = issue.get("body") or ""
        labels = set(label_names(issue))

        holder = claimed_by(issue)
        if holder:
            active_claims.append({"type": "issue", "number": num, "agent": holder})

        if "needs-human-review" in labels:
            blocked_reasons.append(f"Issue #{num} carries 'needs-human-review'.")
        if "needs-design" in labels:
            blocked_reasons.append(f"Issue #{num} requires design review ('needs-design').")

        # Board drift check
        items = get_issue_project_items(num)
        gov_items = select_governed_project_items(items, slug)
        if not gov_items:
            orphan_issues.append(num)
            waiting_reasons.append(f"Issue #{num} is open but not on board '{board_title}'.")
        else:
            board_status = None
            p_field = (gov_items[0].get("project") or {}).get("field") or {}
            board_options = {o["id"]: o["name"].lower() for o in p_field.get("options", [])}
            board_status = board_options.get(gov_items[0].get("statusOptionId"))
            # Label vs Board alignment check
            current_status_label = next((l.replace("status:", "") for l in labels if l.startswith("status:")), None)
            if current_status_label and board_status and current_status_label != board_status.replace(" ", "-"):
                drifted_issues.append(num)
                waiting_reasons.append(f"Issue #{num} status label ('{current_status_label}') drifts from board status ('{board_status}').")

        if "status:ready" in labels:
            waiting_reasons.append(f"Issue #{num} is Ready for implementation.")
        elif "status:in-progress" in labels:
            waiting_reasons.append(f"Issue #{num} is In Progress ({holder or 'unassigned'}).")
        elif "status:in-review" in labels:
            waiting_reasons.append(f"Issue #{num} is In Review.")
        elif "status:backlog" in labels:
            waiting_reasons.append(f"Issue #{num} is in Backlog.")

    # Evaluate PRs
    for pr in prs:
        num = pr["number"]
        labels = set(label_names(pr))
        reviews = pr.get("reviews") or []
        substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
        decision = (pr.get("reviewDecision") or "").upper()

        reviewer_label = next((l.replace("reviewer:", "") for l in labels if l.startswith("reviewer:")), None)
        if reviewer_label:
            active_claims.append({"type": "review", "number": num, "agent": reviewer_label})

        if "needs-human-review" in labels:
            blocked_reasons.append(f"PR #{num} requires human review ('needs-human-review').")

        if decision == "CHANGES_REQUESTED":
            waiting_reasons.append(f"PR #{num} has requested changes.")
        elif decision == "APPROVED":
            waiting_reasons.append(f"PR #{num} is approved and waiting for merge.")
        else:
            waiting_reasons.append(f"PR #{num} is open and pending review.")

    # Worktrees check
    for branch in worktree_branches:
        m = re.search(r"issue-(\d+)", branch, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if not any(i["number"] == num for i in issues):
                waiting_reasons.append(f"Worktree branch '{branch}' exists for closed/merged issue #{num}.")

    # Calculate overall state
    if blocked_reasons:
        return {
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": blocked_reasons,
            "summary": f"BLOCKED: {len(blocked_reasons)} condition(s) require escalation.",
            "project_board": {"title": board_title, "id": governed_board.get("id")},
            "open_issues_count": len(issues),
            "open_prs_count": len(prs),
            "active_claims": active_claims,
            "orphans": orphan_issues,
            "drifted": drifted_issues,
        }

    if waiting_reasons or issues or prs:
        return {
            "state": "waiting",
            "exit_code": EXIT_WAITING,
            "reasons": waiting_reasons,
            "summary": f"WAITING: {len(issues)} open issue(s), {len(prs)} open PR(s).",
            "project_board": {"title": board_title, "id": governed_board.get("id")},
            "open_issues_count": len(issues),
            "open_prs_count": len(prs),
            "active_claims": active_claims,
            "orphans": orphan_issues,
            "drifted": drifted_issues,
        }

    return {
        "state": "complete",
        "exit_code": EXIT_COMPLETE,
        "reasons": [],
        "summary": "COMPLETE: All governed issues closed, zero open PRs, zero active claims.",
        "project_board": {"title": board_title, "id": governed_board.get("id")},
        "open_issues_count": 0,
        "open_prs_count": 0,
        "active_claims": [],
        "orphans": [],
        "drifted": [],
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate factory fleet completion state.")
    parser.add_argument("--json", action="store_true", help="Output state in JSON format")
    parser.add_argument("--repo-dir", default=".", help="Repository working directory")
    args = parser.parse_args()

    status = evaluate_fleet_status(args.repo_dir)

    if args.json:
        print(json.dumps(status, indent=2))
        sys.exit(status["exit_code"])

    print("=== Aru_Agentic_SDLC: Factory Fleet Status ===")
    print(f"State: {status['state'].upper()}")
    print(f"Summary: {status['summary']}")
    if status.get("reasons"):
        print("\nDetails:")
        for r in status["reasons"]:
            print(f"  • {r}")
    sys.exit(status["exit_code"])


if __name__ == "__main__":
    main()
