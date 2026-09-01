---
name: address-pr-feedback
description: Resolve current unresolved review findings or DIRTY merge conflicts on an authored PR.
---

# Address feedback

1. Run `fetch_pr_feedback.py --pr <n>`.
2. Confirm every finding belongs to the current PR and assigned review
   authority.
3. Fix actionable findings or resolve merge conflicts in the existing claimed worktree:
   - For review feedback: fix actionable findings in code.
   - For a DIRTY merge state / merge conflict: merge `origin/main` into the
     feature branch (never rebase or force push), resolving conflicts within
     declared `touches:` boundaries.
4. Run focused verification only, commit, and push.
5. Update the PR `## Verification` commands and rebind them to the exact head
   with `create_pr.py --refresh-verification <PR> --body-file <file>`.
6. For review feedback, reply with the fixing commit and resolve the thread
   through GitHub.
7. Wait for fresh exact-head local verification and a fresh verdict from the
   one assigned external or coding-agent authority.

Do not review your own work. Because the kernel has no scheduler, an external
event or timer must invoke `create_pr.py --refresh-reviewer` for governed
immediate unavailability or the effective policy timeout. Reviewer capacity probes
verify liveness only; if substantive review execution hits quota, recover
immediately with `create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`.
