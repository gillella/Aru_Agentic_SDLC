---
name: remediate-ci-failure
description: Diagnoses failing GitHub Actions or CI runs from full logs, classifies the failure, applies a minimal fix in the worktree, and re-polls until green. Use when the user says fix CI failure, remediate broken build, debug CI run, or fix failing tests on PR.
triggers:
  - "fix CI failure"
  - "remediate broken build"
  - "debug CI run"
  - "fix failing tests on PR"
do_not_trigger_for:
  - "reviewing a clean PR (use code-review instead)"
  - "implementing a new feature (use implement-next-issue instead)"
---

# Remediate CI Failure Procedure

This skill provides a structured failure diagnosis protocol for inspecting and fixing CI pipeline failures without swallowing errors or applying superficial patches.

---

## Remediation Workflow

### Step 1: Fetch Un-truncated CI Failure Logs
1. Execute `python3 "$ARU_SDLC_HOME/scripts/check_ci.py" --pr <PR_ID>` to retrieve failing check run details.
2. Fetch the full, un-truncated build log from the failed step using `gh run view --log-failed`.
3. Do NOT hypothesize root cause without empirical log evidence.

### Step 2: Classify Failure Category

| Failure Category | Log Signature | Remediation Protocol |
|---|---|---|
| **Syntax / Linter Error** | `ESLint error`, `flake8 error`, `Prettier error` | Run project formatter/linter locally and fix formatting/syntax issues. |
| **Unit / Integration Test Failure** | `AssertionError`, `FAIL: test_...`, `Expected X received Y` | Trace the broken assertion to the specific line of code. Fix underlying contract logic. NEVER delete or comment out failing assertions! |
| **Type Compiler Error** | `TypeScript error TS...`, `mypy error`, `compile error` | Correct variable/function type annotations to match schema definitions. |
| **Missing Dependency / Env** | `ModuleNotFoundError`, `Command not found`, `Missing secret` | Update build manifest / package dependencies or notify maintainer if environment secret is missing. |

If remediation is blocked by an unavailable dependency, a missing product
decision, exhausted credentials, or a severe failure that cannot be resolved
safely, record a concise secret-safe blocker on the PR and stop. GitHub remains
the durable coordination record; do not create another queue or local ledger.

### Step 3: Local Verification & Fix Commit
1. Reproduce the failure locally inside the branch worktree.
2. Apply minimal code changes targeting the exact root cause.
3. Re-run local test suite to ensure zero regressions.
4. Commit fix with conventional message: `fix(ci): resolve test assertion in auth pipeline`.
5. Push update to remote feature branch.

### Step 4: Re-poll CI Status
1. Poll CI run status until all checks pass green.
