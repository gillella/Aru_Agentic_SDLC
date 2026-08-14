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
from pathlib import Path
from typing import Any, Optional

from common import get_repo_slug, run_cmd


def get_originating_issue(commit_sha: str) -> Optional[int]:
    """Inspects commit message to find linked Closes #<ID> issue."""
    code, out, _ = run_cmd(["git", "log", "-1", "--format=%B", commit_sha], check=False)
    if code != 0 or not out:
        return None
    match = re.search(r"\b(?:closes|fixes|resolves)\s*#(\d+)\b", out, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def dispatch_cd_workflow(
    commit_sha: str,
    workflow_name: str = "deploy-preview.yml",
    dry_run: bool = False,
) -> Optional[int]:
    """Dispatches the preview deployment workflow for the exact commit SHA and returns run ID."""
    if dry_run:
        print(f"[DRY-RUN] Would dispatch workflow '{workflow_name}' at ref '{commit_sha}'")
        return 12345

    cmd = [
        "gh", "workflow", "run", workflow_name,
        "--ref", commit_sha,
        "-f", f"commit_sha={commit_sha}",
    ]
    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        # Fallback without -f if inputs are not declared
        cmd = ["gh", "workflow", "run", workflow_name, "--ref", commit_sha]
        code, out, err = run_cmd(cmd, check=False)
        if code != 0:
            print(f"[ERROR] Failed to dispatch workflow '{workflow_name}': {err}", file=sys.stderr)
            return None

    # Retrieve the run ID for this dispatch
    list_cmd = [
        "gh", "run", "list",
        "--workflow", workflow_name,
        "--commit", commit_sha,
        "--json", "databaseId,status,conclusion",
        "--limit", "1",
    ]
    code, out, err = run_cmd(list_cmd, check=False)
    if code == 0 and out.strip():
        try:
            runs = json.loads(out)
            if runs and isinstance(runs, list):
                return runs[0].get("databaseId")
        except json.JSONDecodeError:
            pass

    return None


def wait_for_run(run_id: int, dry_run: bool = False) -> bool:
    """Waits for workflow run completion using gh run watch."""
    if dry_run:
        print(f"[DRY-RUN] Would watch run {run_id}")
        return True

    cmd = ["gh", "run", "watch", str(run_id), "--exit-status"]
    code, _, _ = run_cmd(cmd, check=False)
    return code == 0


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

    # Attach to project board as Ready
    sdlc_home = os.environ.get("ARU_SDLC_HOME", ".")
    attach_cmd = [
        sys.executable,
        str(Path(sdlc_home) / "scripts" / "update_issue_status.py"),
        "--issue", str(new_issue_id),
        "--status", "Ready",
        "--require-board",
    ]
    run_cmd(attach_cmd, check=False)

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
    if not issue_id:
        issue_id = get_originating_issue(commit_sha)

    if not issue_id:
        print(f"[ERROR] Originating issue ID not provided and could not be inferred for commit {commit_sha}.", file=sys.stderr)
        return 1

    print(f"Deploying preview for commit {commit_sha[:7]} (originating issue #{issue_id})...")

    run_id = dispatch_cd_workflow(commit_sha, workflow_name=workflow_name, dry_run=dry_run)
    if run_id is None and not dry_run:
        file_remediation_issue(issue_id, commit_sha, f"Could not dispatch workflow '{workflow_name}' for ref '{commit_sha}'.", dry_run=dry_run)
        return 1

    if wait and run_id:
        success = wait_for_run(run_id, dry_run=dry_run)
        if not success and not dry_run:
            file_remediation_issue(issue_id, commit_sha, f"Workflow run {run_id} failed during execution.", dry_run=dry_run)
            return 1

    resolved_url = preview_url or f"https://preview-{commit_sha[:7]}.aru-factory.local"
    post_preview_comment(issue_id, resolved_url, commit_sha, dry_run=dry_run)
    print(f"✅ Preview deployed successfully: {resolved_url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy preview environment for a merged commit.")
    parser.add_argument("--commit", required=True, help="Merged commit SHA to deploy")
    parser.add_argument("--issue", type=int, help="Originating issue ID (inferred from commit message if omitted)")
    parser.add_argument("--workflow", default="deploy-preview.yml", help="CD workflow filename (default: deploy-preview.yml)")
    parser.add_argument("--url", help="Preview URL (generated from commit SHA if omitted)")
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
