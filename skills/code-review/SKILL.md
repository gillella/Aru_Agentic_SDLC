---
name: code-review
description: Reviews an open pull request in an isolated git worktree for correctness, security, tests, and Closes #N linkage under Aru_Agentic_SDLC. Use when the user says code review, review PR, audit pull request, or review code.
triggers:
  - "code review"
  - "review PR #<ID>"
  - "audit pull request"
  - "review code"
do_not_trigger_for:
  - "implementing an unassigned issue (use implement-next-issue instead)"
  - "addressing reviewer comments on your own PR (use address-pr-feedback instead)"
---

# Code Review Procedure

This skill defines the declarative code review procedure for evaluating Pull Requests submitted by human developers or AI agents.

---

## Procedure Steps

### Step 1: Fetch PR & Create Review Worktree
1. Fetch PR details, title, body, diffs, and linked issue (`Closes #X`).
2. Create an isolated git worktree for the PR branch in `.worktrees/review-pr-<PR_ID>` to perform local verification without disturbing your active working directory.

### Step 2: Goal Alignment & Issue Tracing
- Verify that the PR links to an open issue (`Closes #X`).
- Confirm that the PR changes directly address all acceptance criteria outlined in the issue.

### Step 3: Code Quality & Architecture Audit
- Check for subtle bugs, logic flaws, race conditions, or unhandled edge cases.
- Ensure public API signatures, schema types, and data models remain consistent.
- Verify zero unused imports, dead code, or debug statements.
- Apply the **narrower-than-reality** heuristic below to every enforcement, comparison, decision, or state assumption whose effective scope can be narrower than the reality it governs.

### Step 4: Test Coverage & Verification
- Verify that new feature logic or bug fixes are accompanied by unit/integration tests.
- Run the test suite locally inside the review worktree directory.
- Confirm CI pipeline checks pass cleanly.

### Step 5: Security & Performance Audit
- Audit for security vulnerabilities (e.g., input sanitization, exposed credentials, unsafe SQL/shell calls).
- Ensure no unnecessary performance bottlenecks, memory leaks, or high-complexity loops.

### Step 6: Submit Review & Cleanup Worktree
- Submit a substantive GitHub review. A chat summary is not review evidence.
  - With a GitHub account distinct from the PR owner, use `gh pr review
    --approve` when no blocking findings remain, or `--request-changes` with
    constructive inline findings when changes are required.
  - In the normal same-account fleet, GitHub rejects both verdicts as
    self-review. Use `gh pr review --comment` and state the verdict in the body.
    Put each blocking finding in an unresolved inline thread so the picker
    routes the PR back to its author.
- If the review has no blocking findings, complete its independent-agent
  attribution and release the claim with
  `python3 "$ARU_SDLC_HOME/scripts/claim_issue.py" --pr <PR_ID> --agent
  <AGENT_ID> --complete-review`. The resulting `reviewed-by:<id>` label plus the
  substantive `COMMENTED` review is the same-account approval-equivalent read
  by `merge_pr.py`.
- If blocking findings remain, release the reviewer claim with `--release`
  without adding `reviewed-by:`. The author/reviewer loop continues until every
  thread is resolved; review-round count alone is never a human gate.
- Do not move the issue directly to Done. Only the gated `merge_pr.py` close-out
  performs the merge and Done transition after independent review, green CI,
  resolved threads, and the remaining Definition-of-Done checks pass.
- Return to the primary repository root (`cd "$ARU_SDLC_HOME"` or `cd ../..`) and remove the temporary review worktree directory `.worktrees/review-pr-<PR_ID>` safely without `--force` (`git worktree remove .worktrees/review-pr-<PR_ID>`). If git refuses because untracked or modified files exist, inspect the worktree, remove only known generated build/test caches, or retain it for diagnostic recovery rather than discarding uninspected material.

---

## Review Checklist

Apply every pre-submission item before submitting the review, submit the review with clear line comments and verdict, and complete the post-submission close-out after submission. A chat summary that skips this list is not review evidence.

### Pre-Submission Checklist
- [ ] Linked issue (`Closes #N`) is open; every acceptance criterion is met or
      explicitly deferred with a follow-up issue (do not close incomplete work).
- [ ] Diff matches the claim in the PR body **and** the gate/script's actual
      behaviour (state machine, not narrative — see below).
- [ ] **Narrower-than-reality:** for every enforcement, comparison, decision, or state
      assumption in the diff, answer: *What does the underlying system or environment
      actually do or accept, and is our enforcement, comparison, or assumption narrower than that reality?*
- [ ] Tests cover the new behaviour; local suite is green in the review worktree.
- [ ] CI is green (or failures are classified and already under remediation).
- [ ] No secrets, unsafe shell interpolation, or trust-boundary holes introduced.
- [ ] Blocking findings are each prepared as an unresolved inline thread; non-blocking notes
      stay in the review body.

### Review Submission Checklist
- [ ] Submit substantive GitHub review (`gh pr review --comment` for same-account fleet, or `--approve` / `--request-changes` across distinct accounts).
- [ ] For changes requested, create unresolved inline review comment threads on specific diff lines for all blocking findings (so the picker routes the PR back to the author).
- [ ] State overall review verdict and summary in the review body.

### Post-Submission Close-Out
- [ ] Review claim released via `claim_issue.py --pr <PR_ID> --agent <AGENT_ID> --complete-review`
      (if no blocking findings remain) or `--release` (if changes requested).
- [ ] Return to repository root and clean up temporary review worktree safely without `--force` (`git worktree remove .worktrees/review-pr-<PR_ID>`), inspecting or retaining any uncertain files.

---

## Narrower-Than-Reality Heuristic

**Core principle:** Whenever code enforces rules, makes comparisons, resolves state, or relies on assumptions about the environment, ask:

> *What does the underlying system or environment actually do, accept, or produce, and is our enforcement, comparison, or assumption narrower than that reality?*

The enforcement layer's design is usually sound. What keeps breaking is the gap
between what a check or assumption *believes* and what the system *actually does*. When the
logic is narrower, it either false-passes (misses the real write / real test /
real label) or false-fails (blocks legitimate layouts or formats the runner accepts).

### Worked examples

| Check | Narrower than | Failure shape |
|---|---|---|
| Redirect / write scan | Shell quoting and expansion rules | Misses quoted/redirected writes, or treats `$TMP/out` as a repo path |
| Test-discovery glob | pytest's `python_files` defaults (`test_*.py` **and** `*_test.py`) | Fails projects whose suite the runner would collect |
| Jest test glob | Jest's `testMatch` (incl. `__tests__/`) | Same false-positive against a passing suite |
| Merge-gate label read | The labels the framework actually writes (`author:` / `reviewed-by:`) | Accepts the wrong stamp, or requires a stamp nothing applies |
| `_git_write_to_protected` | Git's real pre-subcommand options (`-C`, `--git-dir`, `--work-tree`, quoting) | Historical defect (#117, fixed in PR #119 / commit `0d1b6d2`): `git -C <main> commit` escaped or legitimate worktree commits got refused |

Recent issues in this family: #69 (hook governed the shell's cwd, not the
file), #73 (widening `touches:` had no effect until a cache expired), #76
(redirect target starting with a variable read as a repo path), #117
(protected-branch guard keyed to the shell's branch — fixed in PR #119 / commit `0d1b6d2`).

### State machine, not narrative

Standing lesson from the #24 / #25 near-miss: review the diff against the
**gate's actual behaviour**, not against the claim in the PR body. Both halves
of a two-state protocol (e.g. `reviewer:` claim vs `reviewed-by:` completion)
must be checked against the state machine the merge helper reads. A PR that
*says* it stamps completion while the script still accepts the transient claim
label is a security hole dressed as a fix.
