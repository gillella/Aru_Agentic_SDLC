#!/usr/bin/env python3
"""
create_branch.py - Formats standard branch names and creates a new git feature branch or worktree.
Standard naming format: <type>/issue-<ID>-<short-description>
"""

import argparse
import json
import re
import sys
from typing import Any, Optional

from common import (
    create_worktree,
    fetch_issue_comments,
    get_issue,
    run_cmd,
    terminal_lease_sha,
)

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


def _get_section_content(text: str, heading_pattern: str) -> Optional[str]:
    """Extracts the body text under a specific markdown heading up to the next heading."""
    pattern = rf"(?:^|\n)#{{1,4}}\s*(?:{heading_pattern})[^\n]*\n(.*?)(?=\n#{{1,4}}\s|\Z)"
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


def _is_placeholder_content(body: Optional[str]) -> bool:
    """Returns True if the section body is missing, empty, or only contains placeholder tokens."""
    if not body:
        return True
    cleaned = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    cleaned = re.sub(r"[`*_#>-]", "", cleaned)
    cleaned = re.sub(r"\b(todo|tbd|none|na|n/a|placeholder|tba|\.{2,})\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", "", cleaned)
    return len(cleaned) < 5


def validate_plan_depth(text: str, is_risk: bool = False) -> list[str]:  # noqa: C901, PLR0912
    """Validates plan text and returns a list of missing or placeholder-only required sections."""
    if not text:
        return ["no plan content provided"]
    if not re.search(r"^\s*#{1,4}\s*implementation\s+plan\b", text, re.IGNORECASE | re.MULTILINE):
        return ["missing heading: ## Implementation Plan"]

    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    if len(cleaned) < 40:
        return ["plan is too brief (minimum substantive content required)"]

    missing = []

    approach_body = _get_section_content(cleaned, r"approach|proposed changes|design|architecture|solution")
    if approach_body is None:
        missing.append("missing section: Approach / Proposed Changes")
    elif _is_placeholder_content(approach_body):
        missing.append("section 'Approach / Proposed Changes' contains only placeholder content")

    files_body = _get_section_content(cleaned, r"files|files to touch|affected files|touched files|scope")
    if files_body is None:
        # Fallback to inline touches: check
        inline_match = re.search(r"\b(?:touches|files to touch|affected files)\s*:\s*([^\n]+)", cleaned, re.IGNORECASE)
        if not inline_match:
            missing.append("missing section: Files to Touch / Scope")
        elif _is_placeholder_content(inline_match.group(1)):
            missing.append("section 'Files to Touch / Scope' contains only placeholder content")
    elif _is_placeholder_content(files_body):
        missing.append("section 'Files to Touch / Scope' contains only placeholder content")

    verification_body = _get_section_content(cleaned, r"verification|testing|test strategy|test plan|verification & test strategy")
    if verification_body is None:
        missing.append("missing section: Verification / Test Strategy")
    elif _is_placeholder_content(verification_body):
        missing.append("section 'Verification / Test Strategy' contains only placeholder content")

    if is_risk:
        schema_body = _get_section_content(cleaned, r"schema|schema\s*/\s*api deltas?|api deltas?|data migration|security impact|invariants?")
        if schema_body is None:
            missing.append("missing section (high-risk): Schema / API Deltas or Security Impact")
        elif _is_placeholder_content(schema_body):
            missing.append("section 'Schema / API Deltas' contains only placeholder content")

        alternatives_body = _get_section_content(cleaned, r"rejected alternatives|alternatives considered|alternatives|trade-?offs?")
        if alternatives_body is None:
            missing.append("missing section (high-risk): Rejected Alternatives")
        elif _is_placeholder_content(alternatives_body):
            missing.append("section 'Rejected Alternatives' contains only placeholder content")

    return missing


def is_substantive_plan(text: str, is_risk: bool = False) -> bool:
    """Validates that text contains a substantive implementation plan artifact."""
    return len(validate_plan_depth(text, is_risk=is_risk)) == 0


def format_plan_template(is_risk: bool = False) -> str:
    """Returns the recommended template for an implementation plan comment."""
    if is_risk:
        return (
            "## Implementation Plan\n\n"
            "### Approach\n"
            "<!-- Describe the design, architecture, and step-by-step changes -->\n"
            "...\n\n"
            "### Files to Touch\n"
            "<!-- List exact paths to be created, modified, or deleted -->\n"
            "...\n\n"
            "### Schema / API Deltas\n"
            "<!-- Describe database/API changes, schema migrations, and security boundaries -->\n"
            "...\n\n"
            "### Verification & Test Strategy\n"
            "<!-- Executable test commands and verification plan -->\n"
            "...\n\n"
            "### Rejected Alternatives\n"
            "<!-- Alternatives considered and rationale for choices -->\n"
            "..."
        )
    return (
        "## Implementation Plan\n\n"
        "### Approach\n"
        "<!-- Describe the design, architecture, and step-by-step changes -->\n"
        "...\n\n"
        "### Files to Touch\n"
        "<!-- List exact paths to be created, modified, or deleted -->\n"
        "...\n\n"
        "### Verification & Test Strategy\n"
        "<!-- Executable test commands and verification plan -->\n"
        "..."
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


def _merged_branch_refusal(branch_name: str) -> Optional[str]:
    """Refusal text when `branch_name` was already spent by a merged PR.

    Branch names are deterministic from the issue id and title, so a stale
    writer recreating a branch a governed merge already deleted, or a
    would-be re-run of `create_branch.py` for an issue whose PR already
    merged, must never silently switch onto (or recreate) that old ref
    (#344, modeled on gillella/hermes-trading-automation PR #89). A merged
    PR for this exact head branch means the branch is spent, whether or not
    it also carries the newer terminal-lease label (older merges predate it).
    """
    code, out, _err = run_cmd(
        ["gh", "pr", "list", "--head", branch_name, "--state", "all",
         "--json", "number,state,labels"],
        check=False,
    )
    if code != 0:
        # Best-effort: an unreadable PR list must not block every branch
        # creation on a transient gh/API failure.
        return None
    try:
        candidates = json.loads(out) if out else []
    except json.JSONDecodeError:
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        label_names = [
            (lab.get("name") or "") for lab in candidate.get("labels") or []
            if isinstance(lab, dict)
        ]
        lease_sha = terminal_lease_sha(label_names)
        if lease_sha or (candidate.get("state") or "").upper() == "MERGED":
            number = candidate.get("number")
            detail = f"terminal lease {lease_sha}" if lease_sha else "state MERGED"
            return (
                f"Branch '{branch_name}' was already used by merged PR #{number} "
                f"({detail}); it cannot be recreated or reused. Open a new "
                "governed issue so a fresh, issue-specific branch is derived."
            )
    return None


def create_branch(issue_id: int, branch_type: str = "feat", use_worktree: bool = False,
                  fetch_remote: bool = True, agent: str = "") -> str:
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
                f"  gh issue comment {issue_id} --body \"$(cat <<'EOF'\n{template_body}\nEOF\n)\""
            )
            print(msg, file=sys.stderr)
            sys.exit(1)

    title_slug = "work"
    if issue and "title" in issue:
        clean_title = re.sub(r"^(feat|fix|chore|docs)\s*:\s*", "", issue["title"], flags=re.I)
        title_slug = sanitize_slug(clean_title)

    branch_name = f"{branch_type}/issue-{issue_id}-{title_slug}"

    refusal = _merged_branch_refusal(branch_name)
    if refusal:
        print(f"[BLOCKED] {refusal}", file=sys.stderr)
        sys.exit(1)

    if use_worktree:
        path = create_worktree(branch_name, agent=agent)
        if path is None:
            # A refused worktree means another agent holds this branch's
            # checkout. Continuing would put work in somebody else's directory,
            # which is exactly how one agent's commit acquired another's
            # uncommitted files (#305).
            sys.exit(1)
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
    parser.add_argument("--agent", type=str, default="",
                        help="Agent id owning this worktree. Scopes the worktree "
                             "path so two agents sharing one clone never land in "
                             "the same directory.")
    args = parser.parse_args()

    create_branch(args.issue, args.type, args.worktree, agent=args.agent)


if __name__ == "__main__":
    main()
