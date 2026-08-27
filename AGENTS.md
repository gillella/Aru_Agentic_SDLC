# Aru Minimal Kernel Rules

This repository is a rules-and-guidelines project backed by a small set of
mechanical helpers. These rules apply to Aru itself and to repositories that
adopt the kernel.

## Purpose

Move one approved GitHub issue safely to one merged pull request. Nothing in
this repository schedules agents, stores a private queue, deploys software,
tracks provider capacity, sends notifications, or owns consumer runtime.

## Authorities

- GitHub issue plus Project Board: lifecycle state.
- `touches:`: write boundary.
- Git worktree: implementation isolation.
- exact-head CI: verification authority.
- exactly one `review:<service>` label: external review authority.
- `scripts/merge_pr.py`: only merge authority.
- consumer repository: deployment and production authority.

## Required issue contract

An issue may enter Ready only when it is open and contains:

- an `## Acceptance Criteria` section with at least one unchecked item;
- one line beginning `touches:` with repository-relative paths;
- no unresolved `depends-on: #N` issue.

The five lifecycle statuses are Backlog, Ready, In Progress, In Review, and
Done. Do not create another lifecycle store.

## Eight-step lifecycle

1. File or refine the GitHub issue.
2. Validate the contract and move it to Ready.
3. Claim it before editing.
4. Create one `.worktrees/<branch>` checkout.
5. Make the smallest change and run focused tests.
6. Open a PR containing `Closes #N`; wait for current-head CI and the assigned
   external review service.
7. Resolve every finding and merge only with `merge_pr.py --expected-head`.
8. Verify Done and remove only clean, closed Factory worktrees.

## Reviewer assignment

`create_pr.py` selects one stable reviewer for the PR lifetime:

```text
[coderabbit, sourcery, codeant][issue_number mod 3]
```

No capacity inventory, distributed lock, retry rotation, coding-agent review,
or self-review is part of the kernel. An operator may manually replace the
single label after a concrete service failure and must record the reason on the
PR.

## Failure behavior

Missing, partial, stale, contradictory, or unauthenticated issue, CI, review,
identity, board, or Git data blocks the transition. During a GitHub outage,
preserve already-claimed local work and stop coordination. Never invent
fallback state.

## Scope boundary

The kernel has no scheduler, daemon, worker handoff, agent presence registry,
telemetry, Slack bridge, dashboard, release/deploy system, preview stack,
incident system, provider quota manager, or compatibility router. Optional
planning methods are not installed by this repository.

New components require evidence from three governed consumer repositories and
must explain why GitHub, Git, `gh`, a test, or a document cannot solve the
problem more simply.

## Surface budgets

- production Python plus hooks: at most 6,000 lines;
- tests: at most 9,000 lines;
- source file: at most 800 lines, with 400 preferred;
- supported command scripts: at most 14;
- runtime skills: at most 6;
- active operating documents: exactly the seven named in the kernel contract;
- persistent state stores and background processes: zero.

The v0.2.0 feature freeze lasts through 2026-09-26. During it, accept only
security and correctness fixes.
