---
name: address-pr-feedback
description: Resolve current unresolved review findings on an authored PR.
---

# Address feedback

1. Run `fetch_pr_feedback.py --pr <n>`.
2. Confirm every finding belongs to the current PR and assigned review
   authority.
3. Fix actionable findings in the existing claimed worktree.
4. Run focused verification only, commit, and push.
5. Update the PR `## Verification` commands and rebind them to the exact head
   with `create_pr.py --refresh-verification <PR> --body-file <file>`.
6. Reply with the fixing commit and resolve the thread through GitHub.
7. Wait for fresh exact-head local verification and a fresh verdict from the
   one assigned external or coding-agent authority.

Do not review your own work. Change reviewer authority only through the
governed immediate-unavailability or 15-minute pending fallback in
`create_pr.py --refresh-reviewer`.
