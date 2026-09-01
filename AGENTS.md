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
- exact-head focused local verification evidence: verification authority.
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
6. Open a PR containing `Closes #N`, record only focused local verification
   commands in the PR body, and bind them to the exact head with
   `create_pr.py --refresh-verification`; wait only for that exact-head local
   verification evidence and the one assigned authoritative reviewer.
7. Resolve every finding and merge only with `merge_pr.py --expected-head`.
8. Verify Done and remove only clean, closed Factory worktrees.

## Reviewer assignment

`create_pr.py` reads one validated repository policy from optional
`review-policy:primary=<authority>`, contiguous
`review-policy:fallback-N=<authority>`, and
`review-policy:timeout=<seconds>` label definitions. Without them, the first
registered external provider is primary, the four coding families are ordered
fallbacks, and timeout is 120 seconds. Referenced external reviewers require
`reviewer-registered:<service>`; coding identities remain machine-local and
bound. A coding slot must pass its bounded probe (which verifies liveness only);
an unavailable slot advances. The author identity and GitHub actor are never
eligible. Authority labels provisioned by bootstrap do not register providers.
Exactly one authority label remains. Inspect the effective configuration with
`create_pr.py --reviewer-status --json`; `--probe-reviewers` adds bounded local
coding probes without writing lifecycle state.
An explicit unavailable/error response causes immediate fallback; a merely
pending external assignment is retained until the configured timeout, then
becomes eligible for fallback. Because the kernel itself has no scheduler, an
external event or timer must invoke `create_pr.py --refresh-reviewer <PR>`.
Paused reviews, cost or quota exhaustion, rate limiting, provider outage,
unsupported bot-authored PRs, and explicit unavailable/error responses are
unavailable and cannot satisfy the merge gate through a nominally successful
no-op status. The newest trusted, timestamped provider evidence determines
availability. A governed authority-label transition starts a new configured
pending window. Remove `reviewer-registered:<service>` when an external service
expires or is uninstalled; registration is an operator assertion, not a live
subscription probe.

For coding assignment, smoke-test actual capacity (verifying liveness only) in
`claude-code`, `openai-codex`, `xai-cursor`, `google-antigravity` order,
excluding the author identity and preferring a different model family. Claude
selection probes every configured `claude-sub` subscription. A candidate also
requires one explicit `reviewer-binding:<identity>=<github-login>` registration
whose actor differs from the PR author. `ARU_CODING_REVIEWERS` is the local
allowlist using `family:identity` entries and
`claude-code:identity@subscription` for each Claude subscription. Only
configured identities are probed; subscription additions and removals are
configuration changes. Missing, malformed, duplicate, or unbound configuration,
or no distinct reviewer responding exactly `OK`, keeps the existing authority
and fails closed.

If an assigned coding reviewer later returns an explicit unavailable/error
state, aborts without a verdict, or hits quota during substantive execution,
use `create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`
to audit and recover immediately to the first policy-listed registered external
authority. Do not edit authority labels by hand.

A coding agent may author or remediate code and may authoritatively review code
written by a different agent. It must never review its own PR under normal
conditions. Coding-agent authority requires one `reviewer:<identity>` label and
one substantive formal GitHub Review attestation bound to the full current-head
SHA. Any push makes the earlier verdict stale. `REQUEST_CHANGES`, an unresolved
finding, an unresolved review thread, malformed/spoofed/conflicting evidence,
or multiple authorities blocks merge.

## Failure behavior

Missing, partial, stale, contradictory, or unauthenticated issue, pull-request
verification evidence, review, identity, board, or Git data blocks the
transition. During a GitHub outage, preserve already-claimed local work and
stop coordination. Never invent fallback state.

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
