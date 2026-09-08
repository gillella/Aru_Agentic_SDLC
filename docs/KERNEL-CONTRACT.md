# Minimal Kernel Contract

This is the canonical operating contract for the Aru v2.0.0 source candidate. Other documentation,
skills, and generated instructions summarize or explain this file; they do not
add merge gates.

## v2 public API and compatibility

The v2 public API is the five lifecycle statuses, the issue contract,
the `touches:` write boundary, the eleven supported lifecycle commands named
below, the six installed skills, the `aru-governed-pr` check name and the
`self-hosted-mac` / `github-hosted` runner-profile contract, the path-derived risk
tiers, the `review-policy:*`, `reviewer-registered:*`, `reviewer-binding:*`,
`review:*`, and `agent:*` label contracts, and the seven operating documents.

Compatible v2.x releases may correct or extend those interfaces without
weakening their fail-closed guarantees. Removing or incompatibly changing one
requires a new major version. Private helper internals, evidence documents,
consumer verification commands, external Driver cadence, and consumer release
or deployment systems are not part of the public API.

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

Optional external Driver source may be versioned under `integrations/` while
its execution and operational receipts live in the operator's separate Hermes
home. It is not imported by kernel helpers or installed by consumer bootstrap.
The [Hermes integration](../integrations/hermes/README.md) owns immediate event
wakes, one ten-minute recovery heartbeat per enabled project, capacity locks,
and typed dependency handoffs. GitHub remains the sole lifecycle authority;
operational receipts never authorize a claim, review, merge or issue closure.

## Kernel invariant

One open issue with a valid contract moves through `Backlog`, `Ready`,
`In Progress`, `In Review`, and `Done`. It has one exclusive writer, one
declared write boundary, one isolated worktree, one exact-current-head server
verification check executed only on the repository's one assigned runner
profile, risk-tiered independent review, and one governed mechanical merge through
`scripts/merge_pr.py --expected-head`.

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
   preflight and audit evidence; the same local compute becomes merge authority
   only when GitHub Actions dispatches the exact-head governed job to the
   repository's one assigned runner profile.
6. Open a PR containing `Closes #N`. The consumer-owned `aru-governed-pr`
   workflow checks out the exact PR head on that profile's runners, runs the
   repository-defined `.aru/verify.sh`, and validates `touches:` against the
   actual diff. It has no cross-profile fallback. A GitHub merge queue reruns
   verification on its merge-group revision.
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

### Semantic authorization and external metadata races

The PR scope hook binds its final head reread to the same single parsed closing
issue directive, then rereads the issue and compares its parsed touches scope.
Benign issue prose and declaration formatting or ordering remain compatible.
Merge submission compares two full gate evaluations, including
the actual changed paths, declared touches, claimant, and each parsed acceptance
item's text and completion state. Criterion counts alone are not authorization
evidence. After CI and review reads, one bounded final PR/issue validation checks
the open, ready PR, exact head and base, closing issue, and current issue gate
against that evidence. One final head/base-bound queue snapshot refuses observed
queue configuration or pending queue-entry/auto-merge request drift before the
merge command; it does not rerun the full gates or cancel an existing request.
After those rereads, one bounded review validation uses the final PR snapshot to
require the same assigned authority and successful exact-head evidence for Tier
2-3, then rereads unresolved threads for every tier. Existing provider evidence
forms and nullable review decisions remain compatible. Invalidated evidence,
including assignment resets, refuses submission and returns to external review
convergence without repairing authority. Missing,
unreadable, invalid, or changed authorization blocks submission. Description or
verification prose outside these semantic fields may change without refusal.

Separate GitHub metadata reads and the merge API are **non-atomic**. A bounded
reread catches observed drift; it does not lock issue metadata or make the
operations a transaction. Changes after their last read can still race submission,
including queued merges. The expected-head argument binds the submitted commit,
not all external metadata. Existing head, CI, review, thread, scope, and queue
provenance gates remain required. Post-merge close-out revalidates current
authority before Done; it cannot prevent or undo a merge already submitted.

For the reported JMC #185 downstream blocker, keep review retries frozen at the
reported head until the Factory fix is independently reviewed, merged, and
released. Then use that released canonical source to regenerate the consumer's
tracked Aru integration once and reinstall hooks with
`"$ARU_SDLC_HOME/scripts/install_hooks.sh"` from the consumer checkout. Verify the
generated hook matches canonical source, run the focused semantic-drift probes
and consumer verification, and obtain one distinct current-head follow-up review.
Release publication and consumer regeneration are separate operator work; neither
is an acceptance prerequisite for this source fix. Do not patch consumer copies
independently or relax merge-group provenance to unblock regeneration.

## Review and continuation

Tier 0-1 changes do not wait for an authoritative review.
For Tier 2-3, CodeRabbit is the sole preferred external provider. Sourcery and
CodeAnt are retired: registration and historical evidence never make them
eligible for new assignments. Use one bounded authenticated check for usable
CodeRabbit access to the current repository/head. If access is denied, errored,
rate-limited, unavailable or unproven, immediately select an available distinct
coding reviewer; never wait through retired providers. Generic green checks,
cached installation inventory and empty/skipped reviews are not approval.
The optional `review-policy:timeout=<seconds>` is a completion deadline only
for an accepted review (default 900 seconds, informed by the observed 11-minute
CodeRabbit review). Explicit unavailability bypasses it. Ranked declarations
remain invalid. Use `create_pr.py --refresh-reviewer <PR>` to migrate a retired
assignment; do not hand-edit authority or erase prior findings/history.
A coding reviewer must be bound to an actor distinct from the PR author and
submit a full-current-head formal attestation. All applicable findings remain
resolved before merge. Availability, assignment and actual execution/verdict
are different observations. No available independent reviewer leaves an owned
blocked action; never manufacture approval.

This operator-requested provider-policy change is the v2 major-version
migration. Historical labels and review records remain readable. Status output
uses `aru.reviewer-status/v3`; consumers reading v2 status must adopt the new
selection/retired/capability fields with this source revision. A source merge is
not a published release or proof of installation on other hosts.

The Kernel does not wait or poll. For each pending Tier 2-3 authority
assignment, the external Driver owns exactly one continuation event:

- invoke `create_pr.py --refresh-reviewer <PR>` immediately when trusted
  evidence says the authority is unavailable or errored; otherwise
- invoke it once at the configured timeout from the current authority's latest
  governed label-assignment event if a verdict is still pending.

Before invoking, the Driver rereads the PR head and authority. It cancels stale
events after a head or authority change and stops after the bounded refresh.
The helper alone decides whether to retain or change authority and excludes
reviewers already attempted on the exact head. If substantive
coding review aborts or loses capacity, the same event invokes
`--coding-reviewer-unavailable <reason>`. A later transition receives a new
single event; the Driver does not create another lifecycle store.

Policy and external registration are repository-shared GitHub configuration;
coding identities and subscriptions remain machine-local.
`create_pr.py --reviewer-status --json` reports the effective policy, sources,
registrations, bindings, and exclusions without mutation. Optional
`--probe-reviewers` adds bounded local liveness observations. Remove retired Sourcery/CodeAnt registration definitions during operator rollout;
retain historical review/assignment records. An active CodeRabbit registration
is configuration, not capability proof. Capability is read from CodeRabbit's own
authenticated activity on the head, on either supported surface: App check runs
(`review_progress`) or legacy commit statuses (`commit_status`, creator
`coderabbitai[bot]`). A running or completed review within the deadline is
usable; queued or generic success is not yet proof; denial, error, a skip after
the review label, a stale run or a future timestamp is unavailability. No
activity yet keeps CodeRabbit eligible for initial assignment, because it cannot
run before that label, but an assignment with no authentic activity inside a
120-second window falls back to coding review instead of waiting the deadline.

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

## Runner profiles, verification budget, and trust boundary

GitHub Actions is the orchestration and check identity. Which machines supply
the compute is one **runner profile**, selected by the repository's account:

| Profile | `runs-on` | Assigned account |
| --- | --- | --- |
| `self-hosted-mac` | `[self-hosted, macOS, ARM64, aru-ci]` | `gillella` personal repositories, including Aru itself |
| `github-hosted` | `ubuntu-latest` | `Unum-Inc` repositories |

The account table is exhaustive and has no default. An account outside it
resolves to no profile, so bootstrap refuses to scaffold it and Driver
admission stays blocked rather than borrowing another account's runners. A
consumer may also declare its profile explicitly, but only to confirm its
account's assignment; a declaration that names an unknown profile, contradicts
the assignment, or belongs to an account with no assignment is refused.

The profile chooses compute and its diagnostics only. Every required Kernel
job, under either profile, keeps the `aru-governed-pr` check name, the
exact-head checkout with `persist-credentials: false`, read-only permissions,
the same `pull_request` head-repository provenance condition, `.aru/verify.sh`,
and actual-diff `touches:` enforcement.

The scaffolded workflow records its profile in a single `# aru-runner-profile:`
marker, and `.aru/verify.sh` requires exactly one marker, a known profile, and
the exact `runs-on:` value that profile mandates. There is no cross-profile
fallback in either direction: a `self-hosted-mac` repository whose Macs are all
offline leaves `aru-governed-pr` queued and merge blocked, never rerouting to
hosted runners, and a `github-hosted` repository is refused if its workflow so
much as names `self-hosted`, so hosted verification never reaches a personal
machine. Under both profiles the workflow rejects `pull_request_target`,
Actions caches, artifact uploads, `environment:`, deployment secrets, and write
permissions. Any storage-producing step is an explicit consumer policy choice.

Self-hosted runners are repository-level, maintained and patched by the
operator, and used only for trusted governed repositories; their availability,
electricity, storage, operating-system maintenance, and physical security are
operator-owned costs. GitHub publishes no comparable machine inventory for
hosted runners, so a hosted repository's verification capacity is proven from
the bounded evidence GitHub does publish — one active governed workflow plus
the queued-run depth — and is never fabricated. Under either profile, evidence
that is missing, unreadable, or malformed leaves capacity unknown and blocks
admission. Before checkout, every job rejects a cross-repository fork PR and
proves that Python 3.11+, pip, and `gh` are present.

A profile is a verification decision, not a deployment decision. Neither
profile authorizes a release, a deployment, or production access; those remain
consumer-owned and are never implied by merge.

The portable bootstrap ruleset has no configured bypass actors and requires
the `aru-governed-pr` context from the GitHub Actions App. That workflow is
scaffolded for the account's assigned profile. It does not make
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
