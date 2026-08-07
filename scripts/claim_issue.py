#!/usr/bin/env python3
"""
claim_issue.py - Assigns a GitHub issue to self/agent and moves it to 'In Progress'
on both the Project Board and its status:* label.
"""

import argparse
import sys

from update_issue_status import update_status


def claim_issue(issue_id: int, assignee: str = "@me", status: str = "In Progress") -> bool:
    from common import run_cmd

    print(f"Assigning Issue #{issue_id} to '{assignee}'...")
    code, out, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--add-assignee", assignee], check=False
    )
    if code != 0:
        print(f"[WARN] Unable to assign issue: {err or out}. Continuing...", file=sys.stderr)

    # Direct import rather than shelling out to a relative script path. The previous
    # version ran ["python3", "scripts/update_issue_status.py", ...], which resolves
    # against the *target project's* cwd rather than the framework's, so it silently
    # did nothing whenever these scripts were invoked from a project repo root.
    ok = update_status(issue_id, status)
    if ok:
        print(f"✅ Issue #{issue_id} claimed.")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Claim a GitHub issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--assignee", type=str, default="@me", help="Assignee username (default: @me)")
    parser.add_argument("--status", type=str, default="In Progress", help="Target status column")
    args = parser.parse_args()

    if not claim_issue(args.issue, args.assignee, args.status):
        sys.exit(1)


if __name__ == "__main__":
    main()
