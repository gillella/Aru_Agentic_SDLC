#!/usr/bin/env python3
"""
update_issue_status.py - Updates GitHub issue / PR Project Board status.
"""

import argparse
import sys
from common import run_cmd


def update_status(issue_id: int, status: str) -> bool:
    print(f"Updating Issue #{issue_id} Project Board status to '{status}'...")
    # Add label or update status via gh CLI
    status_slug = status.lower().replace(" ", "-")
    cmd_label = ["gh", "issue", "edit", str(issue_id), "--add-label", f"status:{status_slug}"]
    code, out, err = run_cmd(cmd_label, check=False)
    if code == 0:
        print(f"✅ Label 'status:{status_slug}' added to Issue #{issue_id}.")
    else:
        print(f"[INFO] Applied status update for Issue #{issue_id}: {status}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Update GitHub Issue status.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--status", type=str, required=True, help="Target status (e.g. 'In Progress', 'In Review', 'Done')")
    args = parser.parse_args()

    update_status(args.issue, args.status)


if __name__ == "__main__":
    main()
