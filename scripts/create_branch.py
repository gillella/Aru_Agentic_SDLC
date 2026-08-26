#!/usr/bin/env python3
"""
create_branch.py - Formats standard branch names and creates a new git feature branch or worktree.
Standard naming format: <type>/issue-<ID>-<short-description>
"""

import argparse
import re
import sys
from typing import Any, Optional

from common import (
    agent_labels,
    create_worktree,
    fetch_issue_comments,
    get_agent_id,
    get_issue,
    get_repo_slug,
    label_names,
    query_issue_project_items,
    run_cmd,
    select_governed_project_items,
    terminal_lease_refusal,
    terminal_merge_lease,
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


def worktree_admission_gaps(
    issue: Optional[dict[str, Any]],
    agent: str,
    project_items: Optional[list[dict[str, Any]]],
    repo_slug: Optional[str],
) -> list[str]:
    """Returns every fail-closed claim and board admission gap."""
    gaps = []
    agent = agent.strip()
    if not agent:
        gaps.append("an explicit --agent or agent environment identity is required")

    if not issue:
        gaps.append("the live issue is missing or unreadable")
    else:
        if str(issue.get("state", "")).upper() != "OPEN":
            gaps.append("the issue is not verifiably open")

        expected_claim = f"agent:{agent}" if agent else ""
        claims = agent_labels(issue)
        if not expected_claim or claims != [expected_claim]:
            rendered = ", ".join(claims) or "none"
            gaps.append(
                "exactly one matching settled claim is required "
                f"(expected {expected_claim or 'agent:<id>'}; found {rendered})"
            )

        statuses = sorted({
            name.lower()
            for name in label_names(issue)
            if name.lower().startswith("status:")
        })
        if statuses != ["status:in-progress"]:
            rendered = ", ".join(statuses) or "none"
            gaps.append(
                "the canonical status label must be exactly "
                f"status:in-progress (found {rendered})"
            )

    if not repo_slug or "/" not in repo_slug:
        gaps.append("the repository identity is missing or unreadable")
    if project_items is None:
        gaps.append("the issue Project Board state is unreadable")
    elif repo_slug and "/" in repo_slug:
        governed = select_governed_project_items(project_items, repo_slug)
        if len(governed) != 1:
            gaps.append("exactly one governed Project Board item is required")
        else:
            board_status = str(
                (governed[0].get("status") or {}).get("name", "")
            ).strip()
            if board_status.lower() != "in progress":
                gaps.append(
                    "the governed Project Board status must be In Progress "
                    f"(found {board_status or 'none'})"
                )
    return gaps


def require_worktree_admission(issue_id: int, agent: str) -> dict[str, Any]:
    """Resolve and verify live claim/board authority before any Git mutation."""
    issue = get_issue(issue_id)
    repo_slug = get_repo_slug()
    project_items = query_issue_project_items(issue_id) if repo_slug else None
    gaps = worktree_admission_gaps(issue, agent, project_items, repo_slug)
    if gaps:
        print(
            f"[BLOCKED] Worktree admission failed for issue #{issue_id}:",
            file=sys.stderr,
        )
        for gap in gaps:
            print(f"  - {gap}", file=sys.stderr)
        raise SystemExit(1)
    assert issue is not None
    return issue


def branch_name_for(issue: Optional[dict[str, Any]], issue_id: int, branch_type: str) -> str:
    """Derives the standard branch name from a live, admission-verified issue."""
    title_slug = "work"
    if issue and "title" in issue:
        clean_title = re.sub(r"^(feat|fix|chore|docs)\s*:\s*", "", issue["title"], flags=re.I)
        title_slug = sanitize_slug(clean_title)
    return f"{branch_type}/issue-{issue_id}-{title_slug}"


def create_branch(issue_id: int, branch_type: str = "feat", use_worktree: bool = False,
                  agent: str = "") -> str:
    issue = require_worktree_admission(issue_id, agent)

    if requires_plan(issue, branch_type):
        is_risk = is_high_risk(issue)
        if not has_implementation_plan(issue_id, issue=issue, is_risk=is_risk):
            comments = fetch_issue_comments(issue_id)
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

    branch_name = branch_name_for(issue, issue_id, branch_type)

    # A branch name that already carried a governed merge is spent. Silently
    # checking it out again is how a stale worker rebuilt merged work into an
    # ungoverned orphan commit (#344); refuse instead of reusing.
    lease = terminal_merge_lease(branch_name)
    if lease:
        print(f"[BLOCKED] {terminal_lease_refusal(lease, 'reuse this branch')}", file=sys.stderr)
        sys.exit(1)

    # Admission is a live predicate, not a one-time snapshot, and both the plan
    # gate and the lease lookup above are remote reads that another agent can
    # release the claim or move the board during. Re-verify last, so the final
    # remote read this invocation makes is the authority read itself and only
    # local comparison separates it from the Git write below. GitHub exposes no
    # compare-and-swap on a claim label, so the residual window cannot be closed
    # outright; it is narrowed to local git work, and every divergence -- an
    # unreadable issue, a released claim, a moved board item, or a branch name
    # that no longer matches the one the lease was checked for -- fails closed.
    issue = require_worktree_admission(issue_id, agent)
    if branch_name_for(issue, issue_id, branch_type) != branch_name:
        print(
            f"[BLOCKED] Issue #{issue_id} changed during admission: the merge "
            f"lease was checked for '{branch_name}', which is no longer its "
            "branch name. Re-run once the issue settles.",
            file=sys.stderr,
        )
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
    parser.add_argument("--agent", type=str, default=get_agent_id() or "",
                        help="Claimed agent id owning this worktree. Defaults to an "
                             "explicit agent environment identity; admission fails "
                             "if neither is set.")
    args = parser.parse_args()

    create_branch(args.issue, args.type, args.worktree, agent=args.agent)


if __name__ == "__main__":
    main()
