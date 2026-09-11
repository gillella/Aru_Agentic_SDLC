# Minimal Kernel Contract

This is the canonical operating contract for the released Aru v2.0.0. Other documentation,
skills, and generated instructions summarize or explain this file; they do not
add merge gates.

## Public API and compatibility

The public API is the five lifecycle statuses, the issue contract,
the `touches:` write boundary, the eleven supported lifecycle commands named
below, the six installed skills, the `aru-governed-pr` check name, the optional
`aru-merge-authorized` check with its `ARU_MERGE_APP_RUNNER` / `ARU_MERGE_APP_ID`
configuration, the
`self-hosted-mac` / `github-hosted` runner-profile contract, the one-approval
review rule, the `agent:*` label contract, and the seven operating documents.

It differs from the v2 public API only in review. The one-approval rule replaced
the path-derived review tiers, the `review-policy:*`, `reviewer-registered:*`,
`reviewer-binding:*` and `review:*` label contracts, and the reviewer options of
`create_pr.py`. That removal is a major-version change (v3, not yet released).

Compatible v2.x releases may correct or extend those interfaces without
weakening their fail-closed guarantees. Removing or incompatibly changing one
requires a new major version. Private helper internals, evidence documents,
consumer verification commands, external Driver cadence, and consumer release
or deployment systems are not part of the public API.

The v2.0.0 migration withdrew previously declared merge-queue support.
The workflow verifies same-repository PR heads only; it cannot prove a combined
queue revision and all constituent issue scopes. The helper therefore refuses
configured queues or pending queue/auto-merge requests before submission. It
requires explicit queue-state fields; missing evidence never means disabled.
No existing queue is canceled or reconfigured by this correction.

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
profile, one approval of that head from another GitHub account, and one
governed mechanical merge through `scripts/merge_pr.py --expected-head`.

The issue contract is:

- an `## Acceptance Criteria` section with an unchecked item;
- exactly one safe `touches:` declaration of repository-relative paths;
- no unresolved `depends-on: #N` issue.

Promotion pins that contract: `triage_backlog.py` labels the issue
`ready:<digest>`, a digest of the issue number, the criteria text and the
`touches:` paths. Ticking criteria does not change it. The merge gate recomputes
the digest from the live issue and refuses on a mismatch or on more than one pin,
so a claimant cannot widen its own scope or reword its criteria mid-flight; that
needs a return to Backlog and a fresh promotion. Issues promoted before pinning
carry no pin and are checked as before; removing a pin is hand-editing lifecycle
labels, which this contract forbids. Close-out deletes the pin label.

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
   actual diff. It has no cross-profile fallback and accepts only verified
   same-repository `pull_request` events. Merge-group verification is unsupported.
7. Require that exact-head server check, resolve every current-head finding, and
   obtain one approval of the exact head from a GitHub account other than the
   author. A push invalidates earlier check and approval evidence.
8. Submit a direct merge with `merge_pr.py --expected-head`. Queue configuration
   or a pending queue/auto-merge request blocks admission. With the merge-authority
   App configured, the helper posts `aru-merge-authorized` at the exact head after
   its final revalidation and supersedes it with a failure if submission fails.
   Close out only after
   GitHub confirms the exact PR head merged and current authority still passes.
   `--finalize` recovers confirmed direct merges; a bounded, exact-head and
   merge-commit-bound history read refuses any historical queue entry because
   PR-head checks alone cannot prove its combined revision. Then verify Done
   and remove only clean, closed Factory worktrees.

Missing, partial, stale, contradictory, truncated, or unauthenticated evidence
blocks the next transition. Never repair authority by hand-editing lifecycle
labels.

### Semantic authorization and external metadata races

The PR scope hook binds its final head reread to the same single parsed closing
issue directive, then rereads the issue and compares its parsed touches scope.
Benign issue prose and declaration formatting or ordering remain compatible.
Merge submission compares two full gate evaluations, including
the actual changed paths, declared touches, claimant, linked Project card
identity/status, resolved dependency identities/states, and each parsed acceptance
item's text and completion state. Criterion counts alone are not authorization
evidence. After CI and review reads, one bounded final PR/issue validation checks
the open, ready PR, exact head and base, closing issue, and current issue gate
against that evidence. One final head/base-bound queue snapshot refuses observed
queue configuration or pending queue-entry/auto-merge request drift before the
merge command; it does not rerun the full gates or cancel an existing request.
After those rereads, one bounded review validation rereads the reviews and
unresolved threads and again requires a non-author approval of the exact head;
a withdrawn, dismissed or superseded approval refuses submission. Missing,
unreadable, invalid, or changed authorization blocks submission. Description or
verification prose outside these semantic fields may change without refusal.

Separate GitHub metadata reads and the merge API are **non-atomic**. A bounded
reread catches observed drift; it does not lock issue metadata or make the
operations a transaction. Changes after their last read can still race submission,
including queue configuration changes. The expected-head argument binds the submitted commit,
not all external metadata. Existing head, CI, review, thread, scope, and queue-state
provenance gates remain required. Post-merge close-out revalidates current
authority before Done; it cannot prevent or undo a merge already submitted.

For the reported JMC #185 downstream blocker, keep review retries frozen at the
reported head until the Factory fix is independently reviewed, merged, and
released. Then use that released canonical source to regenerate the consumer's
tracked Aru integration once and reinstall hooks with
`"$ARU_SDLC_HOME/scripts/install_hooks.sh"` from the consumer checkout. Verify the
generated hook matches canonical source, run the focused semantic-drift probes
and consumer verification, and obtain one current-head approval from another account.
Release publication and consumer regeneration are separate operator work; neither
is an acceptance prerequisite for this source fix. Do not patch consumer copies
independently or relax merge-group provenance to unblock regeneration.

## Review

Every pull request, whatever it changes, needs one approval of its exact
current head from a GitHub account other than the PR author. Any reviewer
counts: a person, CodeRabbit, or a coding agent working under its own account.
The Kernel does not assign, rank, probe, time out or replace reviewers, and no
label records review state.

GitHub enforces the rule. The bootstrap ruleset requires one approving review,
dismisses stale approvals on push, and requires the most recent push to be
approved by someone other than its pusher. `merge_pr.py` reads the same
evidence and refuses unless some account other than the author (a GitHub App
author `app/<slug>` and its `<slug>[bot]` login are one account) has, as its
latest decisive review (`APPROVED`, `CHANGES_REQUESTED` or `DISMISSED`),
approved the exact head. Comments change nothing, and an approval of an earlier
commit does not carry forward. A `CHANGES_REQUESTED` review decision or any
unresolved thread still blocks. Agents that share one GitHub account cannot
approve each other's pull requests; give authors and reviewers different
accounts.

The writer owns remediation. A reviewer who pushes a fix becomes the last
pusher, so someone else must approve. All applicable findings need a recorded
code fix, evidence-backed disagreement, advisory-only decision or accepted
tracked follow-up; the feedback skill defines these dispositions. A real
security/correctness defect remains blocking regardless of severity wording; a
follow-up issue cannot waive it. Advisory-only disposition can complete without
unrelated code, new tests or another push. A push still requires fresh
exact-head CI and a fresh approval. Resolving a thread must reflect its
substance, not just its latest bot reply or an outdated marker.

The Kernel does not wait or poll for review. `fetch_next_work.py` reports an
open pull request with a successful check and no qualifying approval as
`review` work with `next_action: review-by-another-account`; the author cannot
do it. An external Driver may surface it or start a reviewer under another
account, but it owns no lifecycle state.

This replaced the path-tiered, provider-routed review of v2. The Kernel no
longer reads or writes `review:*`, `reviewer:*`, `reviewer-actor:*`,
`reviewer-binding:*`, `reviewer-registered:*`, `review-policy:*`, `author:*`,
`author-family:*` or `needs-reviewer` labels, and `create_pr.py` has no reviewer
options. Historical labels and reviews stay on GitHub but authorize nothing. An
existing consumer must add the approval rule to its default-branch ruleset
before GitHub enforces it; until then only `merge_pr.py` does.

## Risk-proportional consumer policy

The Kernel's review rule does not vary by path. `scripts/review_risk.py` still
derives the highest applicable tier from changed paths (an unrecognized but
safe path fails upward to Tier 2; empty, malformed, or unsafe evidence to Tier
3), but no Kernel gate reads it. An external Driver may use it for capacity
admission, and consumers may use it to scale their own evidence:

| Tier | Typical scope | Consumer-owned evidence |
| --- | --- | --- |
| **0 — docs** | Markdown, text, and documentation only | Small documentation checks in `.aru/verify.sh` |
| **1 — ordinary code** | Ordinary source and tests | Focused affected build, lint, and tests |
| **2 — sensitive/contract** | Agent rules, skills, workflows, `.aru/`, hooks, Kernel gate scripts, auth, security, migrations, dependencies, configuration, trading, payments, infrastructure, or unrecognized safe paths | Targeted integration, migration, compatibility, or security evidence |
| **3 — production/destructive** | Deploy, production, destructive, rollback, or revert paths; empty, malformed, or unsafe paths | Human/domain approval, broader release evidence, rollback rehearsal, staged deployment, and observability |

All `scripts/` paths are conservatively classified as sensitive control surfaces.
Documentation-name exceptions apply only to documentation; executable files such
as `README.py` retain a code tier. Production/destructive matches still take precedence.

The Kernel neither requests nor checks the consumer-owned column. Record extra
evidence in the consumer issue, `.aru/verify.sh`, branch rules, or runbook.
Consumers may add stricter parallel checks, approvals, or deployment controls;
the Kernel requires exactly one approval and no serial review rounds.
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

New scaffolds require executable `.aru/verify-project.sh` for consumer product
checks; its generated starter fails until the consumer replaces it. The framework
verifier runs those checks before reporting success. Existing consumers preserve
their verification policy while reconciling this split. Aru itself verifies its
Kernel and integrations directly in its repository workflow; it does not use the
consumer scaffold as its own application test suite.

A profile is a verification decision, not a deployment decision. Neither
profile authorizes a release, a deployment, or production access; those remain
consumer-owned and are never implied by merge.

The portable bootstrap ruleset has no configured bypass actors, allows merge
commits only (the helper never squashes or rebases), requires one approving
review of the last push with stale approvals dismissed, and requires the
`aru-governed-pr` context from the GitHub Actions App. That workflow is
scaffolded for the account's assigned profile. The ruleset does not pin the
repository-owned workflow outside the repository.

Helper-only merge is a Kernel process rule unless the optional merge-authority
App is configured. With `ARU_MERGE_APP_RUNNER` (an executable with the
`<runner> --repo OWNER/REPO -- gh ...` interface) and `ARU_MERGE_APP_ID` both
set, `merge_pr.py` posts `aru-merge-authorized` as that App once every gate
passes, and the ruleset requires that context pinned to the App's
`integration_id`, so no personal token or Actions job can satisfy it. Exactly one
of the two variables set is a misconfiguration and refuses. While the gate is on,
the helper treats `BLOCKED` as admissible because its own required check is
absent until it posts it; GitHub still refuses the submission if anything else
blocks. Bootstrap adds the rule only when the App can already act on the new
repository, because a required check nothing can post deadlocks the first pull
request. Existing repositories opt in by adding the pinned context to their
ruleset after installing the App.

The App must be distinct from any App that agents use for ordinary repository
commands, and it needs only the Checks write permission. The gate stops an
agent from finishing a merge with `gh pr merge`; it does not stop whoever holds
the App's key from posting the check directly, or an administrator from editing
the ruleset. Consumers needing more own plan-appropriate GitHub controls such as
a pinned required workflow or team/file-pattern review rules.

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

Source files are limited to 800 lines; supported scripts to 12-14; runtime
skills to six; and active operating documents to `README.md`, `AGENTS.md`,
`CHANGELOG.md`, `docs/KERNEL-CONTRACT.md`, `docs/ENFORCEMENT-REGISTER.md`,
`docs/OPERATIONS.md`, and `docs/DEGRADED-MODE.md`.

Aggregate line volume is deliberately unbudgeted, for Kernel production code and
tests alike. The former 6,500-line production ceiling was reached in September
2026 and then blocked every small fix until working code was deleted; a total
ceiling on tests likewise measured coverage as debt. The separately bounded
Hermes integration keeps its 6,000 production line limit. The 800-line per-file
cap still applies to every file, tests included, and tests must not be deleted or
compressed to hide a growing safety surface.
