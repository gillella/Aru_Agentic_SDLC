#!/usr/bin/env python3
"""
create_pr.py - Opens a Pull Request pre-populated with issue linking ('Closes #X').
"""

import argparse
import sys
from common import get_current_branch, get_issue, run_cmd


def create_pr(issue_id: int, title: str = "", body: str = "") -> bool:
    current_branch = get_current_branch()
    issue = get_issue(issue_id)

    if not title:
        title = issue["title"] if issue else f"Fix issue #{issue_id}"

    closure_footer = f"\n\nCloses #{issue_id}"
    full_body = (body.strip() + closure_footer) if body else f"Implementation for issue #{issue_id}.{closure_footer}"

    print(f"Opening Pull Request for branch '{current_branch}' linking 'Closes #{issue_id}'...")
    cmd = ["gh", "pr", "create", "--title", title, "--body", full_body, "--head", current_branch]

    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to open PR: {err}", file=sys.stderr)
        return False

    print(f"✅ Pull Request created successfully:\n{out}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Create Pull Request linking an issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--title", type=str, default="", help="Pull Request Title")
    parser.add_argument("--body", type=str, default="", help="Pull Request Description Body")
    args = parser.parse_args()

    create_pr(args.issue, args.title, args.body)


if __name__ == "__main__":
    main()
