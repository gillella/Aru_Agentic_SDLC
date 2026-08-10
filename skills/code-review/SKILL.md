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

### Step 4: Test Coverage & Verification
- Verify that new feature logic or bug fixes are accompanied by unit/integration tests.
- Run the test suite locally inside the review worktree directory.
- Confirm CI pipeline checks pass cleanly.

### Step 5: Security & Performance Audit
- Audit for security vulnerabilities (e.g., input sanitization, exposed credentials, unsafe SQL/shell calls).
- Ensure no unnecessary performance bottlenecks, memory leaks, or high-complexity loops.

### Step 6: Submit Review & Cleanup Worktree
- Submit structured review comments:
  - **Approve**: If code meets all standards, approve PR and transition status to `In Review` / `Done`.
  - **Request Changes**: Leave constructive, actionable feedback referencing specific line numbers.
- Remove temporary review worktree directory `.worktrees/review-pr-<PR_ID>`.
