#!/usr/bin/env python3
"""
create_branch.py - Formats standard branch names and creates a new git feature branch or worktree.
Standard naming format: <type>/issue-<ID>-<short-description>
"""

import argparse
import re
import sys
from typing import Any, Optional

from common import create_worktree, fetch_issue_comments, get_issue, run_cmd

HIGH_RISK_TERMS = {
    "money",
    "pii",
    "schema",
    "schemas",
    "migration",
    "migrations",
    "tenancy",
    "tenant",
    "security",
    "irreversible",
}


def sanitize_slug(text: str) -> str:
    """Sanitizes text to safe git branch slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:30].strip("-")


def requires_plan(issue: Optional[dict[str, Any]], branch_type: str = "feat") -> bool:
    """Returns True if the issue or branch type requires an implementation plan before branch creation."""
    if branch_type == "feat":
        return True
    if not issue:
        return False

    labels = {lbl.get("name", "").lower() for lbl in (issue.get("labels") or [])}
    if "type:feat" in labels or "needs-design" in labels or "feature" in labels:
        return True

    title = (issue.get("title") or "").lower()
    if title.startswith("feat:") or title.startswith("feat/"):
        return True

    body = (issue.get("body") or "").lower()
    text_to_check = f"{title} {body} {' '.join(labels)}"
    for term in HIGH_RISK_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", text_to_check, re.IGNORECASE):
            return True

    return False


def is_substantive_plan(text: str) -> bool:
    """Validates that text contains a substantive implementation plan artifact."""
    if not text:
        return False
    # Must have an explicit Implementation Plan heading
    if not re.search(r"^\s*#{1,4}\s*implementation\s+plan\b", text, re.IGNORECASE | re.MULTILINE):
        return False
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    if len(cleaned) < 40:
        return False
    # Reject placeholder-only statements
    if re.search(r"^\s*#{1,4}\s*implementation\s+plan\s*:\s*(?:tbd|todo|none|n/a|required|wip)\s*$", cleaned, re.IGNORECASE | re.MULTILINE):
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if len(lines) < 4:
            return False

    has_approach_or_changes = bool(re.search(
        r"(?:#{1,4}\s*(?:approach|proposed changes|design|architecture|files|scope|goal|plan)|\b(?:approach|proposed changes|files to change|architecture)\b)",
        cleaned,
        re.IGNORECASE,
    ))
    has_verification_or_tests = bool(re.search(
        r"(?:#{1,4}\s*(?:verification|testing|test strategy|test plan)|\b(?:verification|test strategy|tests)\b)",
        cleaned,
        re.IGNORECASE,
    ))

    return has_approach_or_changes and has_verification_or_tests


def has_implementation_plan(
    issue_id: int,
    issue: Optional[dict[str, Any]] = None,
    comments: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Checks if a substantive implementation plan exists in the issue comments or body."""
    if comments is None:
        comments = fetch_issue_comments(issue_id)

    # Prefer dedicated plan comments
    for comment in comments:
        body = comment.get("body") or ""
        if is_substantive_plan(body):
            return True

    if issue and issue.get("body"):
        if is_substantive_plan(issue["body"]):
            return True

    return False


def create_branch(issue_id: int, branch_type: str = "feat", use_worktree: bool = False, fetch_remote: bool = True) -> str:
    issue = get_issue(issue_id) if fetch_remote else None

    if requires_plan(issue, branch_type):
        if not has_implementation_plan(issue_id, issue=issue):
            msg = (
                f"[BLOCKED] Plan gate: Cannot create branch/worktree for issue #{issue_id} without a durable implementation plan.\n"
                f"Missing: An implementation plan comment on issue #{issue_id} detailing approach, touched files, test strategy, and rejected alternatives.\n\n"
                f"To fix, post an implementation plan comment to issue #{issue_id}:\n"
                f'  gh issue comment {issue_id} --body "## Implementation Plan\\n\\n### Goal\\n...\\n\\n### Proposed Changes\\n...\\n\\n### Verification\\n..."'
            )
            print(msg, file=sys.stderr)
            sys.exit(1)

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
