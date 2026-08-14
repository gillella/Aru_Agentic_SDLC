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


def is_high_risk(issue: Optional[dict[str, Any]]) -> bool:
    """Returns True if the issue touches high-risk domains (money, PII, schema, migration, security, tenancy)."""
    if not issue:
        return False
    labels = {lbl.get("name", "").lower() for lbl in (issue.get("labels") or [])}
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    text_to_check = f"{title} {body} {' '.join(labels)}"
    for term in HIGH_RISK_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", text_to_check, re.IGNORECASE):
            return True
    return False


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

    return is_high_risk(issue)


def validate_plan_depth(text: str, is_risk: bool = False) -> list[str]:
    """Validates plan text and returns a list of missing required sections/elements."""
    if not text:
        return ["no plan content provided"]
    if not re.search(r"^\s*#{1,4}\s*implementation\s+plan\b", text, re.IGNORECASE | re.MULTILINE):
        return ["missing heading: ## Implementation Plan"]

    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    if len(cleaned) < 40:
        return ["plan is too brief (minimum substantive content required)"]

    missing = []
    has_approach = bool(re.search(
        r"(?:#{1,4}\s*(?:approach|proposed changes|design|architecture|solution)|\b(?:approach|proposed changes|architecture)\b)",
        cleaned,
        re.IGNORECASE,
    ))
    if not has_approach:
        missing.append("missing section: Approach / Proposed Changes")

    has_files = bool(re.search(
        r"(?:#{1,4}\s*(?:files|files to touch|affected files|touched files|scope)|\b(?:touches|files to touch|affected files)\s*:\b)",
        cleaned,
        re.IGNORECASE,
    ))
    if not has_files:
        missing.append("missing section: Files to Touch / Scope")

    has_verification = bool(re.search(
        r"(?:#{1,4}\s*(?:verification|testing|test strategy|test plan)|\b(?:verification|test strategy|tests)\b)",
        cleaned,
        re.IGNORECASE,
    ))
    if not has_verification:
        missing.append("missing section: Verification / Test Strategy")

    if is_risk:
        has_schema_or_api = bool(re.search(
            r"(?:#{1,4}\s*(?:schema|api|data|migration|deltas?|invariants?|security impact)|\b(?:schema deltas?|api deltas?|data migration|security impact)\b)",
            cleaned,
            re.IGNORECASE,
        ))
        if not has_schema_or_api:
            missing.append("missing section (high-risk): Schema / API Deltas or Security Impact")

        has_alternatives = bool(re.search(
            r"(?:#{1,4}\s*(?:rejected alternatives|alternatives|trade-?offs?)|\b(?:rejected alternatives|alternatives considered)\b)",
            cleaned,
            re.IGNORECASE,
        ))
        if not has_alternatives:
            missing.append("missing section (high-risk): Rejected Alternatives")

    return missing


def is_substantive_plan(text: str, is_risk: bool = False) -> bool:
    """Validates that text contains a substantive implementation plan artifact."""
    return len(validate_plan_depth(text, is_risk=is_risk)) == 0


def format_plan_template(is_risk: bool = False) -> str:
    """Returns the recommended template for an implementation plan comment."""
    if is_risk:
        return (
            "## Implementation Plan\\n\\n"
            "### Approach\\n...\\n\\n"
            "### Files to Touch\\n...\\n\\n"
            "### Schema / API Deltas\\n...\\n\\n"
            "### Verification & Test Strategy\\n...\\n\\n"
            "### Rejected Alternatives\\n..."
        )
    return (
        "## Implementation Plan\\n\\n"
        "### Approach\\n...\\n\\n"
        "### Files to Touch\\n...\\n\\n"
        "### Verification & Test Strategy\\n..."
    )


def has_implementation_plan(
    issue_id: int,
    issue: Optional[dict[str, Any]] = None,
    comments: Optional[list[dict[str, Any]]] = None,
    is_risk: Optional[bool] = None,
) -> bool:
    """Checks if a substantive implementation plan exists in the issue comments or body."""
    if is_risk is None:
        is_risk = is_high_risk(issue)
    if comments is None:
        comments = fetch_issue_comments(issue_id)

    # Prefer dedicated plan comments
    for comment in comments:
        body = comment.get("body") or ""
        if is_substantive_plan(body, is_risk=is_risk):
            return True

    if issue and issue.get("body"):
        if is_substantive_plan(issue["body"], is_risk=is_risk):
            return True

    return False


def create_branch(issue_id: int, branch_type: str = "feat", use_worktree: bool = False, fetch_remote: bool = True) -> str:
    issue = get_issue(issue_id) if fetch_remote else None

    if requires_plan(issue, branch_type):
        is_risk = is_high_risk(issue)
        if not has_implementation_plan(issue_id, issue=issue, is_risk=is_risk):
            comments = fetch_issue_comments(issue_id) if fetch_remote else []
            candidate_texts = [c.get("body", "") for c in comments] + ([issue.get("body", "")] if issue else [])
            plan_attempts = [t for t in candidate_texts if re.search(r"^\s*#{1,4}\s*implementation\s+plan\b", t, re.I | re.M)]
            if plan_attempts:
                gaps = validate_plan_depth(plan_attempts[0], is_risk=is_risk)
                gaps_str = "\n".join(f"  - {g}" for g in gaps)
                missing_detail = f"Existing plan attempt on issue #{issue_id} is incomplete:\n{gaps_str}"
            else:
                missing_detail = (
                    f"Missing: An implementation plan comment on issue #{issue_id} detailing Approach, "
                    f"Files to Touch, Test Strategy"
                    + (", Schema/API Deltas, and Rejected Alternatives (required for high-risk work)." if is_risk else ".")
                )

            template_body = format_plan_template(is_risk=is_risk)
            msg = (
                f"[BLOCKED] Plan gate: Cannot create branch/worktree for issue #{issue_id} without a durable implementation plan.\n"
                f"{missing_detail}\n\n"
                f"To fix, post an implementation plan comment to issue #{issue_id}:\n"
                f'  gh issue comment {issue_id} --body "{template_body}"'
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
