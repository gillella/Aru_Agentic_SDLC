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
   - For a DIRTY merge state / merge conflict: resolve the repository's default
     branch and merge `origin/<default-branch>` into the feature branch (never
     rebase or force push), resolving conflicts within declared `touches:`
     boundaries.
4. Run `.aru/verify.sh` or narrower focused checks locally when useful, commit,
   and push. Local output is preflight or audit evidence, not merge authority.
5. For review feedback, reply with the fixing commit and resolve the thread
   through GitHub.
6. Wait for the exact-head `aru-governed-pr` server check and, for a Tier 2-3
   change, a fresh verdict from the one assigned external or coding-agent
   authority.

Do not review your own work or poll reviewer state. For Tier 2-3, after the
push, the external Driver owns the one review-continuation event defined in
`docs/KERNEL-CONTRACT.md`, including immediate explicit-unavailability handling
or the configured policy timeout. If substantive coding review aborts or loses
capacity, report the truthful reason through
`create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`.

Retire Sourcery and CodeAnt. Prefer usable authenticated current-head CodeRabbit;
otherwise immediately select an available independent coding reviewer through
the governed refresh helper. A failed availability check does not wait for the
completion timer. Preserve Tier 0–1 without authoritative-review waits.
