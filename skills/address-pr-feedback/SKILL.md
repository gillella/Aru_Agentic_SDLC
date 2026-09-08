---
name: address-pr-feedback
description: Resolve current unresolved review findings or DIRTY merge conflicts on an authored PR.
---

# Address feedback

1. Run `fetch_pr_feedback.py --pr <n>`. Read the original finding and its thread,
   not only the latest bot reply. Handle every still-applicable finding,
   including late results from replaced or retired reviewers. An outdated
   marker does not prove that a defect is fixed; a late result does not replace
   the assigned approval authority.
2. Identify the concrete consequence and evidence. Use one truthful disposition:

   | Disposition | Example and evidence |
   | --- | --- |
   | Code fix | A missing bound permits invalid input. The writer fixes it, cites the commit and relevant regression evidence, then resolves the thread. |
   | Evidence-backed disagreement | The reported duplicate operation is already prevented by the cited guard and existing race test. Explain both and resolve with that evidence; do not invent a code change. |
   | Advisory-only suggestion | A naming preference changes no behavior and violates no contract. Record the reasoned decision to keep the current name and resolve; no unrelated source or test edits are needed. |
   | Accepted separate follow-up | A broader refactor is useful but is not required for this PR's correctness. Record why the present behavior is safe, link the accepted tracked issue, and resolve. An actual defect cannot be deferred merely by filing an issue. |

   Security/correctness defects remain blocking regardless of a low/info label.
   Do not suppress findings, fabricate agreement, or resolve a thread without
   handling its substance. Evidence-backed disposition is not a blanket waiver.
3. The writer owns remediation in the existing claimed worktree. The reviewer
   stays independent and does not commit the fix. If the reviewer becomes an
   author, record that contribution and replace their now-invalid authority
   through `create_pr.py --refresh-reviewer <PR>
   --coding-reviewer-unavailable <truthful reason>`. The replacement must be a
   distinct non-author, not the same person under another identity.
4. For a DIRTY merge state, resolve the repository's default branch and merge
   `origin/<default-branch>` into the feature branch (never rebase or force
   push), resolving conflicts within declared `touches:` boundaries.
5. Verify actual changed behavior. Check existing relevant regression coverage
   before adding tests, following the scoped `tests/**` guidance in
   `.coderabbit.yaml`. Run `.aru/verify.sh` or narrower useful preflight; do not
   repeat broad suites or add unrelated merge-system cases for advisory prose.
6. After a code change, commit and push; reply with the fixing commit and
   evidence, then resolve the handled thread. Require the new exact-head
   `aru-governed-pr` check and, for Tier 2-3, one fresh independent verdict.
   A disposition-only reply does not create a new head or require gratuitous
   commits, repeated verification, extra reviewer brands or extra approval
   rounds. Existing exact-head authority must still be valid when merging.

Do not review your own work or poll reviewer state. For Tier 2-3, after the
push, the external Driver owns the one review-continuation event defined in
`docs/KERNEL-CONTRACT.md`, including immediate explicit-unavailability handling
or the configured policy timeout. If substantive coding review aborts or loses
capacity, report the truthful reason through
`create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`.

Retire Sourcery and CodeAnt. Prefer usable authenticated current-head CodeRabbit;
otherwise immediately select an available independent coding reviewer through
the governed refresh helper. A failed availability check does not wait for the
completion timer. Preserve Tier 0-1 without authoritative-review waits.
