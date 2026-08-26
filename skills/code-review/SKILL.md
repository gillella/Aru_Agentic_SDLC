---
name: code-review
description: Reviews a PR only when the operator explicitly assigned this independent coding agent as the emergency fallback after external reviewers were exhausted or their wait was declared excessive.
---

# Emergency Independent-Agent Code Review

External review is normal: `create_pr.py` labels every new PR
`review:coderabbit`, and only an operator may reassign it to Sourcery or
CodeAnt, recording the concrete unavailability or excessive wait that justified
the move. Reassignment is never automatic. A coding agent may review only when
`reassign_review.py` has already moved one PR to exactly `review:agent` after
the operator recorded external exhaustion or an operator-declared excessive
wait, and assigned exactly `reviewer:<CURRENT_AGENT_ID>`. This is recovery of
one explicit assignment, not a review queue, rotation, scheduler, or permission
to select a PR yourself.

Before inspecting the diff, verify all of the following from live GitHub data:

- exactly one `review:agent` authority label and no other `review:` label;
- exactly one `reviewer:<id>`, matching this task's stable agent id;
- exactly one non-empty `author:<id>`, different from the reviewer;
- the PR is open and its exact current head SHA is known.

If any condition fails, do not review. Release only a claim belonging to this
agent, report the malformed assignment, and return to the picker.

For a valid assignment:

1. Confirm it with `claim_issue.py --pr <PR> --agent <ID>`; the helper only
   resumes a preassigned emergency review and refuses normal review claims.
2. Inspect the exact head in an isolated, detached `review-pr-<PR>-<ID>`
   worktree. Compare it with the current base, acceptance criteria, declared
   `touches:`, security boundaries, and focused tests. Never edit the author's
   branch or remediate findings yourself.
3. Submit a substantive GitHub review on that exact head. Findings remain open
   for the author; do not complete the review until a later author push has
   been freshly reviewed. A push invalidates every earlier completion.
4. When the exact head has no blocking findings, complete attribution with:

   `claim_issue.py --pr <PR> --agent <ID> --model-family <FAMILY> --complete-review --review-disposition <no-findings|findings-resolved>`

   The helper requires the preassignment, a substantive current-head GitHub
   review, independent identity, family, completion time, and finding
   disposition. It then releases the transient reviewer claim.
5. Return to the picker. Merge remains exclusively governed by `merge_pr.py`;
   CI, acceptance, verification, thread, size, issue-link, and freshness gates
   are unchanged.

Labels, ordinary comments, self-review, stale heads, missing family, malformed
or duplicate completion markers, and ambiguous identity never satisfy review.

Human operational authorization for money, production cutover, destructive
migration, credentials, or external-account mutations remains a separate gate
and is never supplied by a code review.
