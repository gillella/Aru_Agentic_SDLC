# Minimal Kernel Contract

## Purpose

Aru authorizes and blocks the path from one approved GitHub issue to one safely
merged pull request.

## Lifecycle

Requirement -> issue contract -> Ready -> claim -> isolated worktree -> focused
implementation -> PR -> exact-head CI and one authoritative review ->
mechanical merge -> Done and cleanup.

## Authority map

| Decision | Authority |
| --- | --- |
| work exists and is approved | GitHub issue and Project Board |
| who may write | exclusive claim plus `touches:` |
| where work occurs | isolated Git worktree |
| code is verified | exact-current-head CI |
| code is reviewed | one `review:<authority>` for the exact current head |
| code may merge | `scripts/merge_pr.py` |
| code may deploy | consumer repository |
| who schedules work | Hermes or a human, outside this repository |

## Supported commands

`init_project.py`, `update_issue_status.py`, `triage_backlog.py`,
`fetch_next_work.py`, `claim_issue.py`, `create_branch.py`,
`create_pr.py`, `check_ci.py`, `fetch_pr_feedback.py`, `merge_pr.py`,
`cleanup_worktrees.py`, and `revert_merge.py`. Installation is provided by
`install_agent_integration.sh` and `install_hooks.sh`.

## Non-goals

No scheduler, daemon, private queue, presence registry, worker handoff,
telemetry, persistent capacity registry, notification bridge, dashboard,
preview, deployment, release, smoke, incident, or second state store.

## Reviewer state machine

External authorities are tried in CodeRabbit, Sourcery, CodeAnt order. Only an
installed provider explicitly registered by `reviewer-registered:<service>` is
a candidate; bootstrap authority labels do not register providers. The first
registered available service receives the only `review:*` label. Explicit
unavailability falls back immediately; pending external work retains authority
for less than 15 minutes and falls back at 15 minutes. Unavailability includes
cost or quota exhaustion, rate limiting, provider outage, unsupported
bot-authored PRs, and explicit unavailable/error responses.

Fallback probes actual capacity in Claude Code, OpenAI Codex, xAI Cursor,
Google Antigravity order. Every candidate must have one explicit
`reviewer-binding:<identity>=<github-login>` whose authenticated actor differs
from the PR author. All three Claude subscriptions are tested. The author
identity is excluded and another model family is preferred. No successful,
bound, distinct probe leaves the prior authority unchanged and blocks progress.
An explicitly unavailable or aborted assigned coding reviewer may recover to
the first registered external authority only through the audited refresh
command; hand-editing authority labels is not a state transition.

A coding agent may author or remediate code and may review a different agent's
code. It may not authoritatively review its own PR under normal conditions. Its
formal GitHub Review must use the assigned reviewer identity, originate from a
GitHub actor distinct from the PR author, contain a substantive verdict and
focused verification, confirm the issue/acceptance criteria and exact diff plus
surrounding code were read, record severity and `file:line` for findings, and
bind the full current-head SHA. A push invalidates prior authority immediately.
Malformed, missing, duplicate, conflicting, spoofed, or stale evidence,
`REQUEST_CHANGES`, unresolved findings or threads, or multiple authorities
blocks `merge_pr.py`.

## Admission rule

A component enters the kernel only when it directly authorizes or blocks a
lifecycle transition, no simpler GitHub/Git primitive solves it, the default
path uses it now, and it is independently explainable and testable. New
capabilities also require repeated evidence from three consumer repositories.

## Budgets

Production Python and hooks <=6,000 lines; tests <=9,000; no source file above
800 lines; 12-14 supported scripts; at most six runtime skills; these seven
active documents only: `README.md`, `AGENTS.md`,
`docs/KERNEL-CONTRACT.md`, `docs/ENFORCEMENT-REGISTER.md`,
`docs/OPERATIONS.md`, `docs/DEGRADED-MODE.md`, and `CHANGELOG.md`.
