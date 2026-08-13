#!/usr/bin/env python3
"""
revert_merge.py - Governed reverse gear for Aru_Agentic_SDLC.

Reverts a merged PR safely:
1. Locates the merge commit via the PR metadata or checkpoint tag.
2. Creates an isolated worktree branch 'revert/pr-<id>-<slug>' off origin/<baseRefName>.
3. Executes git revert. If clean, opens a revert PR pre-populated with
   Closes #N and Reverts #<id>.
4. Reopens affected issues on GitHub and moves them from 'Done' back to 'Ready'
   (or specified target status) with an explanatory comment.
5. If the revert encounters git conflicts, refuses with the exact conflict files
   and manual remediation instructions.
"""

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional

from common import (
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
        tags = [t.strip() for t in out.splitlines() if t.strip()]
        if len(tags) > 1:
            print(f"[ERROR] Multiple checkpoint tags found for PR #{pr_id}: {tags}. Ambiguous checkpoint tag.", file=sys.stderr)
            return None
        if len(tags) == 1:
            tag = tags[0]
            code_rev, sha, _ = run_cmd(["git", "rev-parse", f"{tag}^{{commit}}"], check=False)
            if code_rev == 0 and sha:
                return sha.strip()

    return None


def is_merge_commit(commit_sha: str, cwd: Optional[str] = None) -> bool:
    """Checks if a commit is a merge commit (has more than 1 parent)."""
    code, out, _ = run_cmd(["git", "rev-parse", f"{commit_sha}^2"], check=False, cwd=cwd)
    return code == 0


def get_unmerged_files(cwd: str) -> List[str]:
    """Detects all unmerged file paths from git status and diff-filter."""
    unmerged = set()
    code, out, _ = run_cmd(["git", "diff", "--name-only", "--diff-filter=U"], check=False, cwd=cwd)
    if code == 0 and out:
        for f in out.splitlines():
            if f.strip():
                unmerged.add(f.strip())

    code, status_out, _ = run_cmd(["git", "status", "--porcelain"], check=False, cwd=cwd)
    if code == 0 and status_out:
        unmerged_prefixes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
        for line in status_out.splitlines():
            if len(line) >= 3 and line[:2] in unmerged_prefixes:
                filepath = line[3:].strip().split(" -> ")[-1]
                if filepath:
                    unmerged.add(filepath)

    return sorted(list(unmerged))


def revert_merge_pr(
    pr_id: int,
    agent: str,
    family: str = "",
    dry_run: bool = False,
    target_status: str = "Ready",
    revert_issue: Optional[int] = None,
) -> int:
    """Main workflow function for reverting a PR."""
    if not agent:
        print("[ERROR] --agent is required. Revert PRs must be stamped with author identity.", file=sys.stderr)
        return EXIT_ERROR

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
    base_ref = pr_data.get("baseRefName", "main")
    linked_issues = parse_linked_issues(pr_data.get("body", ""))

    print(f"=== Revert Plan — PR #{pr_id}: {title} ===")
    print(f"  Merge Commit: {merge_sha}")
    print(f"  Base Branch: {base_ref}")
    print(f"  Linked Issues: {linked_issues if linked_issues else 'None'}")
    print(f"  Target Issue Status: {target_status}")

    title_slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:30]
    revert_branch = f"revert/pr-{pr_id}-{title_slug}"
    worktree_path = os.path.join(".worktrees", revert_branch.replace("/", "-"))

    if dry_run:
        print("\n[DRY RUN] Would create worktree from remote base, execute git revert, open revert PR, reopen issues, and update board.")
        return EXIT_OK

    # Ensure remote base is fresh
    run_cmd(["git", "fetch", "origin", base_ref], check=False)

    # Create worktree off origin/<baseRefName>
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    code_wt, _, err_wt = run_cmd(
        ["git", "worktree", "add", "-b", revert_branch, worktree_path, f"origin/{base_ref}"],
        check=False,
    )
    if code_wt != 0:
        # Retry attaching if branch exists
        code_wt2, _, err_wt2 = run_cmd(["git", "worktree", "add", worktree_path, revert_branch], check=False)
        if code_wt2 != 0:
            print(f"[ERROR] Could not create worktree at '{worktree_path}': {err_wt2 or err_wt}", file=sys.stderr)
            return EXIT_ERROR

    actual_path = worktree_path

    # Determine if merge commit or single commit revert
    is_merge = is_merge_commit(merge_sha, cwd=actual_path)
    if is_merge:
        revert_cmd = ["git", "revert", "-m", "1", "--no-edit", merge_sha]
        revert_cmd_str = f"git revert -m 1 {merge_sha}"
    else:
        revert_cmd = ["git", "revert", "--no-edit", merge_sha]
        revert_cmd_str = f"git revert {merge_sha}"

    code, out, err = run_cmd(revert_cmd, check=False, cwd=actual_path)
    if code != 0:
        # Conflict encountered
        conflicts = get_unmerged_files(actual_path)

        # Abort revert
        run_cmd(["git", "revert", "--abort"], check=False, cwd=actual_path)
        run_cmd(["git", "worktree", "remove", "--force", actual_path], check=False)

        print(f"\n[ERROR] Revert of PR #{pr_id} failed due to merge conflicts.", file=sys.stderr)
        print("Conflicting files:", file=sys.stderr)
        for cf in conflicts:
            print(f"  - {cf}", file=sys.stderr)
        print("\nManual Remediation Required:", file=sys.stderr)
        print(f"  1. Create branch '{revert_branch}' from origin/{base_ref} manually.", file=sys.stderr)
        print(f"  2. Execute '{revert_cmd_str}' and resolve conflicts manually.", file=sys.stderr)
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
    # Note: Revert PRs intentionally omit "Closes #<issue_number>" for the original
    # reverted issue(s) because revert PRs reopen (rather than close) those associated issues.
    # If --revert-issue is explicitly passed for a tracking issue of the revert itself,
    # that tracking issue is closed via closure_links.
    closure_links = []
    if revert_issue:
        closure_links.append(f"Closes #{revert_issue}")
    elif linked_issues:
        closure_links.extend([f"Closes #{issue_id}" for issue_id in linked_issues])

    closure_text = "\n".join(closure_links) if closure_links else ""
    reopen_text = "\n".join([f"- Reopens #{issue_id}" for issue_id in linked_issues]) if linked_issues else "None"
    pr_body = (
        f"## Revert Summary\n"
        f"This PR reverts PR #{pr_id} (\"{title}\"), reverting merge commit `{merge_sha[:7]}`.\n\n"
        f"## Reopened Issues\n"
        f"{reopen_text}\n\n"
        f"Reverts #{pr_id}\n"
    )
    if closure_text:
        pr_body += f"{closure_text}\n"

    pr_cmd = ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", revert_branch, "--base", base_ref]
    code_pr, pr_out, err_pr = run_cmd(pr_cmd, check=False, cwd=actual_path)
    if code_pr != 0:
        print(f"[ERROR] Failed to open revert PR: {err_pr}", file=sys.stderr)
        return EXIT_ERROR

    revert_pr_url = pr_out.strip()
    revert_pr_num = revert_pr_url.split("/")[-1] if "/" in revert_pr_url else revert_pr_url
    print(f"✅ Revert PR #{revert_pr_num} created: {revert_pr_url}")

    # Stamp identity and enqueue for review (failing closed if identity stamp fails)
    if not apply_identity(revert_pr_num, agent=agent, family=family):
        print(f"[ERROR] Failed to stamp author identity on Revert PR #{revert_pr_num}.", file=sys.stderr)
        return EXIT_ERROR
    enqueue_review(revert_pr_num)

    # Reopen affected issues & update board state (failing closed on any failure)
    for issue_id in linked_issues:
        # 1. Reopen GitHub issue
        code_reopen, _, err_reopen = run_cmd(["gh", "issue", "reopen", str(issue_id)], check=False)
        if code_reopen != 0:
            print(f"[ERROR] Failed to reopen Issue #{issue_id} on GitHub: {err_reopen}", file=sys.stderr)
            return EXIT_ERROR

        # 2. Update board status and labels
        board_ok = update_status(issue_id, target_status, require_board=True)
        if not board_ok:
            print(f"[ERROR] Failed to update board status for Issue #{issue_id} to '{target_status}'.", file=sys.stderr)
            return EXIT_ERROR

        # 3. Post explanatory comment
        comment_body = (
            f"⚠️ PR #{pr_id} has been reverted by Revert PR #{revert_pr_num} (branch `{revert_branch}`). "
            f"Moving issue status back to '{target_status}'."
        )
        code_comm, _, err_comm = run_cmd(["gh", "issue", "comment", str(issue_id), "--body", comment_body], check=False)
        if code_comm != 0:
            print(f"[ERROR] Failed to post comment on Issue #{issue_id}: {err_comm}", file=sys.stderr)
            return EXIT_ERROR

        print(f"✅ Issue #{issue_id} reopened on GitHub, moved to '{target_status}', and commented.")

    print(f"\n🎉 Governed revert of PR #{pr_id} complete. Revert PR #{revert_pr_num} is now in queue for review.")
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Governed revert helper for merged PRs under Aru_Agentic_SDLC.")
    parser.add_argument("--pr", type=int, required=True, help="Merged PR number to revert")
    parser.add_argument("--agent", type=str, required=True, help="Agent ID executing the revert")
    parser.add_argument("--model-family", "--family", dest="family", type=str, default="", help="Model family (e.g. google, anthropic)")
    parser.add_argument("--dry-run", action="store_true", help="Preview revert actions without mutating git/GitHub")
    parser.add_argument("--target-status", type=str, default="Ready", help="Status to move affected issues to (default: Ready)")
    parser.add_argument("--revert-issue", type=int, default=None, help="Optional tracking issue number to link with Closes #N")

    args = parser.parse_args()
    sys.exit(revert_merge_pr(
        args.pr,
        agent=args.agent,
        family=args.family,
        dry_run=args.dry_run,
        target_status=args.target_status,
        revert_issue=args.revert_issue,
    ))


if __name__ == "__main__":
    main()
