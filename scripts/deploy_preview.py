#!/usr/bin/env python3
"""deploy_preview.py — Governed preview deployment helper.

Dispatches configured CD workflow for a specific merged commit, waits for completion,
records the preview URL on the originating issue, or files an attached remediation issue
on the Project Board if deployment fails.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Set

from common import get_repo_slug, run_cmd


def get_default_branch() -> str:
    """Resolves default branch from git remote or falls back to main."""
    code, out, _ = run_cmd(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
    if code == 0 and out.strip():
        return out.strip().split("/")[-1]
    return "main"


def get_originating_issue(commit_sha: str) -> Optional[int]:
    """Inspects commit message to find linked Closes #<ID> issue."""
    code, out, _ = run_cmd(["git", "log", "-1", "--format=%B", commit_sha], check=False)
    if code != 0 or not out:
        return None
    match = re.search(r"\b(?:closes|fixes|resolves)\s*#(\d+)\b", out, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def verify_commit_merged(commit_sha: str, default_branch: Optional[str] = None) -> tuple[bool, str]:
    """Verifies that commit_sha exists and is merged into the default branch.

    Returns (is_valid, resolved_full_sha).
    """
    if not commit_sha or commit_sha.startswith("-"):
        return False, ""

    # Validate commit exists
    code, out, _ = run_cmd(["git", "rev-parse", "--verify", f"{commit_sha}^{{commit}}"], check=False)
    if code != 0 or not out.strip():
        return False, ""
    full_sha = out.strip()

    if not default_branch:
        default_branch = get_default_branch() or "main"

    # Check ancestry against default branch (local or remote tracking)
    target_ref = default_branch
    code, _, _ = run_cmd(["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"], check=False)
    if code != 0:
        target_ref = f"origin/{default_branch}"
        code, _, _ = run_cmd(["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"], check=False)
        if code != 0:
            # Cannot resolve default branch
            return False, full_sha

    code, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", full_sha, target_ref], check=False)
    if code != 0:
        return False, full_sha

    return True, full_sha


def get_existing_run_ids(workflow_name: str, commit_sha: str) -> Set[int]:
    """Fetches currently indexed run IDs for workflow and commit."""
    list_cmd = [
        "gh", "run", "list",
        "--workflow", workflow_name,
        "--commit", commit_sha,
        "--json", "databaseId",
        "--limit", "30",
    ]
    code, out, _ = run_cmd(list_cmd, check=False)
    if code != 0 or not out.strip():
        return set()
    try:
        runs = json.loads(out)
        return {r["databaseId"] for r in runs if isinstance(r, dict) and "databaseId" in r}
    except (json.JSONDecodeError, KeyError):
        return set()


def dispatch_cd_workflow(
    commit_sha: str,
    workflow_name: str = "deploy-preview.yml",
    pre_existing_run_ids: Optional[Set[int]] = None,
    max_poll_attempts: int = 5,
    poll_interval: float = 2.0,
    dry_run: bool = False,
) -> Optional[int]:
    """Dispatches preview deployment workflow for commit SHA and correlates the new run ID."""
    if dry_run:
        print(f"[DRY-RUN] Would dispatch workflow '{workflow_name}' at ref '{commit_sha}'")
        return 12345

    if pre_existing_run_ids is None:
        pre_existing_run_ids = get_existing_run_ids(workflow_name, commit_sha)

    cmd = [
        "gh", "workflow", "run", workflow_name,
        "--ref", commit_sha,
        "-f", f"commit_sha={commit_sha}",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        # Fallback without -f if workflow inputs are not declared
        cmd = ["gh", "workflow", "run", workflow_name, "--ref", commit_sha]
        code, out, err = run_cmd(cmd, check=False)
        if code != 0:
            print(f"[ERROR] Failed to dispatch workflow '{workflow_name}': {err}", file=sys.stderr)
            return None

    # Poll for the newly created run ID
    for attempt in range(max_poll_attempts):
        if attempt > 0 and poll_interval > 0:
            time.sleep(poll_interval)
        current_runs = get_existing_run_ids(workflow_name, commit_sha)
        new_runs = current_runs - pre_existing_run_ids
        if new_runs:
            # Return newest run ID
            return max(new_runs)

    # Fallback to latest run if no new ID was distinguished but a run exists
    current_runs = get_existing_run_ids(workflow_name, commit_sha)
    if current_runs:
        return max(current_runs)

    print(f"[ERROR] Timed out waiting for new run of workflow '{workflow_name}' for commit '{commit_sha}'.", file=sys.stderr)
    return None


def wait_for_run(run_id: int, dry_run: bool = False) -> bool:
    """Waits for workflow run completion using gh run watch."""
    if dry_run:
        print(f"[DRY-RUN] Would watch run {run_id}")
        return True

    cmd = ["gh", "run", "watch", str(run_id), "--exit-status"]
    code, _, _ = run_cmd(cmd, check=False)
    return code == 0


def extract_preview_url_from_run(run_id: int, dry_run: bool = False) -> Optional[str]:
    """Inspects completed workflow run for preview environment URL."""
    if dry_run:
        return "https://preview.dry-run.local"

    cmd = ["gh", "run", "view", str(run_id), "--json", "jobs"]
    code, out, _ = run_cmd(cmd, check=False)
    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            jobs = data.get("jobs", [])
            for job in jobs:
                steps = job.get("steps", [])
                for step in steps:
                    # Look for URL in step outputs or names
                    step_name = step.get("name", "")
                    match = re.search(r"https?://[^\s'\"<>]+", step_name)
                    if match:
                        return match.group(0)
        except json.JSONDecodeError:
            pass
    return None


def post_preview_comment(issue_id: int, preview_url: str, commit_sha: str, dry_run: bool = False) -> bool:
    """Posts a preview URL comment to the originating issue."""
    body = (
        f"🚀 **Preview Environment Deployed**\n\n"
        f"- **Commit**: `{commit_sha[:7]}`\n"
        f"- **Preview URL**: [{preview_url}]({preview_url})\n"
    )
    if dry_run:
        print(f"[DRY-RUN] Would comment on issue #{issue_id}:\n{body}")
        return True

    cmd = ["gh", "issue", "comment", str(issue_id), "--body", body]
    code, _, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to post comment to issue #{issue_id}: {err}", file=sys.stderr)
        return False
    return True


def file_remediation_issue(
    issue_id: int,
    commit_sha: str,
    error_details: str,
    dry_run: bool = False,
) -> Optional[int]:
    """Files a governed fix(deploy) issue attached to the Project Board."""
    title = f"fix(deploy): preview deployment failed for commit {commit_sha[:7]}"
    body = f"""## Problem Description
Preview deployment failed for merged commit `{commit_sha}` (originating from issue #{issue_id}).

## Error Log / Context
```
{error_details.strip()}
```

## Acceptance Criteria
- [ ] Preview deployment succeeds for commit `{commit_sha[:7]}` (verify: `python3 scripts/deploy_preview.py --commit {commit_sha} --issue {issue_id}`)

## Decision Boundaries
- Target: preview environment CD pipeline
- Error handling: surface pipeline failure to originating issue

## Non-Goals
- Production release configuration

## Verification
`python3 scripts/deploy_preview.py --commit {commit_sha} --issue {issue_id}` exits 0.

## Dependencies
depends-on: none
touches: .github/workflows/deploy-preview.yml
parallel-eligible: true
"""
    if dry_run:
        print(f"[DRY-RUN] Would create issue '{title}'")
        return 9999

    cmd = [
        "gh", "issue", "create",
        "--title", title,
        "--body", body,
        "--label", "type:fix,priority:p1",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to create remediation issue: {err}", file=sys.stderr)
        return None

    # Parse issue number from URL output
    match = re.search(r"/issues/(\d+)", out)
    if not match:
        print(f"[ERROR] Could not parse issue number from: {out}", file=sys.stderr)
        return None

    new_issue_id = int(match.group(1))
    print(f"✅ Created remediation issue #{new_issue_id}")

    # Attach to project board as Ready; FAIL CLOSED if attachment fails
    sdlc_home = os.environ.get("ARU_SDLC_HOME", ".")
    attach_cmd = [
        sys.executable,
        str(Path(sdlc_home) / "scripts" / "update_issue_status.py"),
        "--issue", str(new_issue_id),
        "--status", "Ready",
        "--require-board",
    ]
    code, _, err = run_cmd(attach_cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to attach remediation issue #{new_issue_id} to Project Board: {err}", file=sys.stderr)
        return None

    # Notify originating issue
    notify_body = (
        f"⚠️ **Preview Deployment Failed**\n\n"
        f"Preview deployment failed for commit `{commit_sha[:7]}`. "
        f"Created governed remediation issue #{new_issue_id} on the Project Board."
    )
    run_cmd(["gh", "issue", "comment", str(issue_id), "--body", notify_body], check=False)

    return new_issue_id


def deploy_preview(
    commit_sha: str,
    issue_id: Optional[int] = None,
    workflow_name: str = "deploy-preview.yml",
    preview_url: Optional[str] = None,
    wait: bool = True,
    dry_run: bool = False,
) -> int:
    """Executes full preview deployment procedure."""
    # 1. Enforce merged commit invariant
    is_merged, resolved_sha = verify_commit_merged(commit_sha)
    if not is_merged:
        print(f"[ERROR] Commit '{commit_sha}' is not merged into the default branch.", file=sys.stderr)
        return 1

    commit_sha = resolved_sha

    if not issue_id:
        issue_id = get_originating_issue(commit_sha)

    if not issue_id:
        print(f"[ERROR] Originating issue ID not provided and could not be inferred for commit {commit_sha}.", file=sys.stderr)
        return 1

    print(f"Deploying preview for commit {commit_sha[:7]} (originating issue #{issue_id})...")

    pre_existing_runs = get_existing_run_ids(workflow_name, commit_sha) if not dry_run else set()
    run_id = dispatch_cd_workflow(
        commit_sha,
        workflow_name=workflow_name,
        pre_existing_run_ids=pre_existing_runs,
        dry_run=dry_run,
    )
    if run_id is None and not dry_run:
        remedy_id = file_remediation_issue(issue_id, commit_sha, f"Could not dispatch workflow '{workflow_name}' for ref '{commit_sha}'.", dry_run=dry_run)
        return 1

    if wait and run_id:
        success = wait_for_run(run_id, dry_run=dry_run)
        if not success and not dry_run:
            remedy_id = file_remediation_issue(issue_id, commit_sha, f"Workflow run {run_id} failed during execution.", dry_run=dry_run)
            return 1

    resolved_url = preview_url
    if not resolved_url and run_id:
        resolved_url = extract_preview_url_from_run(run_id, dry_run=dry_run)

    if not resolved_url and not dry_run:
        print(f"[ERROR] No preview URL could be determined from deployment run {run_id}. Provide --url or declare preview URL in workflow output.", file=sys.stderr)
        file_remediation_issue(issue_id, commit_sha, f"Deployment completed but no preview URL was found in run {run_id}.", dry_run=dry_run)
        return 1

    final_url = resolved_url or "https://preview.dry-run.local"
    comment_ok = post_preview_comment(issue_id, final_url, commit_sha, dry_run=dry_run)
    if not comment_ok and not dry_run:
        print(f"[ERROR] Failed to record preview URL on issue #{issue_id}.", file=sys.stderr)
        return 1

    print(f"✅ Preview deployed successfully: {final_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy preview environment for a merged commit.")
    parser.add_argument("--commit", required=True, help="Merged commit SHA to deploy")
    parser.add_argument("--issue", type=int, help="Originating issue ID (inferred from commit message if omitted)")
    parser.add_argument("--workflow", default="deploy-preview.yml", help="CD workflow filename (default: deploy-preview.yml)")
    parser.add_argument("--url", help="Preview URL (extracted from workflow run if omitted)")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for workflow run completion")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without mutations")

    args = parser.parse_args()
    return deploy_preview(
        commit_sha=args.commit,
        issue_id=args.issue,
        workflow_name=args.workflow,
        preview_url=args.url,
        wait=not args.no_wait,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
