---
name: address-pr-feedback
description: Resolve current unresolved review findings or DIRTY merge conflicts on an authored PR.
---

# Address feedback

1. Run `python3 "$ARU_SDLC_HOME/scripts/fetch_pr_feedback.py" --pr <n>`. Read the original finding and its thread,
   not only the latest bot reply. Handle every still-applicable finding,
   including late results from any reviewer and summary-only P0/P1 findings. An outdated marker does not prove
   that a defect is fixed.
2. Identify the concrete consequence and evidence. Use one truthful disposition:

   | Disposition | Example and evidence |
   | --- | --- |
   | Code fix | A missing bound permits invalid input. The writer fixes it, cites the commit and relevant regression evidence, then resolves the thread. |
   | Evidence-backed disagreement | The reported duplicate operation is already prevented by the cited guard and existing race test. Explain both and resolve with that evidence; do not invent a code change. |
   | Advisory-only suggestion | A naming preference changes no behavior and violates no contract. Record the reasoned decision to keep the current name and resolve; no unrelated source or test edits are needed. |
   | Accepted separate follow-up | A broader refactor is useful but is not required for this PR's correctness. Record why the present behavior is safe, link the accepted tracked issue, and resolve. An actual defect cannot be deferred merely by filing an issue. |

   A `[P2]` suggestion usually takes the advisory-only disposition; a `[P0]` or
   `[P1]` finding needs a code fix or evidence-backed disagreement.
   Security/correctness defects remain blocking regardless of a low/info label.
   Every resolved finding cites its evidence: the fixing commit and test, or the
   guard and existing test that already prevent it.
   Do not suppress findings, fabricate agreement, or resolve a thread without
   handling its substance. Evidence-backed disposition is not a blanket waiver.

   A blocking review summary is cleared only by its original reviewer, distinct
   from the PR author. After the writer posts fixes and regression evidence,
   that reviewer submits a new COMMENT or APPROVE review on the exact current
   head with a standalone `Resolves review: <review_id>` line and written
   verification/disposition evidence. Use the numeric ID returned by the feedback
   helper or the GitHub review URL. This confirms every finding in that summary;
   do not resolve it while one defect remains. Use a separate line for each
   summary, with no code fences or new P0/P1 finding labels in the confirmation.
   An approval alone, an author acknowledgement, a new commit or an outdated
   marker cannot clear findings. A new push needs current-head confirmation again;
   editing the original summary also requires a confirmation after that edit.
   Do not erase the original summary to remove a blocker. If its reviewer is
   unavailable, leave the PR blocked and report why through GitHub.
3. The writer owns remediation in the existing claimed worktree. The reviewer
   stays independent and does not commit the fix; a reviewer who pushes to the
   PR becomes its last pusher, and GitHub then needs an approval from someone
   else.
4. For a DIRTY merge state, resolve the repository's default branch and merge
   `origin/<default-branch>` into the feature branch (never rebase or force
   push), resolving conflicts within declared `touches:` boundaries.
5. Verify actual changed behavior. Check existing relevant regression coverage
   before adding tests, following the scoped `tests/**` guidance in
   `.coderabbit.yaml`. Run `$ARU_SDLC_HOME/scripts/verify_consumer.sh` or narrower useful preflight; do not
   repeat broad suites or add unrelated merge-system cases for advisory prose.
6. After a code change, commit and push; reply with the fixing commit and
   evidence, then resolve the handled thread. Require the new exact-head
   `aru-governed-pr` check and a fresh approval from another account, because
   the push dismissed the earlier one. A disposition-only reply creates no new
   head and needs no extra commits, verification, reviewer brands or approval
   rounds; the existing exact-head approval must still stand when merging.

A green service check, a bot's review summary or a COMMENT review is not
approval. Approval is a GitHub review of the exact head by an account other than
the author that the default branch's `.aru/review.json` authorizes; under `human`
that is a listed named account.

When a finding repeats a mistake already seen on earlier pull requests, link it to
the maintained consumer or Kernel rule and the regression or agent-eval case that
should catch it, or file an issue naming the owner and source evidence, following
`integrations/adoption/README.md`. Copy no personal memories, customer data or
credentials into that record.

Do not approve your own work or poll for reviews. Waiting for an approval is not
author work; an operator or the external Driver arranges a reviewer on another
account.
