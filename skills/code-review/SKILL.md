---
name: code-review
description: Procedure for AI agents to conduct thorough, constructive code reviews of open pull requests, evaluating correctness, security, performance, test coverage, and git hygiene.
---

# Code Review Procedure

This skill defines the declarative code review procedure for evaluating Pull Requests submitted by human developers or AI agents.

---

## Review Checklist

### 1. Issue Tracing & Goal Alignment
- Verify that the PR links to an open issue (`Closes #X`).
- Confirm that the PR changes directly solve the acceptance criteria outlined in the issue.

### 2. Correctness & Architecture
- Check for subtle bugs, logic flaws, race conditions, or unhandled edge cases.
- Ensure API signatures, schema types, and data models remain consistent.

### 3. Code Hygiene & Maintainability
- Ensure variable and function names are clear and descriptive.
- Confirm docstrings and comments are preserved.
- Verify zero unused imports or dead code.

### 4. Test Coverage & Verification
- Verify that new feature logic or bug fixes are accompanied by unit/integration tests.
- Confirm CI pipeline checks pass cleanly.

### 5. Security & Performance
- Audit for security vulnerabilities (e.g., input sanitization, credentials in code).
- Ensure no unnecessary performance bottlenecks or high-complexity loops.

---

## Action Steps
1. Checkout and inspect PR branch locally if needed.
2. Review file diffs and run test suite.
3. Submit review comments or approval:
   - If changes required: Request modifications with clear rationale.
   - If approved: Approve PR and update Project Board status to `Done` upon merge.
