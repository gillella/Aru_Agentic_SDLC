---
name: address-pr-feedback
description: Procedure for fetching inline PR review comments, converting reviewer feedback into a checklist, applying fixes, and replying to comments.
triggers:
  - "address PR feedback"
  - "fix reviewer comments"
  - "resolve PR review"
  - "update PR from feedback"
do_not_trigger_for:
  - "reviewing someone else's PR (use code-review instead)"
  - "fixing CI build failures (use remediate-ci-failure instead)"
---

# Address PR Review Feedback Procedure

This skill dictates the procedure for processing human developer or peer agent review comments on an open Pull Request.

---

## Procedure Steps

### Step 1: Fetch Inline PR Comments & Build Checklist
1. Fetch all unresolved inline PR comments using `python3 scripts/fetch_pr_feedback.py --pr <PR_ID>`.
2. Compile a Markdown task checklist mapping each comment to file location, line number, and requested change.

### Step 2: Implement Fixes in Branch Worktree
1. Navigate to the branch worktree (`.worktrees/<branch-name>`).
2. Implement code modifications addressing each item in the review checklist.
3. Run local unit tests and lint checks to ensure compliance.

### Step 3: Commit & Push Update
1. Create conventional commit message: `fix(review): address peer feedback for JWT auth`.
2. Push commits upstream to the PR branch.

### Step 4: Reply & Resolve Review Comments
1. Post reply comments on GitHub confirming the changes made and referencing the fix commit hash.
2. Mark review conversations as resolved.
3. Re-request review from maintainer/agent.
