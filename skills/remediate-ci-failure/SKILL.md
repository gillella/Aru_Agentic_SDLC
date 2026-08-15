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

#### Blocking and HITL notification

Ordinary actionable CI failures stay in the GitHub remediation loop and do
not produce Slack noise. If remediation is blocked by an unavailable
dependency/agent, a missing product decision, exhausted credentials/credits,
or a severe failure that cannot be resolved safely, use one of these complete
consumer-repository commands. First write the concise, secret-safe summary or
decision to an operator-owned `0600` file using a non-shell file-writing
mechanism, then set `ARU_ALERT_TEXT_FILE` or `ARU_ALERT_DECISION_FILE` to its
path:

```bash
python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id <PROJECT_ID> --agent <AGENT_ID> --family <FAMILY> \
  --event blocked --repo <OWNER/REPO> --pr <PR_ID> \
  --repo-dir <CONSUMER_REPO_ROOT> --text-file "$ARU_ALERT_TEXT_FILE"

python3 "$ARU_SDLC_HOME/scripts/slack_notify.py" \
  --project-id <PROJECT_ID> --agent <AGENT_ID> --family <FAMILY> \
  --event hitl --repo <OWNER/REPO> --pr <PR_ID> \
  --repo-dir <CONSUMER_REPO_ROOT> --decision-file "$ARU_ALERT_DECISION_FILE"
```

The project registry is authoritative for the repository slug and checkout
path; `--repo` and `--repo-dir` remain compatibility/documentation fields and
cannot redirect an alert.

The helper must write its durable GitHub alert comment before posting to
Slack. For HITL, state only the decision needed; the configured operator
allowlist supplies the mention. Never include prompts, diffs, tokens, test
logs, or credential values. Slack failure does not halt GitHub remediation,
but the helper's structured failure audit must remain available for retry and
recovery evidence.

### Step 3: Local Verification & Fix Commit
1. Reproduce the failure locally inside the branch worktree.
2. Apply minimal code changes targeting the exact root cause.
3. Re-run local test suite to ensure zero regressions.
4. Commit fix with conventional message: `fix(ci): resolve test assertion in auth pipeline`.
5. Push update to remote feature branch.

### Step 4: Re-poll CI Status
1. Poll CI run status until all checks pass green.
