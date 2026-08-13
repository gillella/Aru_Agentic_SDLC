#!/usr/bin/env python3
"""
revert_merge.py - Governed reverse gear for Aru_Agentic_SDLC.

Reverts a merged PR safely:
1. Locates the merge commit via the PR metadata or checkpoint tag.
2. Creates an isolated worktree branch 'revert/pr-<id>-<slug>'.
3. Executes git revert. If clean, opens a revert PR pre-populated with
   links to the original PR and its affected issues.
4. Moves affected issues from 'Done' back to 'Ready' (or specified target status)
   with an explanatory comment linking to the revert PR.
5. If the revert encounters git conflicts, refuses with the conflict details
   and manual remediation instructions.
"""

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional

from common import (
    create_worktree,
    run_cmd,
    run_gh_json,
)
from create_pr import apply_identity, enqueue_review
from update_issue_status import update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


def parse_linked_issues(pr_body: str) -> List[int]:
    """Parses linked issue numbers (Closes #X, Fixes #X, Resolves #X) from PR body."""
    if not pr_body:
        return []
    pattern = r"(?:closes|fixes|resolves)\s+#(\d+)"
    matches = re.findall(pattern, pr_body, re.IGNORECASE)
    return sorted(list(set(int(m) for m in matches)))


def fetch_pr_details(pr_id: int) -> Optional[Dict[str, Any]]:
    """Fetches details for a PR using gh CLI."""
    fields = "number,title,body,state,mergedAt,mergeCommit,headRefName,baseRefName"
    cmd = ["gh", "pr", "view", str(pr_id), "--json", fields]
    return run_gh_json(cmd)


def get_merge_commit_sha(pr_data: Dict[str, Any]) -> Optional[str]:
    """Extracts the merge commit SHA from PR data or git checkpoint tag."""
    pr_id = pr_data.get("number")
    merge_commit = pr_data.get("mergeCommit")
    if isinstance(merge_commit, dict) and merge_commit.get("oid"):
        return merge_commit["oid"]

    # Fallback to checking checkpoint tag ckpt/<pr_id>-<sha7>
    code, out, _ = run_cmd(["git", "tag", "-l", f"ckpt/{pr_id}-*"], check=False)
    if code == 0 and out:
        tag = out.splitlines()[0].strip()
        code_rev, sha, _ = run_cmd(["git", "rev-parse", f"{tag}^{{commit}}"], check=False)
        if code_rev == 0 and sha:
            return sha.strip()

    return None


def is_merge_commit(commit_sha: str, cwd: Optional[str] = None) -> bool:
    """Checks if a commit is a merge commit (has more than 1 parent)."""
    code, out, _ = run_cmd(["git", "rev-parse", f"{commit_sha}^2"], check=False, cwd=cwd)
    return code == 0


def revert_merge_pr(
    pr_id: int,
    agent: str = "",
    family: str = "",
    dry_run: bool = False,
    target_status: str = "Ready",
) -> int:
    """Main workflow function for reverting a PR."""
    pr_data = fetch_pr_details(pr_id)
    if not pr_data:
        print(f"[ERROR] Could not fetch details for PR #{pr_id}.", file=sys.stderr)
        return EXIT_ERROR

    state = pr_data.get("state", "").upper()
    merged_at = pr_data.get("mergedAt")
    if state != "MERGED" and not merged_at:
        print(f"[ERROR] PR #{pr_id} is not merged (state: {state}). Only merged PRs can be reverted.", file=sys.stderr)
        return EXIT_ERROR

    merge_sha = get_merge_commit_sha(pr_data)
    if not merge_sha:
        print(f"[ERROR] Could not determine merge commit SHA for PR #{pr_id}.", file=sys.stderr)
        return EXIT_ERROR

    title = pr_data.get("title", f"PR #{pr_id}")
    linked_issues = parse_linked_issues(pr_data.get("body", ""))

    print(f"=== Revert Plan — PR #{pr_id}: {title} ===")
    print(f"  Merge Commit: {merge_sha}")
    print(f"  Linked Issues: {linked_issues if linked_issues else 'None'}")
    print(f"  Target Issue Status: {target_status}")

    title_slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:30]
    revert_branch = f"revert/pr-{pr_id}-{title_slug}"
    worktree_path = os.path.join(".worktrees", revert_branch.replace("/", "-"))

    if dry_run:
        print("\n[DRY RUN] Would create worktree, execute git revert, open revert PR, and reopen issues.")
        return EXIT_OK

    # Create worktree
    actual_path = create_worktree(revert_branch, path=worktree_path)

    # Determine if merge commit or single commit revert
    if is_merge_commit(merge_sha, cwd=actual_path):
        revert_cmd = ["git", "revert", "-m", "1", "--no-edit", merge_sha]
    else:
        revert_cmd = ["git", "revert", "--no-edit", merge_sha]

    code, out, err = run_cmd(revert_cmd, check=False, cwd=actual_path)
    if code != 0:
        # Conflict encountered
        _, status_out, _ = run_cmd(["git", "status", "--porcelain"], check=False, cwd=actual_path)
        conflicts = [line[3:].strip() for line in status_out.splitlines() if line.startswith("UU") or line.startswith("U ") or line.startswith(" M")]

        # Abort revert
        run_cmd(["git", "revert", "--abort"], check=False, cwd=actual_path)
        run_cmd(["git", "worktree", "remove", "--force", actual_path], check=False)

        print(f"\n[ERROR] Revert of PR #{pr_id} failed due to merge conflicts.", file=sys.stderr)
        print("Conflicting files:", file=sys.stderr)
        for cf in conflicts:
            print(f"  - {cf}", file=sys.stderr)
        print("\nManual Remediation Required:", file=sys.stderr)
        print(f"  1. Create branch '{revert_branch}' manually.", file=sys.stderr)
        print(f"  2. Execute 'git revert -m 1 {merge_sha}' and resolve conflicts manually.", file=sys.stderr)
        print(f"  3. Commit resolved revert, push '{revert_branch}', and open a PR linking 'Reverts #{pr_id}'.", file=sys.stderr)
        return EXIT_CONFLICT

    print(f"✅ Clean revert achieved in worktree '{actual_path}'.")

    # Push revert branch
    code_push, _, err_push = run_cmd(["git", "push", "-u", "origin", revert_branch], check=False, cwd=actual_path)
    if code_push != 0:
        print(f"[ERROR] Failed to push revert branch '{revert_branch}': {err_push}", file=sys.stderr)
        return EXIT_ERROR

    # Create Revert PR
    pr_title = f"revert: PR #{pr_id} — {title}"
    issues_text = "\n".join([f"- Reopens #{issue_id}" for issue_id in linked_issues]) if linked_issues else "None"
    pr_body = (
        f"## Revert Summary\n"
        f"This PR reverts PR #{pr_id} (\"{title}\"), reverting merge commit `{merge_sha[:7]}`.\n\n"
        f"## Reopened Issues\n"
        f"{issues_text}\n\n"
        f"Reverts #{pr_id}\n"
    )

    pr_cmd = ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", revert_branch, "--base", "main"]
    code_pr, pr_out, err_pr = run_cmd(pr_cmd, check=False, cwd=actual_path)
    if code_pr != 0:
        print(f"[ERROR] Failed to open revert PR: {err_pr}", file=sys.stderr)
        return EXIT_ERROR

    revert_pr_url = pr_out.strip()
    revert_pr_num = revert_pr_url.split("/")[-1] if "/" in revert_pr_url else revert_pr_url
    print(f"✅ Revert PR #{revert_pr_num} created: {revert_pr_url}")

    # Stamp identity and enqueue for review
    if agent or family:
        apply_identity(revert_pr_num, agent=agent, family=family)
    enqueue_review(revert_pr_num)

    # Reopen affected issues
    for issue_id in linked_issues:
        update_status(issue_id, target_status)
        comment_body = (
            f"⚠️ PR #{pr_id} has been reverted by Revert PR #{revert_pr_num} (branch `{revert_branch}`). "
            f"Moving issue status back to '{target_status}'."
        )
        run_cmd(["gh", "issue", "comment", str(issue_id), "--body", comment_body], check=False)
        print(f"✅ Issue #{issue_id} moved to '{target_status}' and commented.")

    print(f"\n🎉 Governed revert of PR #{pr_id} complete. Revert PR #{revert_pr_num} is now in queue for review.")
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Governed revert helper for merged PRs under Aru_Agentic_SDLC.")
    parser.add_argument("--pr", type=int, required=True, help="Merged PR number to revert")
    parser.add_argument("--agent", type=str, default="", help="Agent ID executing the revert")
    parser.add_argument("--model-family", "--family", dest="family", type=str, default="", help="Model family (e.g. google, anthropic)")
    parser.add_argument("--dry-run", action="store_true", help="Preview revert actions without mutating git/GitHub")
    parser.add_argument("--target-status", type=str, default="Ready", help="Status to move affected issues to (default: Ready)")

    args = parser.parse_args()
    sys.exit(revert_merge_pr(args.pr, agent=args.agent, family=args.family, dry_run=args.dry_run, target_status=args.target_status))


if __name__ == "__main__":
    main()
