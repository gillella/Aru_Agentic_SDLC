---
name: remediate-ci-failure
description: Repair stale or failing exact-head local verification on an authored PR.
---

# Remediate Local Verification

1. Confirm the PR head and that you own the implementation branch.
2. Read the PR `## Verification` section and identify the stale, malformed, or
   prohibited focused command entry.
3. Re-run only the focused local verification needed for the current head.
4. Apply the smallest fix inside the existing claimed worktree and
   `touches:` budget.
5. Commit and push if code changed, then refresh the PR-body evidence with
   `create_pr.py --refresh-verification <PR> --body-file <file>`.
6. Re-read the new head and wait for fresh exact-head local verification and a
   fresh verdict from the one assigned external or distinct coding-agent
   reviewer.

Never treat stale or broad verification text as current-head evidence.
