---
name: address-pr-feedback
description: Fetches unresolved PR review comments, builds a fix checklist, implements changes in the branch worktree, replies with commit hashes, and resolves threads. Use when the user says address PR feedback, fix reviewer comments, resolve PR review, or update PR from feedback.
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

### Step 0: Which kind of feedback is this?

The picker emits two shapes under `feedback`. Check `work.unmet_gates` before
Step 1, because only one of them has threads to fetch.

| `work.unmet_gates` | Meaning | Go to |
|---|---|---|
| absent or empty | A reviewer left unresolved threads | Step 1 |
| non-empty | A Definition-of-Done gate only the author can clear | **Step 1G** |

Running Step 1 on a gate-fix item returns an empty checklist, because the picker
only surfaces these when the thread count is **zero**. Treating that empty
checklist as "nothing to do" is what makes the item reappear on the next cycle
forever, so it must be routed to Step 1G instead.

When present, `work.gate_details` contains the authoritative dry-run failure
message for a subtype-sensitive gate. Read it before choosing the action.

### Step 1G: Clear the author-only gate

No unresolved threads exist. Act on each name in `work.unmet_gates`:

- **`accept`** — read the linked issue and verify each acceptance criterion
  against the implementation and evidence. If satisfied, tick the boxes. If a
  criterion is genuinely unmet, leave it unticked and record why on the issue.
- **`ci`** — follow `remediate-ci-failure` with the complete hosted logs; do
  not retry or guess from the check title alone.
- **`rebased`** — in the PR's worktree: `git fetch origin && git rebase
  origin/main`, re-run the full local verification, then push with
  `--force-with-lease`. This **invalidates the prior review by design**
  (`merge_pr.py` requires a review at the current head), so the PR returns to
  `review` afterwards. That is correct; do not route around it.
- **`size`** — split the PR, or add `size-waiver: <rationale>` to its body
  explaining why splitting is worse. Never waive silently, and never waive
  purely to clear the gate.
- **`review-evidence`** — a peer already reviewed and threads are resolved, but
  Definition of Done still fails `review` because a resolved thread has no
  commit after the finding. Locate that resolved thread (not the unresolved
  checklist) and push a fix, or reply on it starting with `Withdrawn:` and
  why. This is not a request to self-review.
- **`tests`** — read `work.gate_details.tests`. If changed-file data is
  truncated, split the PR until the gate can inspect the complete diff;
  otherwise add or update test coverage for the changed production files.
- **`verification`** — read `work.gate_details.verification`. Refresh stale
  current-head evidence with `create_pr.py --refresh-pr`; if malformed or
  duplicate markers prevent the helper from refreshing, repair the marker
  structure first, then run the helper rather than fabricating its JSON.
- **`spec-sync`** — the code and spec table diverged. Update the spec table or
  the code so they agree, run `python3 "$ARU_SDLC_HOME/scripts/sync_spec.py"`
  (or the PR's documented sync command), and push the new head.

Then refresh evidence for the new head:

```
python3 "$ARU_SDLC_HOME/scripts/create_pr.py" --refresh-pr <PR_ID> --issue <N> \
  --verify-command "ruff check ." \
  --verify-command "python3 -m unittest discover tests"
```

Skip Steps 1-4 and return to the loop. If the gate still fails afterwards,
record why on the PR instead of repeating the identical push.

### Step 1: Fetch Inline PR Comments & Build Checklist
1. Fetch all unresolved inline PR comments using `python3 "$ARU_SDLC_HOME/scripts/fetch_pr_feedback.py" --pr <PR_ID>`.
2. Compile a Markdown task checklist mapping each comment to file location, line number, and requested change.
3. **Review-round threshold (issue #98):** inspect the `review rounds` line from
   `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <PR_ID> --dry-run`.
   When rounds are at or above the threshold (3), run
   `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <PR_ID> --emit-review-split`
   before editing. That posts automated split guidance and files follow-up
   issues with `depends-on:` edges. Round count is audit data — it does **not**
   escalate to a human and does **not** change merge authority.

### Step 2: Implement Fixes in Branch Worktree
1. Navigate to the branch worktree (`.worktrees/<branch-name>`).
2. Implement code modifications addressing each item in the review checklist.
3. When split guidance was emitted, shrink this PR to the **smallest coherent
   change** that can pass review; leave separable findings on the follow-up
   issues rather than expanding the PR again.
4. Run local unit tests and lint checks to ensure compliance.

### Step 3: Commit & Push Update
1. Create conventional commit message: `fix(review): address peer feedback for JWT auth`.
2. Push commits upstream to the PR branch.

### Step 4: Reply & Resolve Review Comments
1. Post reply comments on GitHub confirming the changes made and referencing the fix commit hash.
2. Mark review conversations as resolved.
3. Re-request review from maintainer/agent.
