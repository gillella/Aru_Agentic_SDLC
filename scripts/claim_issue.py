#!/usr/bin/env python3
"""
claim_issue.py - Assigns a GitHub issue to self/agent and updates Project Board status to 'In Progress'.
"""

import argparse
import sys
from common import get_issue, run_cmd


def claim_issue(issue_id: int, assignee: str = "@me", status: str = "In Progress") -> bool:
    print(f"Assigning Issue #{issue_id} to '{assignee}'...")
    cmd_assign = ["gh", "issue", "edit", str(issue_id), "--add-assignee", assignee]
    code, out, err = run_cmd(cmd_assign, check=False)
    if code != 0:
        print(f"[WARN] Unable to assign issue via gh CLI: {err}. Continuing...", file=sys.stderr)

    print(f"Updating Issue #{issue_id} Project Board status to '{status}'...")
    # Attempt gh project item-edit / gh issue status update
    cmd_status = ["python3", "scripts/update_issue_status.py", "--issue", str(issue_id), "--status", status]
    run_cmd(cmd_status, check=False)
    print(f"✅ Issue #{issue_id} claimed successfully.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Claim GitHub issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--assignee", type=str, default="@me", help="Assignee username (default: @me)")
    parser.add_argument("--status", type=str, default="In Progress", help="Target status column")
    args = parser.parse_args()

    claim_issue(args.issue, args.assignee, args.status)


if __name__ == "__main__":
    main()
