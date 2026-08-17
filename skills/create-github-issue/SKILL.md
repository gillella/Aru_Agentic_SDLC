---
name: create-github-issue
description: Creates structured GitHub issues with depends-on, touches, parallel-eligible metadata and board placement under Aru_Agentic_SDLC. Use when the user says create issue, file a bug, create feature request, or add task to backlog.
triggers:
  - "create issue"
  - "file a bug"
  - "create feature request"
  - "add task to backlog"
do_not_trigger_for:
  - "implementing an issue (use implement-next-issue instead)"
  - "opening a pull request (use create_pr script instead)"
---

# Create GitHub Issue Procedure

This skill defines the declarative workflow for creating clear, actionable GitHub issues.

---

## Procedure Steps

### 1. Identify Requirement & Scope
- Determine issue type: `feature`, `bug`, or `task`.
- Define clear summary, background context, and explicit machine-checkable criteria:
  - **Acceptance Criteria / Predicates**: Machine-checkable assertions with `verify:` commands where possible.
  - **Decision Boundaries**: Explicit defaults, edge cases, error paths, and thresholds.
  - **Non-Goals**: Stated boundaries to prevent agent improvisation.

### 2. Specify Dependencies & Parallel Eligibility
- Identify if the issue depends on prior issues being completed (`depends-on: #X`).
- Declare every path the work may modify (`touches: src/**, tests/**`).
- Mark whether the issue can be implemented independently in parallel (`parallel-eligible: true`).

### 3. Format & Submit Issue
- Apply appropriate title prefixes (`feat: `, `fix: `, `chore: `).
- Use structured Markdown issue templates from `.github/ISSUE_TEMPLATE/`.
- Submit the issue and record the issue number from the created URL.
- Immediately attach it to the governed project and assign its initial board
  status (`Backlog` / `Ready`) with:
  `python3 "$ARU_SDLC_HOME/scripts/update_issue_status.py" --issue <ID> --status "<STATUS>" --require-board`
  The helper resolves the exact `<repo> Board`, or the sole project linked to
  the repository; never hardcode a project number.
- Board attachment is idempotent. Re-running the command for an already-added
  issue only updates its status.
- If attachment fails, the issue still exists. Treat the warning as incomplete
  coordination and run the printed `gh project item-add` manual remedy before
  considering the issue ready for pickup.

### Slack epic splits

When an epic is sliced in Slack, do **not** treat the thread or a thumbs-up as
a filed issue. Write a mode-`0600` JSON split document (no secrets) and run:

```
python3 "$ARU_SDLC_HOME/scripts/slack_control_room.py" file-split \
  --epic <N> --from-file <split.json> --repo-dir "$PWD"
```

The helper emits `create-github-issue` bodies (`depends-on` for sibling or
prerequisite issues — never the still-open parent epic — `touches`,
`parallel-eligible`, `Epic: #<epic>`). Ready requires the same contract as
triage. Use `--dry-run` first. Then pick work only through
`fetch_next_work.py`. Pass `--thread-ts` to reply in the Slack thread.

### 4. Production signals use intake, not a second process
Do not file production alerts by hand through this skill. Monitoring, health
checks, and error-rate thresholds invoke:

```bash
python3 "$ARU_SDLC_HOME/scripts/incident_intake.py" \
  --signal <firing|resolved> \
  --source <monitor> \
  --component <component> \
  --alert-name <alert> \
  --severity <p0|p1|p2|p3> \
  --evidence "<summary>" \
  [--evidence-file <path>] \
  [--dry-run]
```

Intake opens a skill-shaped issue on the same Project Board (`Backlog`, labels
`type:fix`, `origin:incident`, `priority:<severity>`). Recurring alerts comment
on that open issue. Resolved alerts comment on claimed or in-flight issues,
close unclaimed Backlog/Ready issues, and prune leftover `issue-<N>` worktrees
when Done — only clean worktrees, never with `--force`. `--severity` applies
only to firing signals. `--dry-run` validates identity without writing to
GitHub. `--evidence-file` reads a bounded diagnostic excerpt from a file.
Triage replaces `touches: pending-ops-triage` before promoting to Ready. There
is no second incident process. Post-merge janitor cleanup for arbitrary
leftovers is issue #126, not this intake door.
