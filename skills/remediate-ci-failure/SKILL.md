---
name: remediate-ci-failure
description: Diagnose and fix a failing current-head CI check on an authored PR.
---

# Remediate CI

1. Confirm the PR head and that you own the implementation branch.
2. Read complete failing logs with `gh run view --log-failed`.
3. Reproduce the failure locally.
4. Apply the smallest fix inside the existing claimed worktree and
   `touches:` budget.
5. Run focused local verification, commit, and push.
6. Re-read the new head and wait for fresh CI and fresh external review.

Never treat a stale successful run as current-head evidence.
