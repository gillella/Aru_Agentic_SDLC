# Minimal Kernel Contract

The canonical operating contract. Every rule here blocks or authorizes a
lifecycle transition. Other documentation summarizes it and adds no gate.
Rationale lives in `docs/decisions/`; version history lives in `CHANGELOG.md`.

## Public API and compatibility

The v2 public API is the five lifecycle statuses, the issue contract, the
`touches:` write boundary, the eleven supported lifecycle commands, the six
installed skills, the `aru-governed-pr` check name, the optional
`aru-merge-authorized` check with its `ARU_MERGE_APP_RUNNER` / `ARU_MERGE_APP_ID`
configuration, the `self-hosted-mac` / `github-hosted` runner-profile contract,
the one-approval review rule, the `agent:*` label contract, and the seven
operating documents.

Compatible releases may correct or extend those interfaces without weakening
their fail-closed guarantees; removing or incompatibly changing one requires a
new major version. Helper internals, evidence documents, consumer verification
commands, external Driver cadence, and consumer release or deployment systems
are not part of the public API. `CHANGELOG.md` records version history.

## Three layers

| Layer | Responsibility | State authority |
| --- | --- | --- |
| **Kernel** | Authorize and block one approved issue through one safe merge | GitHub issue, linked Project Board, Git, and PR evidence |
| **External Driver** | Decide when to invoke the next bounded kernel command | None; it must reread GitHub and Git on every activation |
| **Consumer policy** | Product scope, additional risk controls, engineering checks, release, deploy, and production | The consumer repository and its operators |

The Kernel is this repository. A Driver may be a human, a scheduled task, or any
agent in any framework, working outside this repository; its source and receipts
live outside, and neither the Kernel nor consumer bootstrap installs one.
Consumer policy may be stricter for a product but is never a universal Aru
requirement. GitHub remains the sole lifecycle authority; operational receipts
never authorize a claim, review, merge or issue closure.

## Kernel invariant

One open issue with a valid contract moves through `Backlog`, `Ready`,
`In Progress`, `In Review`, and `Done`. It has one exclusive writer, one declared
write boundary, one isolated worktree, one exact-current-head server verification
check executed only on the repository's one assigned runner profile, one approval
of that head from another GitHub account, and one governed mechanical merge
through `scripts/merge_pr.py --expected-head`.

The issue contract is:

- an `## Acceptance Criteria` section with an unchecked item;
- exactly one safe `touches:` declaration of repository-relative paths;
- no unresolved `depends-on: #N` issue.

The merge gate reads the declaration that is live at merge time, refuses any
changed path outside it, and records the declaration it enforced as merge
evidence. Scope is not frozen at promotion: a claimant who needs an omitted path
corrects the declaration, and the gate judges the corrected one. See
`docs/decisions/0001-scope-is-judged-live.md`.

## Required lifecycle

1. File or refine the issue in Backlog.
2. Validate it with `triage_backlog.py` and move it to Ready.
3. Claim it before editing.
4. Create and use one `.worktrees/<branch>` checkout.
5. Make the smallest change inside `touches:`. Local checks are preflight and
   audit evidence only; the same compute becomes merge authority only when
   GitHub Actions dispatches the exact-head governed job to the repository's one
   assigned runner profile.
6. Open a PR containing `Closes #N`. The consumer-owned `aru-governed-pr`
   workflow checks out the exact PR head on that profile's runners, runs the
   Factory's `scripts/verify_consumer.sh`, called by the stub through a pinned
   action, and validates `touches:` against the
   actual diff. It has no cross-profile fallback and accepts only verified
   same-repository `pull_request` events. Merge-group verification is
   unsupported, so configured queues and pending queue or auto-merge requests
   are refused before submission; missing queue-state evidence never means
   disabled.
7. Require that exact-head server check, resolve every current-head finding, and
   obtain one approval of the exact head from a GitHub account other than the
   author. A push invalidates earlier check and approval evidence.
8. Submit a direct merge with `merge_pr.py --expected-head`. With the
   merge-authority App configured, the helper posts `aru-merge-authorized` at the
   exact head after its final revalidation and supersedes it with a failure if
   submission fails. Close out only after GitHub confirms the exact PR head
   merged and current authority still passes. `--finalize` recovers confirmed
   direct merges and refuses any historical queue entry. Then verify Done and
   remove only clean, closed Factory worktrees.

Missing, partial, stale, contradictory, truncated, or unauthenticated evidence
blocks the next transition. Never repair authority by hand-editing lifecycle
labels.

Merge submission compares two full gate evaluations, then performs one bounded
final revalidation of the PR, issue, queue state and review evidence immediately
before the merge command. Separate GitHub metadata reads and the merge API are
**non-atomic**: a reread catches observed drift but does not lock metadata, and
post-merge close-out cannot undo a merge already submitted. The evidence those
passes compare, and the races that remain, are recorded in
`docs/decisions/0003-merge-evidence-is-non-atomic.md`.

## Review

Every pull request, whatever it changes, needs one approval of its exact current
head from a GitHub account other than the PR author. Any reviewer counts: a
person, CodeRabbit, or a coding agent working under its own account. The Kernel
does not assign, rank, probe, time out or replace reviewers, and no label records
review state.

GitHub enforces the rule: the bootstrap ruleset requires one approving review,
dismisses stale approvals on push, and requires the most recent push to be
approved by someone other than its pusher. `merge_pr.py` reads the same evidence
and refuses unless some account other than the author (a GitHub App author
`app/<slug>` and its `<slug>[bot]` login are one account) has, as its latest
decisive review (`APPROVED`, `CHANGES_REQUESTED` or `DISMISSED`), approved the
exact head. Comments do not supply approval, and an
approval of an earlier commit does not carry forward. A `CHANGES_REQUESTED`
decision or any unresolved thread still blocks. Agents sharing one GitHub account
cannot approve each other's pull requests.

The writer owns remediation; a reviewer who pushes a fix becomes the last pusher,
so someone else must approve. Every applicable finding needs a disposition from
the feedback skill, and a real security/correctness defect remains blocking
regardless of severity wording; a follow-up issue cannot waive it. Resolving a
thread must reflect its substance.

`fetch_pr_feedback.py` reads all submitted review summaries, in addition to inline
threads. An explicit `[P0]`, `[P1]`, `P0:` or `P1:` finding label (including
Markdown emphasis/headings and `P0 Badge`/`P1 Badge` image labels) blocks merge,
final revalidation and close-out. The author picker returns these summaries as
feedback before CI or merge, even when the PR has no inline comments. Earlier
commits and dismissed reviews remain evidence; an outdated inline thread remains
blocking until resolved. Reviewers should put each defect in an inline thread
and use explicit severity labels when also reporting it in a review summary.

For a summary finding, the writer posts fixes and regression evidence on GitHub.
The original reviewer then submits a later COMMENT or APPROVE review on the exact
current head containing a standalone `Resolves review: <numeric-review-id>` line
and written verification/disposition evidence outside that line. The numeric ID
comes from the finding's `review_id` or its GitHub `pullrequestreview-<id>` URL.
Each line resolves the entire named summary, so every finding in that summary
must be addressed; multiple summaries require separate reference lines. The
confirmation must be by the original reviewer, distinct from the PR author,
and contain no new P0/P1 labels. Do not place the reference in quoted or fenced
example text. A general approval, an author reply, a dismissed review, or a
confirmation on an earlier head does not resolve the finding. Editing the
original summary invalidates any confirmation submitted before that edit. A subsequent push
requires fresh confirmation for that head as well as the usual approval.

This is GitHub-native evidence, not a second lifecycle store or another required
approver. It cannot infer severity from arbitrary prose or verify the truth of
a reviewer's explanation; unlabelled defects still need unresolved inline
threads. Identity is the authenticated GitHub account, not an agent name in the
body; agents sharing credentials cannot be distinguished. If the original
reviewer is unavailable, a summary stays blocked rather than inventing a waiver.
All review pages must be readable and complete. As with the other merge evidence,
separate GitHub reads and merge submission are not atomic.

The Kernel does not wait or poll for review. `fetch_next_work.py` reports an open
pull request with a successful check and no qualifying approval as `review` work
with `next_action: review-by-another-account`; the author cannot do it. An
external Driver may surface it or start a reviewer under another account, but it
owns no lifecycle state. An existing consumer must add the approval rule to its
default-branch ruleset before GitHub enforces it; until then only `merge_pr.py`
does.

The review rule does not vary by path. Aru declares no risk tiers; a consumer
that wants evidence proportional to risk owns that policy itself. See
`docs/decisions/0002-risk-tiers-are-not-kernel-gates.md`.

## Runner profiles and trust boundary

GitHub Actions is the orchestration and check identity. Which machines supply the
compute is one **runner profile**, selected by the repository's account:

| Profile | `runs-on` | Assigned account |
| --- | --- | --- |
| `self-hosted-mac` | `[self-hosted, macOS, ARM64, aru-ci]` | `gillella` personal repositories, including Aru itself |
| `github-hosted` | `ubuntu-latest` | `Unum-Inc` repositories |

Those assignments are declared in `scripts/policy.toml`, not compiled into the
helpers, so adopting Aru under another account is a data change rather than a
source patch. There is still no default: an account with no assignment must pass
`--runner-profile` explicitly, and declaring nothing is refused. A declaration
that contradicts an existing assignment is refused, and there is never a
cross-profile fallback.

The profile chooses compute only. Under either profile every required Kernel job
keeps the `aru-governed-pr` check name, the exact-head checkout with
`persist-credentials: false`, read-only permissions, the same `pull_request`
head-repository provenance condition, the Factory's verifier, and actual-diff
`touches:` enforcement; and rejects `pull_request_target`, Actions caches,
artifact uploads, `environment:`, deployment secrets and write permissions. The
scaffolded workflow records its profile in a single `# aru-runner-profile:`
marker, and the Factory's verifier requires exactly one marker, a known profile, and
that profile's exact `runs-on:` value. There is no cross-profile fallback in
either direction: an offline `aru-ci` pool leaves `aru-governed-pr` queued and
merge blocked rather than rerouting to hosted runners, and a `github-hosted`
repository is refused if its workflow so much as names `self-hosted`. Before
checkout, every job rejects a cross-repository fork PR and proves Python 3.11+,
pip and `gh` are present. Evidence that is missing, unreadable or malformed
leaves capacity unknown and blocks admission.

Every scaffolded repository carries `.aru/manifest.json`, the sha256 of each
Factory-managed file for its runner profile, and `.aru/factory-version`, the Factory
release those files came from; both are copies of the Factory's committed
`templates/manifests/<profile>.json` and declared version, regenerated only by
`init_project.py --sync`. `AGENTS.md` is managed as a block: only the text between the
`ARU_SDLC_GOVERNANCE` markers is hashed and rewritten, and a consumer's own instructions
outside the markers are preserved by verification and by `--sync`; missing or duplicated
markers are a failing check. `aru-merge-policy` verifies every managed file and block
against the manifest first and unconditionally on the exact head, and `aru-merge-policy` verifies
the head's managed files, read through the API and never executed, against the base
branch's manifest; a head that changes `.aru/factory-version` is judged against its own
manifest. A hand-edited manifest is a failing check, not a customization. The residual
boundary is unchanged: an administrator who rewrites the stubs together with the
manifest, or edits the workflow or the ruleset, is outside what a repository-owned file
can refuse.

New scaffolds require an executable `.aru/verify-project.sh` for consumer product
checks; its generated starter fails until replaced, and the framework verifier
runs those checks before reporting success.

The portable bootstrap ruleset has no configured bypass actors, allows merge
commits only, requires one approving review of the last push with stale approvals
dismissed, and requires the `aru-governed-pr` context from the GitHub Actions
App. Helper-only merge is a Kernel process rule unless the optional
merge-authority App is configured: with `ARU_MERGE_APP_RUNNER` (an executable
with the `<runner> --repo OWNER/REPO -- gh ...` interface) and `ARU_MERGE_APP_ID`
both set, `merge_pr.py` posts `aru-merge-authorized` once every gate passes and
the ruleset requires that context pinned to the App's `integration_id`. Exactly
one of the two variables set is a misconfiguration and refuses. While that gate
is on, the helper treats `BLOCKED` as admissible because its own required check
is absent until it posts it; GitHub still refuses if anything else blocks. That App must be distinct from any App agents use for ordinary commands
and needs only Checks write permission. It does not stop whoever holds the App's
key, or an administrator editing the ruleset; that residual boundary is
consumer-owned. A profile is a verification decision, never a deployment one.

## Distribution

The plugin package (root manifest, Claude Code adapter, subagent personas,
advisory hooks, and the installed copy of `scripts/`) is a distribution artifact
of the knowledge layer, not a Kernel component and not an authorization
mechanism. It adds no merge gate, GitHub Actions never loads it, and authority
resolves independently of it.

## Supported surface and non-goals

Supported lifecycle commands are `init_project.py`, `triage_backlog.py`,
`fetch_next_work.py`, `claim_issue.py`, `create_branch.py`, `create_pr.py`,
`check_ci.py`, `fetch_pr_feedback.py`, `merge_pr.py`, `cleanup_worktrees.py`, and
`revert_merge.py`. Installation is `install_agent_integration.sh` and
`install_hooks.sh`.

The Kernel contains no scheduler, daemon, private queue, presence registry,
worker handoff, telemetry, persistent capacity store, notification bridge,
dashboard, preview, deployment, release, incident, or second lifecycle store.

A component enters the Kernel only when it directly authorizes or blocks a
lifecycle transition, no simpler GitHub/Git primitive solves it, the default path
uses it now, and evidence from three governed consumers shows the need.

Source files are limited to 800 lines; supported scripts to 12-14; runtime skills
to six; and active operating documents to `README.md`, `AGENTS.md`,
`CHANGELOG.md`, `docs/KERNEL-CONTRACT.md`, `docs/ENFORCEMENT-REGISTER.md`,
`docs/OPERATIONS.md`, and `docs/DEGRADED-MODE.md`. Aggregate line volume is
deliberately unbudgeted, for production code and tests alike; the 800-line
per-file cap still applies to every file, and tests must not be deleted or
compressed to hide a growing safety surface.
