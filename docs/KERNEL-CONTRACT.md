# Minimal Kernel Contract

This is the canonical operating contract for the current Aru v0.2 line. The
latest release is v0.2.8; changes after that tag remain listed under Unreleased
in `CHANGELOG.md`. Other documentation, skills, and generated instructions
summarize or explain this file; they do not add merge gates.

## Three layers

| Layer | Responsibility | State authority |
| --- | --- | --- |
| **Kernel** | Authorize and block one approved issue through one safe merge | GitHub issue, linked Project Board, Git, and PR evidence |
| **External Driver** | Decide when to invoke the next bounded kernel command | None; it must reread GitHub and Git on every activation |
| **Consumer policy** | Product scope, additional risk controls, engineering checks, release, deploy, and production | The consumer repository and its operators |

The Kernel is this repository. A Driver may be a human, Hermes, a scheduled
task, or another event-driven agent outside this repository. Consumer policy
may be stricter than the Kernel for a particular product, but it must not be
presented as a universal Aru requirement.

## Kernel invariant

One open issue with a valid contract moves through `Backlog`, `Ready`,
`In Progress`, `In Review`, and `Done`. It has one exclusive writer, one
declared write boundary, one isolated worktree, one exact-current-head server
verification check, risk-tiered independent review, and one governed mechanical
merge through `scripts/merge_pr.py --expected-head`.

The issue contract is:

- an `## Acceptance Criteria` section with an unchecked item;
- exactly one safe `touches:` declaration of repository-relative paths;
- no unresolved `depends-on: #N` issue.

## Required lifecycle

1. File or refine the issue in Backlog.
2. Validate it with `triage_backlog.py` and move it to Ready.
3. Claim it before editing.
4. Create and use one `.worktrees/<branch>` checkout.
5. Make the smallest change inside `touches:`. Local focused checks are useful
   preflight and audit evidence, but are not the merge authority.
6. Open a PR containing `Closes #N`. The consumer-owned `aru-governed-pr`
   workflow checks out the exact PR head, runs the repository-defined
   `.aru/verify.sh`, and validates `touches:` against the actual diff. A GitHub
   merge queue reruns verification on its merge-group revision.
7. Require that exact-head server check and resolve every current-head finding.
   Tier 2-3 changes also require one assigned authoritative reviewer distinct
   from the author. A push invalidates earlier check and review evidence.
8. Submit with `merge_pr.py --expected-head`. When GitHub uses a merge queue,
   submission is not completion: close out only after its merge-group check and
   GitHub confirmation show the exact PR head merged. Then verify Done and
   remove only clean, closed Factory worktrees.

Missing, partial, stale, contradictory, truncated, or unauthenticated evidence
blocks the next transition. Never repair authority by hand-editing lifecycle or
review labels.

## Review and continuation

Tier 0-1 changes do not wait for an authoritative review. Tier 2-3 changes use
one authority selected from registered external providers or locally
configured, bound coding identities. Selection order is an implementation
detail, not a fairness, speed, or capacity guarantee. Bootstrap `review:*`
labels do not register a provider. A coding reviewer must be bound to a GitHub
actor distinct from the PR author and must submit the required
full-current-head formal attestation. For a review-required change, self-review,
stale verdicts, unresolved findings or threads, `REQUEST_CHANGES`, no-op
provider results, and zero or multiple authorities block merge.

The Kernel does not wait or poll. For each pending Tier 2-3 authority
assignment, the external Driver owns exactly one continuation event:

- invoke `create_pr.py --refresh-reviewer <PR>` immediately when trusted
  evidence says the authority is unavailable or errored; otherwise
- invoke it once at 15 minutes from the current authority's latest governed
  label-assignment event if a verdict is still pending.

Before invoking, the Driver rereads the PR head and authority. It cancels stale
events after a head or authority change and stops after the bounded refresh.
The helper alone decides whether to retain or change authority. If substantive
coding review aborts or loses capacity, the same event invokes
`--coding-reviewer-unavailable <reason>`. A later transition receives a new
single event; the Driver does not create another lifecycle store.

## Risk-proportional consumer policy

The Kernel derives the highest applicable tier from the actual changed paths.
An unrecognized but safe path fails upward to Tier 2; empty, malformed, or
unsafe path evidence fails upward to Tier 3. These are not new lifecycle
statuses or labels:

| Tier | Typical scope | Kernel review requirement | Consumer-owned evidence |
| --- | --- | --- | --- |
| **0 — docs** | Markdown, text, and documentation only | No authoritative review | Small documentation checks in `.aru/verify.sh` |
| **1 — ordinary code** | Ordinary source and tests | No authoritative review | Focused affected build, lint, and tests |
| **2 — sensitive/contract** | Agent rules, skills, workflows, `.aru/`, hooks, Kernel gate scripts, auth, security, migrations, dependencies, configuration, trading, payments, infrastructure, or unrecognized safe paths | One distinct authoritative review | Targeted integration, migration, compatibility, or security evidence |
| **3 — production/destructive** | Deploy, production, destructive, rollback, or revert paths; empty, malformed, or unsafe paths | One distinct authoritative review | Human/domain approval, broader release evidence, rollback rehearsal, staged deployment, and observability |

Record extra evidence in the consumer issue, `.aru/verify.sh`, branch rules, or
runbook. Consumers may add stricter parallel checks, approvals, or deployment
controls, but they do not rewrite the Kernel's path-derived tier. The Kernel
requires at most one authoritative review and no serial review rounds.
Deployment is never implied by merge.

The portable bootstrap ruleset has no configured bypass actors and requires
the `aru-governed-pr` context from the GitHub Actions App. It does not make
`merge_pr.py` the only technically possible GitHub merge path, condition
server-side review on a path tier, or pin the repository-owned workflow outside
the repository. Helper-only merge is a Kernel process rule. Consumers needing
a stronger security boundary own plan-appropriate GitHub controls such as a
pinned required workflow, dedicated merge App identity, or team/file-pattern
review rules.

## Supported surface and non-goals

Supported lifecycle commands are `init_project.py`, `triage_backlog.py`,
`fetch_next_work.py`, `claim_issue.py`,
`create_branch.py`, `create_pr.py`, `check_ci.py`, `fetch_pr_feedback.py`,
`merge_pr.py`, `cleanup_worktrees.py`, and `revert_merge.py`. Installation is
provided by `install_agent_integration.sh` and `install_hooks.sh`.

The Kernel contains no scheduler, daemon, private queue, presence registry,
worker handoff, telemetry, persistent capacity store, notification bridge,
dashboard, preview, deployment, release, incident, or second lifecycle store.

A component enters the Kernel only when it directly authorizes or blocks a
lifecycle transition, no simpler GitHub/Git primitive solves it, the default
path uses it now, and evidence from three governed consumers shows the need.

Production Python and hooks are limited to 6,000 lines; tests to 9,000; source
files to 800 lines; supported scripts to 12-14; runtime skills to six; and
active operating documents to `README.md`, `AGENTS.md`, `CHANGELOG.md`,
`docs/KERNEL-CONTRACT.md`, `docs/ENFORCEMENT-REGISTER.md`,
`docs/OPERATIONS.md`, and `docs/DEGRADED-MODE.md`.
