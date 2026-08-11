#!/usr/bin/env python3
"""
create_branch.py - Formats standard branch names and creates a new git feature branch or worktree.
Standard naming format: <type>/issue-<ID>-<short-description>
"""

import argparse
import re
import sys
from common import create_worktree, get_issue, run_cmd


def sanitize_slug(text: str) -> str:
    """Sanitizes text to safe git branch slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:30].strip("-")


def create_branch(issue_id: int, branch_type: str = "feat", use_worktree: bool = False) -> str:
    issue = get_issue(issue_id)
    title_slug = "work"
    if issue and "title" in issue:
        clean_title = re.sub(r"^(feat|fix|chore|docs)\s*:\s*", "", issue["title"], flags=re.I)
        title_slug = sanitize_slug(clean_title)

    branch_name = f"{branch_type}/issue-{issue_id}-{title_slug}"

    if use_worktree:
        path = create_worktree(branch_name)
        print(f"✅ Isolated worktree ready at: {path}")
        return path
    else:
        print(f"Creating & checking out git branch: '{branch_name}'...")
        code, out, err = run_cmd(["git", "checkout", "-b", branch_name], check=False)
        if code != 0:
            print(f"[INFO] Branch '{branch_name}' may already exist. Switching to it...", file=sys.stderr)
            run_cmd(["git", "checkout", branch_name])
        print(f"✅ Checked out branch: '{branch_name}'")
        return branch_name


def main():
    parser = argparse.ArgumentParser(description="Create standardized git branch or worktree for an issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--type", type=str, default="feat", choices=["feat", "fix", "chore", "docs"], help="Branch type prefix")
    parser.add_argument("--worktree", action="store_true", help="Create isolated git worktree directory")
    args = parser.parse_args()

    create_branch(args.issue, args.type, args.worktree)


if __name__ == "__main__":
    main()
