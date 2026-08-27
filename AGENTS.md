# Aru Minimal Kernel Rules

This repository is a rules-and-guidelines project backed by a small set of
mechanical helpers. These rules apply to Aru itself and to repositories that
adopt the kernel.

## Purpose

Move one approved GitHub issue safely to one merged pull request. Nothing in
this repository schedules agents, stores a private queue or capacity ledger,
deploys software, sends notifications, or owns consumer runtime.

## Authorities

- GitHub issue plus Project Board: lifecycle state.
- `touches:`: write boundary.
- Git worktree: implementation isolation.
- exact-head CI: verification authority.
- exactly one `review:<authority>` label: external-service or independent
  coding-agent review authority for the current PR head.
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
6. Open a PR containing `Closes #N`; wait for current-head CI and the one
   assigned authoritative reviewer.
7. Resolve every finding and merge only with `merge_pr.py --expected-head`.
8. Verify Done and remove only clean, closed Factory worktrees.

## Reviewer assignment

`create_pr.py` selects the first external reviewer explicitly registered by a
`reviewer-registered:<service>` label in `coderabbit`, `sourcery`, `codeant`
order. Authority labels provisioned by bootstrap do not register providers.
Exactly one authority label remains.
An explicit unavailable/error response causes immediate fallback; a merely
pending external assignment is retained until 15 minutes after assignment, then
falls back. Cost, quota exhaustion, rate limiting, provider outage, unsupported
bot-authored PRs, and explicit unavailable/error responses are unavailable.
The newest trusted, timestamped provider evidence determines availability. A
governed authority-label transition starts a new 15-minute pending window.

Before fallback, smoke-test actual capacity in `claude-code`, `openai-codex`,
`xai-cursor`, `google-antigravity` order, excluding the author identity and
preferring a different model family. Claude selection probes all three
`claude-sub` subscriptions. A candidate also requires one explicit
`reviewer-binding:<identity>=<github-login>` registration whose actor differs
from the PR author. If no bound distinct reviewer responds exactly `OK`, keep
the existing authority and fail closed.

If an assigned coding reviewer later returns an explicit unavailable/error
state or aborts without a verdict, use `create_pr.py --refresh-reviewer <PR>
--coding-reviewer-unavailable <reason>` to audit and recover to the first
registered external authority. Do not edit authority labels by hand.

A coding agent may author or remediate code and may authoritatively review code
written by a different agent. It must never review its own PR under normal
conditions. Coding-agent authority requires one `reviewer:<identity>` label and
one substantive formal GitHub Review attestation bound to the full current-head
SHA. Any push makes the earlier verdict stale. `REQUEST_CHANGES`, an unresolved
finding, an unresolved review thread, malformed/spoofed/conflicting evidence,
or multiple authorities blocks merge.

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
