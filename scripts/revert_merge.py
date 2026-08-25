#!/usr/bin/env python3
# line-ceiling: 498
"""
revert_merge.py - Governed reverse gear for Aru_Agentic_SDLC.

Reverts a merged PR safely:
1. Locates the merge commit via the PR metadata or checkpoint tag.
2. Creates an isolated worktree branch 'revert/pr-<id>-<slug>' off origin/<baseRefName>.
3. Executes git revert and opens a tracked revert PR when clean.
4. Reopens affected issues and restores their active board status.
5. If the revert encounters git conflicts, refuses with the exact conflict files
   and manual remediation instructions.
"""

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional

from common import (
    claimed_by,
    get_issue,
    run_cmd,
    run_gh_json,
)
from create_pr import MODEL_FAMILIES, apply_identity, finalize_review_assignment
from update_issue_status import VALID_STATUSES, update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


def parse_linked_issues(pr_body: str) -> List[int]:
    """Parses issue references using every GitHub-supported closing keyword."""
    if not pr_body:
        return []
    pattern = r"(?<!\w)(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\b\s+#(\d+)"
    matches = re.findall(pattern, pr_body, re.IGNORECASE)
    return sorted(list(set(int(m) for m in matches)))


def revert_commit_marker(merge_sha: str) -> str:
    """Returns the exact commit-message line emitted by ``git revert``."""
    return f"This reverts commit {merge_sha}."


def has_exact_revert_commit(log_output: str, merge_sha: str) -> bool:
    """Accepts only a complete canonical git-revert marker line."""
    expected = revert_commit_marker(merge_sha)
    return any(line.rstrip("\r") == expected for line in log_output.splitlines())


def revert_pr_marker(pr_id: int, merge_sha: str) -> str:
    """Returns the durable marker that binds a recovery PR to its source."""
    return f"<!-- aru-revert:v1 source-pr={pr_id} merge-commit={merge_sha} -->"


def fetch_pr_details(pr_id: int) -> Optional[Dict[str, Any]]:
    """Fetches details for a PR using gh CLI."""
    fields = "number,title,body,state,mergedAt,mergeCommit,headRefName,baseRefName"
    cmd = ["gh", "pr", "view", str(pr_id), "--json", fields]
    return run_gh_json(cmd)


def find_existing_revert_pr(revert_branch: str) -> Optional[Dict[str, Any]]:
    """Finds an existing open PR for the revert branch if one was already created."""
    fields = "number,url,body,headRefName,baseRefName,headRefOid,commits"
    cmd = ["gh", "pr", "list", "--head", revert_branch, "--state", "open", "--json", fields]
    prs = run_gh_json(cmd)
    if isinstance(prs, list) and prs:
        return prs[0]
    return None


def validate_existing_revert_pr(
    existing_pr: Dict[str, Any],
    *,
    revert_branch: str,
    base_ref: str,
    pr_id: int,
    merge_sha: str,
    revert_issue: int,
) -> bool:
    """Fail closed unless a recovery PR is bound to the exact revert operation."""
    expected_lines = {
        revert_pr_marker(pr_id, merge_sha),
        f"Reverts #{pr_id}",
        f"Closes #{revert_issue}",
    }
    body_lines = {line.strip() for line in str(existing_pr.get("body") or "").splitlines()}
    commits = existing_pr.get("commits")
    commit_is_bound = False
    if isinstance(commits, list) and len(commits) == 1 and isinstance(commits[0], dict):
        commit = commits[0]
        message = f"{commit.get('messageHeadline', '')}\n{commit.get('messageBody', '')}"
        commit_is_bound = (
            commit.get("oid") == existing_pr.get("headRefOid")
            and has_exact_revert_commit(message, merge_sha)
        )
    return (
        existing_pr.get("headRefName") == revert_branch
        and existing_pr.get("baseRefName") == base_ref
        and isinstance(existing_pr.get("headRefOid"), str)
        and bool(existing_pr["headRefOid"].strip())
        and commit_is_bound
        and expected_lines.issubset(body_lines)
    )


def validate_existing_worktree(worktree_path: str, revert_branch: str) -> bool:
    """Verify that a reused path is the clean worktree for the expected branch."""
    code_root, root, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], check=False, cwd=worktree_path)
    code_branch, branch, _ = run_cmd(["git", "branch", "--show-current"], check=False, cwd=worktree_path)
    code_status, status, _ = run_cmd(["git", "status", "--porcelain"], check=False, cwd=worktree_path)
    return (
        code_root == 0
        and os.path.realpath(root.strip()) == os.path.realpath(worktree_path)
        and code_branch == 0
        and branch.strip() == revert_branch
        and code_status == 0
        and not status.strip()
    )


def validate_reused_branch_state(worktree_path: str, base_ref: str, merge_sha: str) -> bool:
    """Allow only an untouched base or one canonical revert commit on a reused branch."""
    code_ancestor, _, _ = run_cmd(
        ["git", "merge-base", "--is-ancestor", f"origin/{base_ref}", "HEAD"],
        check=False,
        cwd=worktree_path,
    )
    code_count, count_text, _ = run_cmd(
        ["git", "rev-list", "--count", f"origin/{base_ref}..HEAD"],
        check=False,
        cwd=worktree_path,
    )
    if code_ancestor != 0 or code_count != 0 or not count_text.strip().isdigit():
        return False
    commit_count = int(count_text.strip())
    if commit_count == 0:
        return True
    if commit_count != 1:
        return False
    code_message, message, _ = run_cmd(["git", "log", "-1", "--format=%B", "HEAD"], check=False, cwd=worktree_path)
    return code_message == 0 and has_exact_revert_commit(message, merge_sha)


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


def revert_merge_pr(  # noqa: C901, PLR0912, PLR0915
    pr_id: int,
    agent: str,
    family: str = "",
    revert_issue: Optional[int] = None,
    dry_run: bool = False,
    target_status: str = "Ready",
) -> int:
    """Main workflow function for reverting a PR."""
    agent = (agent or "").strip()
    if not agent:
        print("[ERROR] --agent is required and cannot be empty. Revert PRs must be stamped with author identity.", file=sys.stderr)
        return EXIT_ERROR

    family = (family or "").strip().lower()
    if not family:
        print(f"[ERROR] --model-family is required. Allowed families: {', '.join(MODEL_FAMILIES)}", file=sys.stderr)
        return EXIT_ERROR

    if family not in MODEL_FAMILIES:
        print(f"[ERROR] Unknown model family '{family}'. Allowed: {', '.join(MODEL_FAMILIES)}", file=sys.stderr)
        return EXIT_ERROR

    if not revert_issue or revert_issue <= 0:
        print("[ERROR] --revert-issue <ID> is required. Revert PRs must link a tracked issue with 'Closes #<ID>' to pass the merge gate.", file=sys.stderr)
        return EXIT_ERROR

    # Preflight validation of tracking issue
    tracking_issue = get_issue(revert_issue)
    if not tracking_issue:
        print(f"[ERROR] Revert tracking issue #{revert_issue} not found.", file=sys.stderr)
        return EXIT_ERROR

    if tracking_issue.get("state", "").upper() != "OPEN":
        print(f"[ERROR] Revert tracking issue #{revert_issue} is not OPEN.", file=sys.stderr)
        return EXIT_ERROR

    holder = claimed_by(tracking_issue)
    if holder != agent:
        print(f"[ERROR] Revert tracking issue #{revert_issue} is not claimed by agent '{agent}' (current holder: '{holder}'). Claim it first.", file=sys.stderr)
        return EXIT_ERROR

    # Preflight validation of target status
    canonical_status = next((s for s in VALID_STATUSES if s.lower() == target_status.strip().lower()), None)
    if not canonical_status:
        print(f"[ERROR] '{target_status}' is not a valid target status. Expected one of: {', '.join(VALID_STATUSES)}", file=sys.stderr)
        return EXIT_ERROR

    if canonical_status == "Done":
        print("[ERROR] Revert cannot set target issue status to 'Done'. Issues must be restored to an active/unresolved state.", file=sys.stderr)
        return EXIT_ERROR

    target_status = canonical_status

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
    print(f"  Revert Tracking Issue: #{revert_issue}")
    print(f"  Linked Issues to Reopen: {linked_issues if linked_issues else 'None'}")
    print(f"  Target Issue Status: {target_status}")

    title_slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:30]
    revert_branch = f"revert/pr-{pr_id}-{title_slug}"
    worktree_path = os.path.join(".worktrees", revert_branch.replace("/", "-"))

    if dry_run:
        print("\n[DRY RUN] Would create worktree from remote base, execute git revert, open revert PR, reopen issues, and update board.")
        return EXIT_OK

    # Check if a revert PR was already created in a previous attempt (resumption support)
    existing_pr = find_existing_revert_pr(revert_branch)
    if existing_pr:
        if not validate_existing_revert_pr(
            existing_pr,
            revert_branch=revert_branch,
            base_ref=base_ref,
            pr_id=pr_id,
            merge_sha=merge_sha,
            revert_issue=revert_issue,
        ):
            print(
                f"[ERROR] Existing revert PR for branch '{revert_branch}' is not bound to "
                f"PR #{pr_id}, merge {merge_sha}, base '{base_ref}', and tracking issue #{revert_issue}.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        revert_pr_num = str(existing_pr["number"])
        revert_pr_url = existing_pr.get("url", f"#{revert_pr_num}")
        print(f"ℹ️ Revert PR #{revert_pr_num} already exists ({revert_pr_url}). Resuming post-creation steps...")
    else:
        # Ensure remote base is fresh
        code_fetch, _, err_fetch = run_cmd(["git", "fetch", "origin", base_ref], check=False)
        if code_fetch != 0:
            print(f"[ERROR] Failed to fetch origin/{base_ref}: {err_fetch}", file=sys.stderr)
            return EXIT_ERROR

        code_verify, _, err_verify = run_cmd(["git", "rev-parse", "--verify", f"origin/{base_ref}"], check=False)
        if code_verify != 0:
            print(f"[ERROR] Remote base branch 'origin/{base_ref}' does not exist or cannot be resolved: {err_verify}", file=sys.stderr)
            return EXIT_ERROR

        # Create or reuse worktree off origin/<baseRefName>
        reused_branch = False
        if os.path.exists(worktree_path):
            actual_path = worktree_path
            reused_branch = True
            if not validate_existing_worktree(actual_path, revert_branch):
                print(
                    f"[ERROR] Existing path '{actual_path}' is not a clean worktree for branch '{revert_branch}'.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            print(f"ℹ️ Reusing existing worktree at '{actual_path}'.")
        else:
            os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
            code_wt, _, err_wt = run_cmd(
                ["git", "worktree", "add", "-b", revert_branch, worktree_path, f"origin/{base_ref}"],
                check=False,
            )
            if code_wt != 0:
                # Branch might already exist from a prior attempt; attach worktree to existing branch
                code_wt2, _, err_wt2 = run_cmd(
                    ["git", "worktree", "add", worktree_path, revert_branch],
                    check=False,
                )
                if code_wt2 != 0:
                    print(f"[ERROR] Could not create or attach worktree for branch '{revert_branch}' at '{worktree_path}': {err_wt} / {err_wt2}", file=sys.stderr)
                    return EXIT_ERROR
                reused_branch = True
            actual_path = worktree_path

        if reused_branch and not validate_reused_branch_state(actual_path, base_ref, merge_sha):
            print(
                f"[ERROR] Reused branch '{revert_branch}' is not exactly origin/{base_ref} or one canonical revert commit ahead.",
                file=sys.stderr,
            )
            return EXIT_ERROR

        # Check only commits added on the revert branch. The default message from
        # `git revert --no-edit` records the exact reverted commit in its body.
        code_log, out_log, _ = run_cmd(
            ["git", "log", "--format=%B", f"origin/{base_ref}..HEAD"],
            check=False,
            cwd=actual_path,
        )
        has_revert_commit = code_log == 0 and has_exact_revert_commit(out_log, merge_sha)

        if not has_revert_commit:
            is_merge = is_merge_commit(merge_sha, cwd=actual_path)
            if is_merge:
                revert_cmd = ["git", "revert", "-m", "1", "--no-edit", merge_sha]
                revert_cmd_str = f"git revert -m 1 {merge_sha}"
            else:
                revert_cmd = ["git", "revert", "--no-edit", merge_sha]
                revert_cmd_str = f"git revert {merge_sha}"

            code, out, err = run_cmd(revert_cmd, check=False, cwd=actual_path)
            if code != 0:
                conflicts = get_unmerged_files(actual_path)
                run_cmd(["git", "revert", "--abort"], check=False, cwd=actual_path)
                run_cmd(["git", "worktree", "remove", "--force", actual_path], check=False)
                run_cmd(["git", "branch", "-D", revert_branch], check=False)

                if conflicts:
                    print(f"\n[ERROR] Revert of PR #{pr_id} failed due to merge conflicts.", file=sys.stderr)
                    print("Conflicting files:", file=sys.stderr)
                    for cf in conflicts:
                        print(f"  - {cf}", file=sys.stderr)
                    print("\nManual Remediation Required:", file=sys.stderr)
                    print(f"  1. Create branch '{revert_branch}' from origin/{base_ref} manually.", file=sys.stderr)
                    print(f"  2. Execute '{revert_cmd_str}' and resolve conflicts manually.", file=sys.stderr)
                    print(f"  3. Commit resolved revert, push '{revert_branch}', and open a PR linking 'Reverts #{pr_id}'.", file=sys.stderr)
                    return EXIT_CONFLICT
                else:
                    print(f"\n[ERROR] Revert of PR #{pr_id} failed: {err or out}", file=sys.stderr)
                    print(f"Command attempted: {revert_cmd_str}", file=sys.stderr)
                    return EXIT_ERROR

            print(f"✅ Clean revert achieved in worktree '{actual_path}'.")

        # Push revert branch
        code_push, _, err_push = run_cmd(["git", "push", "-u", "origin", revert_branch], check=False, cwd=actual_path)
        if code_push != 0:
            print(f"[ERROR] Failed to push revert branch '{revert_branch}': {err_push}", file=sys.stderr)
            return EXIT_ERROR

        # Create Revert PR
        pr_title = f"revert: PR #{pr_id} — {title}"
        closure_text = f"Closes #{revert_issue}"
        reopen_text = "\n".join([f"- Reopens #{issue_id}" for issue_id in linked_issues]) if linked_issues else "None"
        pr_body = (
            f"## Revert Summary\n"
            f"This PR reverts PR #{pr_id} (\"{title}\"), reverting merge commit `{merge_sha[:7]}`.\n\n"
            f"{revert_pr_marker(pr_id, merge_sha)}\n\n"
            f"## Reopened Issues\n"
            f"{reopen_text}\n\n"
            f"Reverts #{pr_id}\n"
            f"{closure_text}\n"
        )

        pr_cmd = ["gh", "pr", "create", "--draft", "--title", pr_title, "--body", pr_body, "--head", revert_branch, "--base", base_ref]
        code_pr, pr_out, err_pr = run_cmd(pr_cmd, check=False, cwd=actual_path)
        if code_pr != 0:
            print(f"[ERROR] Failed to open revert PR: {err_pr}", file=sys.stderr)
            return EXIT_ERROR

        revert_pr_url = pr_out.strip()
        revert_pr_num = revert_pr_url.split("/")[-1] if "/" in revert_pr_url else revert_pr_url
        print(f"✅ Revert PR #{revert_pr_num} created: {revert_pr_url}")

    # Affected issues stay closed until identity and CodeRabbit finalization succeed.
    if not apply_identity(revert_pr_num, agent=agent, family=family):
        print(f"[ERROR] Failed to stamp author identity on Revert PR #{revert_pr_num}.", file=sys.stderr)
        return EXIT_ERROR
    if not finalize_review_assignment(revert_pr_num, revert_issue):
        print(f"[ERROR] Failed to finalize CodeRabbit review for Revert PR #{revert_pr_num}; affected issues remain closed.", file=sys.stderr)
        return EXIT_ERROR

    # Reopen affected issues & update board state (failing closed on any failure)
    failed_issues = []
    for issue_id in linked_issues:
        # 1. Reopen GitHub issue
        code_reopen, _, err_reopen = run_cmd(["gh", "issue", "reopen", str(issue_id)], check=False)
        if code_reopen != 0:
            print(f"[ERROR] Failed to reopen Issue #{issue_id} on GitHub: {err_reopen}", file=sys.stderr)
            failed_issues.append((issue_id, "reopen"))
            continue

        # 2. Update board status and labels
        board_ok = update_status(issue_id, target_status, require_board=True)
        if not board_ok:
            print(f"[ERROR] Failed to update board status for Issue #{issue_id} to '{target_status}'.", file=sys.stderr)
            failed_issues.append((issue_id, "board_status"))
            continue

        # 3. Post explanatory comment
        comment_body = (
            f"⚠️ PR #{pr_id} has been reverted by Revert PR #{revert_pr_num} (branch `{revert_branch}`). "
            f"Moving issue status back to '{target_status}'."
        )
        code_comm, _, err_comm = run_cmd(["gh", "issue", "comment", str(issue_id), "--body", comment_body], check=False)
        if code_comm != 0:
            print(f"[ERROR] Failed to post comment on Issue #{issue_id}: {err_comm}", file=sys.stderr)
            failed_issues.append((issue_id, "comment"))
            continue

        print(f"✅ Issue #{issue_id} reopened on GitHub, moved to '{target_status}', and commented.")

    if failed_issues:
        print(f"\n[ERROR] Revert PR #{revert_pr_num} processed, but issue restoration failed for: {failed_issues}", file=sys.stderr)
        print("Manual remediation or re-running revert_merge.py is required.", file=sys.stderr)
        return EXIT_ERROR

    print(f"\n🎉 Governed revert of PR #{pr_id} complete. Revert PR #{revert_pr_num} is assigned to CodeRabbit.")
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Governed revert helper for merged PRs under Aru_Agentic_SDLC.")
    parser.add_argument("--pr", type=int, required=True, help="Merged PR number to revert")
    parser.add_argument("--agent", type=str, required=True, help="Agent ID executing the revert")
    parser.add_argument("--model-family", "--family", dest="family", type=str, required=True, choices=MODEL_FAMILIES, help="Model family (e.g. google, anthropic)")
    parser.add_argument("--revert-issue", type=int, required=True, help="Tracking issue number to link with Closes #N (must be claimed by agent)")
    parser.add_argument("--dry-run", action="store_true", help="Preview revert actions without mutating git/GitHub")
    parser.add_argument("--target-status", type=str, default="Ready", help="Status to move affected issues to (default: Ready)")

    args = parser.parse_args()
    sys.exit(revert_merge_pr(
        args.pr,
        agent=args.agent,
        family=args.family,
        revert_issue=args.revert_issue,
        dry_run=args.dry_run,
        target_status=args.target_status,
    ))


if __name__ == "__main__":
    main()
