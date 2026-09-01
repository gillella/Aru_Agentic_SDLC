# Changelog

## Unreleased

- Made `fetch_next_work.py` batch selection reserve touches from every open
  governed PR across all authors globally and fail closed on unreadable,
  missing, or ambiguous reservations.
- Made `fetch_next_work.py` return an actionable conflict remediation work
  result carrying PR number, head, and reason when an authored open PR has
  `mergeStateStatus: DIRTY`.
- Made `fetch_next_work.py` obtain and validate live current-head
  `mergeStateStatus` via a narrow repository read when active open PR records
  lack merge state, classifying DIRTY conflicts before evaluating CI failure
  while preserving snapshot-based reservations.
- Updated `address-pr-feedback` and operational docs to instruct merging
  `origin/main` into the feature branch (never rebase/force push), rerunning
  focused verification, and refreshing exact-head evidence on DIRTY conflicts.
- Clarified that the kernel has no scheduler so external reviewer 15-minute
  fallback requires an external event/timer invoking `create_pr.py --refresh-reviewer`,
  coding reviewer probes test liveness only, and substantive review execution
  hitting quota triggers immediate governed unavailable recovery.
- Removed the repository workflow file and stopped scaffolding one for adopted
  repositories.
- Replaced the repository-hosted merge gate with exact-head focused local
  verification evidence stored in the PR body and refreshed through
  `create_pr.py --refresh-verification`.
- Made `check_ci.py` and `merge_pr.py` fail closed on missing, stale,
  malformed, no-execution, or broad/full-suite verification evidence.
- Updated active operating docs, templates, and runtime skills to require
  exact-head focused local verification with honest project-specific commands
  and to reserve any full suite for separate explicit release activity.

## v0.2.2 - Consumer Adoption Ready - 2026-08-27

- Expanded the README into a user-friendly adoption entry point.
- Replaced the terse operations note with a complete graphical developer guide
  covering prerequisites, new and existing repository adoption, GitHub setup,
  the full lifecycle, every supported command, recovery, troubleshooting, and
  a pilot checklist.
- Documented the operator-owned setup that the kernel intentionally does not
  automate, including initial publication, Project item admission, real CI,
  branch protection, and external reviewer installation.
- Reconciled every existing Project Board item to Done and archived the final
  legacy branch heads before removing their obsolete worktrees and branches.
- Marked the minimal kernel complete and ready for consumer-project pilots.

## v0.2.1 - Reset Correctness Fixes - 2026-08-27

- Scoped surface-budget checks to tracked kernel files so preserved external
  worktrees and untracked operator files do not create false failures.
- Replaced legacy Aru pre-push hooks during installation without chaining the
  retired lifecycle implementation.

## v0.2.0 - Minimal Kernel

- Reset Aru to one issue-to-safe-merge lifecycle.
- Removed scheduler, loop, presence, handoff, telemetry, review-capacity,
  reassignment-lock, release, deploy, preview, incident, Slack, visualizer,
  compatibility, and historical factory-product surfaces.
- Replaced the oversized core with small fail-closed commands.
- Reduced active documentation to seven operating files and runtime skills to
  six.
- Made reviewer assignment stable and deterministic without an open-PR
  inventory or distributed lock.
- Added hard surface-budget and forbidden-surface tests.
- Preserved the pre-reset repository at
  `pre-v0.2.0-2026-08-27` (commit
  `388a22b3183e523ee67f857979448d2124e1a854`).

Feature development is frozen through 2026-09-26; only security and correctness
fixes are admitted during the freeze.

## Historical releases

The full pre-v0.2 history and release notes remain available in Git and GitHub.
