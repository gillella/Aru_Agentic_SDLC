---
name: address-pr-feedback
description: Resolve current unresolved review findings on an authored PR.
---

# Address feedback

1. Run `fetch_pr_feedback.py --pr <n>`.
2. Confirm every finding belongs to the current PR and assigned review
   authority.
3. Fix actionable findings in the existing claimed worktree.
4. Run focused verification, commit, and push.
5. Reply with the fixing commit and resolve the thread through GitHub.
6. Wait for fresh current-head CI and external review.

Do not review your own work and do not rotate reviewers automatically.
