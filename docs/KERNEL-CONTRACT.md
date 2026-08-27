# Minimal Kernel Contract

## Purpose

Aru authorizes and blocks the path from one approved GitHub issue to one safely
merged pull request.

## Lifecycle

Requirement -> issue contract -> Ready -> claim -> isolated worktree -> focused
implementation -> PR -> exact-head CI and one external review -> mechanical
merge -> Done and cleanup.

## Authority map

| Decision | Authority |
| --- | --- |
| work exists and is approved | GitHub issue and Project Board |
| who may write | exclusive claim plus `touches:` |
| where work occurs | isolated Git worktree |
| code is verified | exact-current-head CI |
| code is reviewed | one `review:<service>` authority |
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
telemetry, provider-capacity routing, notification bridge, dashboard, preview,
deployment, release, smoke, incident, or second state store.

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
