# Changelog

## v2.3.0 - Managed-file manifest - 2026-09-14

- Every scaffolded repository now carries `.aru/manifest.json`, the sha256 of each
  Factory-managed file for its runner profile, and `.aru/factory-version`, the release
  those files came from. Both are copies of the Factory's committed
  `templates/manifests/<profile>.json` and declared version; `init_project.py` writes
  them on scaffold and rewrites them on `--sync` (#727).
- `.aru/verify.sh` verifies every managed file against that manifest in its first
  section, unconditionally: the check no longer depends on the pull request having
  touched a governance path (#727).
- `aru-merge-policy` runs `.aru/hooks/check_manifest.py` from the base branch. It reads
  each managed file at the exact head through the GitHub contents API, hashing but never
  executing it, and compares it to the base branch's manifest. A head that changes both
  the manifest and `.aru/factory-version` is reported as an upgrade and judged against
  its own manifest; changing one without the other is refused (#727).
- `templates/manifests/self-hosted-mac.json` and `github-hosted.json` are committed and
  rendered, with tests that fail on drift, name the regeneration command, and require a
  `[release]` bump whenever a manifest's hashes change since the newest tag (#727).
- `scripts/manifest.py` is the one library that defines the managed set;
  `integrations/adoption/check.py` keeps no file list of its own any more, so its report
  no longer omitted `merge-policy.yml`, and gained a `manifest` field (#727).
- New gate `managed-file-integrity` in `scripts/policy.toml` and the register, and the
  rule in `docs/KERNEL-CONTRACT.md`. The stated residuals: an upgrade head's new manifest
  is unauthenticated until the Factory verifies it, and an administrator can still
  rewrite `.aru/verify.sh` with the manifest or edit the workflow or ruleset (#727).

- The release version is declared once, as `[release]` in `scripts/policy.toml`.
  `policy.release()` and `policy.version()` read it; README's project-status line,
  the newest released heading in this file and the newest `v*` tag are asserted
  against it by `tests/test_release_truth.py`, and `docs/OPERATIONS.md` §18 records
  the release procedure. This history was reconstructed on 2026-09-14 from git:
  README and this file had said v2.0.0 while `v2.2.1` was the newest tag, the
  thirteen sections now under `v2.1.0` were written as separate `## Unreleased`
  sections and first shipped in that tag, and the work since `v2.2.1` is listed
  here for the first time (#725).
- `init_project.py --adopt` brings an existing, ungoverned repository under the
  kernel: it plans, writes, commits and pushes the framework files, then provisions
  labels, Project and ruleset, naming `--sync --ruleset` as the recovery when
  provisioning fails after the push (#714, #716).
- The scaffolded `merge-policy.yml` runs the hook path the scaffold actually writes
  (#718).
- A `touches:` rule that can match no file, such as a bare `docs/`, is refused when
  the contract is validated instead of after the work is done (#719).
- A review summary carrying an unresolved P0/P1 finding blocks merge, and editing
  that summary invalidates a clearance given before the edit (#723).

## v2.2.1 - Ruleset matched by its contexts - 2026-09-13

- `init_project.py --sync --ruleset` finds the governed ruleset by the contexts it
  requires rather than by its name, so a renamed ruleset is updated instead of
  duplicated (#712). Sync consumers from this tag, not from v2.2.0.

## v2.2.0 - Consumer sync - 2026-09-13

- `init_project.py --sync [--check] [--ruleset]` brings an already governed
  repository up to the current framework files and boundary while preserving the
  consumer-owned `.aru/review.json`, `.aru/verify-project.sh` and `.gitignore`
  (#710). Reprovisioning under this tag could create a duplicate ruleset, fixed in
  v2.2.1.

## v2.1.0 - Policy source, one-approval review, merge authority - 2026-09-12

The sections below were written as separate `## Unreleased` sections and first
shipped in this tag; they are preserved as written. Also in this tag and not
recorded at the time: `1678326` declares the kernel gates once in
`scripts/policy.toml` and renders `docs/ENFORCEMENT-REGISTER.md` from it;
`e9392d6` renders the `AGENTS.md` kernel path and the bootstrap ruleset from the
same file; `923b7d1` adds the read-only delivery report and `f5529b9` makes it read
check runs from the head commit (#704); `4619acd` exposes the kernel commands as
typed MCP tools and `9b9fb64` validates their arguments (#703); `0069d5b`
attributes every merge refusal to a declared gate; `1e57c92` removes the risk-tier
surface no gate read; `22d9b35` resolves the runner profile from declared data;
`415ef43` makes the merge boundary unweakenable by a policy edit (#705). The
one-approval review rule below is a breaking change that shipped in this minor
tag without a major-version bump.

### The contract states rules, not history

- `docs/KERNEL-CONTRACT.md` dropped from 334 to 210 lines in this change (243 after
  later amendments). Every remaining paragraph
  states a rule that blocks or authorizes a transition; rationale, implementation
  detail and one consumer's incident runbook moved to `docs/decisions/`.
- New records: `0001-scope-is-judged-live` (why the promotion digest pin went),
  `0002-risk-tiers-are-not-kernel-gates` (the tier table, kept for consumers),
  `0003-merge-evidence-is-non-atomic` (which evidence each gate pass compares and
  which races remain), `0004-jmc-185-downstream-regeneration` (the JMC #185
  procedure, which bound no transition in this repository).
- Decision records are nested under `docs/`, so `tests/test_surface.py` does not
  count them against the seven operating documents; a new test asserts that and
  that the budget still binds.
- Migration accounting that was in the contract lives here: the one-approval rule
  replaced the path-tiered, provider-routed review of v2, retiring the `review:*`,
  `reviewer*`, `review-policy:*`, `author:*`, `author-family:*` and
  `needs-reviewer` label contracts and the reviewer options of `create_pr.py`.
  That removal is a breaking change and shipped in v2.1.0 without a major-version
  bump; this history records that rather than promising a separate v3. The v2.0.0
  migration withdrew merge-queue support; the workflow verifies same-repository PR
  heads only and cannot prove a combined queue revision, so the helper refuses
  configured queues and pending queue or auto-merge requests before submission,
  and no existing queue is canceled by that correction.
- No rule was dropped: every load-bearing identifier in the previous contract was
  checked to survive in the contract, a decision record, or this file.

### Reducing the CLI token

- The operations guide records the scopes the kernel requires (`repo`, `project`,
  `read:org`), that `gh auth refresh` only adds scopes so reducing them means
  authenticating again, and that a new repository's first push fails without `workflow`
  until the App is installed on it.
- It states what the change is worth now: with a base-branch check judging pull requests
  and a ruleset carrying no bypass actors, removing the scope is least privilege rather
  than the control that closes the workflow-rewrite route.


### Runner isolation runbook

- The operations guide now states what pull-request code can reach on a self-hosted runner,
  and gives ordered steps for moving the runners onto an unprivileged account: create it,
  make the verification toolchain reachable from it, deregister, re-register, and confirm.
- It records the two details that are expensive to discover: a fresh account may not
  resolve `gh`, which fails the trust-boundary step before any test runs, and a lone
  `--ephemeral` runner deregisters after one job and halts verification.
- It is explicit that an unprivileged account bounds what pull-request code reaches and
  does not sandbox it.


### Less machinery around the review gate

- `review_authority.py` no longer carries a command-line entry point. It existed for the
  base-branch workflow step that was removed when that workflow stopped applying the
  posture; nothing has invoked it since.
- `aru-merge-policy` publishes its verdict once, through its own check run, and no longer
  requests `statuses: write`. The explicit commit-status step was written on the belief
  that the check run attached to the base commit and could never be required; the head
  carries both, so it was a duplicate.
- The operations guide now records which check gates a pull request and why the workflow
  triggers on `pull_request_target` alone.


### An approval says something

- Under the `human` posture an approval must carry a written body of at least twelve
  characters, and the refusal names the missing judgement rather than reporting a generic
  approval failure. The permissive postures are unchanged.
- Twelve characters is stated and reasoned rather than tuned: it excludes the tokens typed
  without looking and admits a short real sentence. It is a floor on effort. It does not
  establish that the change was read, and the operating guide says so.
- Corrects a claim left by the previous change: the enforcement register said the
  base-branch workflow applied the posture refusal. It does not, because the trigger that
  would let it re-evaluate after an approval runs the pull request's own copy of the
  workflow. `merge_pr.py` is the only path that applies it.


### The scope contract is judged live

- Promotion no longer stamps a `ready:<digest>` scope pin, and the merge gate no longer
  refuses an issue whose body changed after promotion. Correcting a `touches:` declaration
  on a claimed, in-flight issue now needs no status change and no hand-edited board.
- The declaration is still required at Ready, still reserves paths, and is still enforced
  against the actual diff; the declaration that was enforced is still recorded as merge
  evidence, so the approved scope stays auditable.
- The pin directed the operator to return the issue to Backlog. The kernel implements no
  transition to Backlog, and once a pull request is open the issue is In Review and
  releasing the claim is refused, so the instruction could not be followed at all. No
  kernel message now instructs a transition the kernel does not implement, and a test
  asserts it.
- Stale `ready:*` labels on issues promoted before this change are inert.


### Authorized reviewers

- A repository declares who may approve in `.aru/review.json` on its default branch.
  `human` requires the approving account to be listed and refuses any GitHub App, `any`
  reproduces the previous rule, and `none` requires no approval. An absent, malformed or
  unknown declaration resolves to `human`, so the gate cannot be lost by omission.
- The declaration is read from the default branch, never the pull-request head, so a
  change cannot authorize itself: weakening the posture, or adding a reviewer, needs an
  approval that the posture already on the default branch accepts.
- `merge_pr.py` and the `aru-merge-policy` workflow apply the same refusal, so neither
  path alone admits a merge. That workflow now also runs on `pull_request_review`,
  because an approval is not a push and would otherwise never be re-evaluated.
- The bootstrap seeds the creating account as the first authorized reviewer, so a new
  repository is not deadlocked by its own strict default.
- A repository that has never declared a posture authorizes the account that owns it,
  so the strict default cannot refuse a repository its own first merge. An
  organization owner matches no person, so an organization declares its reviewers.
- This binds approval authority to named accounts and structurally excludes Apps. It does
  not establish that a person read the change: an agent holding a credential for a listed
  account is indistinguishable from its owner at every API, and that residual is covered
  by credential hygiene rather than by this gate.

### Safer local cleanup

- `cleanup_worktrees.py` retains worktrees that git reports locked (a worker
  holds them) in both dry-run and real runs, and retains worktrees whose ignored
  files include anything other than disposable caches. A cache counts only in the
  role it really has -- a cache name as a directory, `.DS_Store` as a file -- so an
  unrelated `.DS_Store/backup.json` or `reports/data.pyc` keeps the worktree, and
  ignored paths are read NUL-delimited so unusual names classify correctly. A
  failure on one worktree is reported, the sweep continues, and the command exits
  non-zero.
- The operations guide documents verified checkpoints, worktree locks and
  host-local cleanup.

### Agent-independent Aru

- Removed the Hermes Project Driver (`integrations/hermes`), its Hermes plugin
  (`integrations/chopin`) and the Driver-only persona routing
  (`integrations/personas`). Aru's rules work for any agent in any project; an
  optional external Driver is any person, scheduler or agent outside this
  repository.
- Operating documents drop the Hermes project commands, installed-Driver
  operations, canary records and Driver recovery sections. Tests and the pytest
  path no longer reference the removed integrations.
- Installed Hermes runtimes are unaffected and are retired separately.

### One approval from another account (v3)

- Review is one rule for every PR: an approval of the exact head from a GitHub
  account other than the author. The bootstrap ruleset requires one approval,
  dismisses stale approvals and requires last-push approval; `merge_pr.py`
  rereads the reviews before submission.
- Removed the reviewer-policy machinery: path-tiered review, CodeRabbit
  capability probes and deadlines, coding-agent attestations and bindings,
  `review_policy.py`, `review_evidence.py`, `reviewer_probe.py`,
  `legacy_recovery.py`, and every reviewer option of `create_pr.py`. The Kernel
  no longer reads or writes `review:*`, `reviewer*`, `review-policy:*`,
  `author:*`, `author-family:*` or `needs-reviewer` labels.
- `fetch_next_work.py` finds an agent's PR from its claimed issue's branch and
  reports an unapproved PR as `review` work for another account.
- The Hermes Driver no longer launches review workers; PRs wait for an approval.
- Breaking: existing consumers add the approval rule to their ruleset, and
  agents authoring under one GitHub account need a reviewer under another.

### Scope pinning and reviewer gaps

- Drop the 6,500-line Kernel production ceiling; it was full and blocked every fix.
  The 800-line per-file cap and the Hermes Driver's own limit remain.

- Pin the Ready contract at promotion: triage labels the issue `ready:<digest>`
  of its number, criteria text and `touches:`; the merge gate refuses if either
  changed afterwards. Unpinned issues from before this change are grandfathered,
  and close-out deletes the pin label.
- With no reviewer available, `create_pr.py` opens the PR as `needs-reviewer`
  instead of refusing; the merge gate still refuses until `--refresh-reviewer`
  assigns an authority.
- Triage lists issues without gh's `--label` filter, which went through the
  lagging search index and hid freshly labelled issues; truncated inventories
  now refuse.
- `revert_merge.py` opens its PR from the revert worktree; it previously always
  failed with "current branch does not belong to the issue".
- Tier 3 docs no longer imply a stricter Kernel gate than Tier 2.
- Tests fail if they reach live GitHub; one existing test had been reading the
  real repository.

### Merge authority

- Add an optional merge-authority App. With `ARU_MERGE_APP_RUNNER` and
  `ARU_MERGE_APP_ID` set, `merge_pr.py` posts `aru-merge-authorized` at the exact
  head after every gate, and the ruleset requires it pinned to that App, so
  `gh pr merge` alone can no longer complete a governed merge. Unset, behaviour
  is unchanged.
- Bootstrap pins the check only when the App can already act on the new
  repository, and new consumer rulesets allow merge commits only.
- A later "Reviews paused" note from the external reviewer no longer retracts an
  approval already given for the exact head; any other unavailability still does.
- New repositories no longer get the retired `review:sourcery` / `review:codeant`
  labels, and the scaffolded secret scan matches any `*API_SECRET*` assignment
  instead of naming one client's variable.

### Manual audit corrections

- Revalidate linked Project card and current dependency authorization before merge.
- Protect Kernel helper paths and executable README-like files with appropriate review tiers.
- Require configured consumer product verification; retain framework checks and upgrade guidance.
- Bound unchanged worker retries and align quota review reservations with required risk.
- Add a read-only consumer comparison tool and consumer-owned deployment guidance.
- Reconcile current release/freeze instructions and broaden integration verification.
- Prepared as an explicitly authorized one-time manual maintenance change.
  This source entry does not claim publication, installation, or live Driver acceptance.

## v2.0.0 - Reviewer-policy and verification migration - 2026-09-09

- Withdraw the previously declared merge-queue capability: verification is
  PR-only, and configured queues or pending queue/auto-merge requests are refused
  before submission. Missing queue evidence is not absence. Confirmed direct
  merges retain `--finalize` recovery; historical queue work is refused instead
  of being closed from PR-head checks. This compatibility change belongs to
  the v2.0.0 migration and does not activate or reconfigure a live queue.
- Retire Sourcery and CodeAnt while retaining historical evidence.
- Prefer CodeRabbit with bounded current-head App capability; otherwise select
  independent coding fallback immediately. Completed generic green checks are
  not capability or approval. Accepted reviews use a configurable 900-second
  default completion deadline; explicit failure bypasses it.
- Reviewer discovery uses `aru.reviewer-status/v3`. Reconcile consumers from
  the merged source; this entry is not a release or installed rollout claim.

## v1.0.4 - Native Hermes Event Compatibility - 2026-09-05

- Fixed a live Hermes Project Driver compatibility failure discovered during
  the v1.0.3 rollout: the deployed gateway authenticates and binds each webhook
  session as `webhook:ROUTE:DELIVERY` but can omit the separate message-ID
  environment export.
- The Driver now accepts that native binding and rejects contradictory
  identifiers, wrong routes, and malformed deliveries before dispatch.
- Consumer scaffold artifacts are unchanged from v1.0.3; no consumer product
  was deployed by this release.
- Source: `8f28876698e5f74b20423e71791ccbecee7a7f90`, merged through PR #561;
  the merged tree matches the verified PR head. Validation: 965 tests, Ruff,
  and exact-head `aru-governed-pr` passed.

## v1.0.3 - Authorization Freshness and Hermes Driver Recovery - 2026-09-05

- Revalidate the linked issue, declared write scope, and completed acceptance
  semantics immediately before merge submission, allowing benign
  description-only edits; refuse changed or missing closing-issue authorization
  after the final hook reread.
- Restored separately installed Hermes Project Driver recovery: immediate
  native events plus one ten-minute recovery heartbeat, shared-account
  admission, bounded queues, and typed cross-project handoffs.
- Preserved retries and duplicate suppression with atomic bounded event state,
  validated worker receipts, reversible installation, and bounded agent
  execution with descendant cleanup.
- Existing v1.0.2 consumers need the updated canonical scope-enforcement hook;
  regenerate centrally owned scaffolding and preserve your own verification
  policy.
- Immutable release source: `e7c66db6ace256a95f42457b697a88875fa71931`
  (merged PRs #558 and #559). Validation on that commit: 955 tests passed;
  Ruff, verification-script syntax, and diff hygiene passed.

## v1.0.2 - Fail-Closed Merge-Group Provenance - 2026-09-02

- Supersedes v1.0.1 for governed consumers using persistent self-hosted
  runners.
- `pull_request` runs remain admitted only with proven same-repository head
  provenance; `merge_group` and any other event lacking trusted
  same-repository provenance now fail closed before checkout or
  repository-controlled execution.
- Both the canonical template and Factory's live workflow carry the same rule
  and are covered by drift/regression tests.
- Consumer action: do not merge consumers generated from v1.0.1; refresh
  `.github/workflows/governed-pr.yml` from v1.0.2, rerun exact-head governed CI
  and authoritative review, then merge through the Factory helper. Rollback
  baseline: v1.0.0.
- Exact source: Factory PR #553 merge commit
  `44da258dd547dbbbf9e7d78e092c360b07ab96f8`. Full suite: 535 passed.

## v1.0.1 - Self-Hosted Trust and Verification Hardening - 2026-09-01

### Included changes

- Hardened `templates/governed-pr.yml` and `.github/workflows/governed-pr.yml`
  to preserve raw provenance and fail closed before checkout when
  head-repository identity is empty, a fork, or an unproven merge group.
- Expanded `templates/verify.sh` secret scanner to detect high-entropy
  `API_SECRET_KEY` / `JMC_API_SECRET` assignments and modern hyphenated
  `sk-proj-...` keys without self-match or placeholder false positives.
- Ensured deterministic rename collection (both source and destination) in
  `templates/verify.sh` and `hooks/enforce_touches.py`.
- Made `templates/verify.sh` workflow permission and runner checks portable
  across BSD/macOS grep using POSIX character classes.
- Used machine-safe NUL-delimited diff parsing (`--name-status -z`) in
  `templates/verify.sh` and `hooks/enforce_touches.py` to handle paths with
  spaces, tabs, newlines, quotes, and Unicode filenames without ambiguous shell
  conversion, including both rename and copy sides.
- Added bounded PR head revalidation immediately before return in
  `hooks/enforce_touches.py` to close race windows against late pushes during
  touches enforcement.
- Standardized `python3` invocation across workflow preflight and touches
  enforcement.
- Extended consumer drift and scaffold fixture tests to bind hashes for
  `governed-pr.yml` and `.aru/lib/touches.py`, and executable permissions for
  `.aru/verify.sh`.

## v1.0.0 - Governed, Risk-Proportional Delivery - 2026-09-01

### Release summary

- Promoted the existing GitHub-centered issue-to-safe-merge Kernel to its
  stable v1 public contract without adding a release subsystem.
- Since v0.2.8, exact-head self-hosted Actions is merge authority, review is
  path-risk proportional, work selection is one read-only pick plus one claim,
  and the installed integration contains exactly six skills.

### Upgrade from v0.2.8

Stage a fresh scaffold, reconcile `AGENTS.md`, `.github/`, `.aru/`, and
`.gitignore`, configure the consumer verification script, register an
Apple-silicon macOS `aru-ci` runner, and complete one governed pilot. Keep
v0.2.8 as the rollback baseline; never move the v1.0.0 tag. Aru remains a
governance Kernel, not a scheduler, release manager, or deploy platform.

### Included changes

- Required governed GitHub Actions jobs to run only on repository-level
  Apple-silicon macOS self-hosted runners labeled `aru-ci`, with no
  GitHub-hosted fallback.
- Kept exact-head Actions verification as merge authority while avoiding
  GitHub-hosted runner minutes and default artifact/cache storage; documented
  that runner availability, maintenance, electricity, disk, and security are
  operator-owned.
- Installed a consumer-owned `aru-governed-pr` workflow that checks out the
  exact PR head, runs `.aru/verify.sh`, and enforces the linked issue's
  `touches:` boundary against the actual diff; merge queues rerun verification
  on `merge_group` revisions, and bootstrap pins the required context to the
  GitHub Actions App.
- Made the exact-head server check the required verification authority and
  local verification optional preflight or audit evidence; documented the
  portable ruleset's server-enforcement boundary without claiming helper-only
  merge is technically exclusive.
- Established one short canonical contract separating the Kernel, external
  Driver, and consumer policy; documented path-derived risk tiers, omitted the
  authoritative-review wait for Tier 0-1, and retained one review plus one
  continuation event for Tier 2-3.
- Removed provider-order and reviewer-selection algorithms from policy prose,
  along with repeated verification ceremony and mandatory merge preview
  guidance.
- Reconciled installed agent guidance with the six current skills and
  `fetch_next_work.py`; the installer safely migrates the known legacy Codex
  global guidance while preserving a backup.
- Reduced each activation to one read-only selection for one agent and removed batch
  allocation, automatic Backlog recovery, and epic reconciliation from the
  Kernel lifecycle.
- Removed the generic lifecycle-status setter, hardened exclusive claim acquire
  and release races, and added restart-safe merge-queue finalization.
- Authenticated required and provider check runs to their GitHub Apps, bound
  reviewer evidence to the current head and assignment window, and made the
  default branch dynamic.
- Contained bootstrap and hook installation within trusted repository metadata;
  symlinked scaffold paths, custom external hook paths, and unsafe hook targets
  now fail closed.
- Removed unauthenticated optional check-name configuration; wider consumer
  verification belongs inside `.aru/verify.sh` or consumer-owned branch rules.

## v0.2.8 - Consumer Repository App Routing - 2026-08-31

- Passed the governed consumer repository to the GitHub App runner.
- Classified malformed Ready dependency metadata without preventing selection
  of later valid candidates.

## v0.2.7 - Live Batch PR State Hotfix - 2026-08-31

- Read live current-head merge state for batch PR recovery instead of trusting
  an incomplete activation snapshot.

## v0.2.6 - Board Liveness and Conflict Recovery - 2026-08-31

- Restored bounded event-driven Backlog recovery when Ready is empty.
- Added capacity-aware multi-lane assignment, structured Ready exclusions,
  transactional status safeguards, and verified epic reconciliation.
- Moved routine PR verification away from repository-hosted GitHub Actions to
  the then-current local evidence model.

## v0.2.5 - Bounded Multi-Lane Dispatch - 2026-08-28

- Added one-snapshot bounded multi-lane work selection with global `touches:`
  reservations and fail-closed inventory validation.
- Hardened deleted-path write-boundary enforcement and reviewer-state recovery.
- Added the governance audit and north-star evidence documents without growing
  the seven-document operating surface.

## v0.2.4 - Distributed Review and Coordination Efficiency - 2026-08-27

- Added governed coding-reviewer identities and capacity probes while excluding
  the author.
- Replaced full-board Project scans with targeted status updates to reduce
  GraphQL quota use.
- Aligned Ready-contract validation across issue entry points.

## v0.2.3 - Governed Reviewer Fallback - 2026-08-27

- Added exact-head coding-agent reviewer fallback, identity bindings, formal
  attestations, and guarded recovery from unavailable review capacity.
- Hardened trusted external-provider evidence and GitHub App authentication.

## v0.2.2 - Consumer Adoption Ready - 2026-08-27

- Expanded the README and operations guide for new and existing consumers.
- Documented operator-owned setup and reconciled the reset-era board,
  branches, and worktrees.
- Marked the minimal kernel ready for consumer pilots.

## v0.2.1 - Reset Correctness Fixes - 2026-08-27

- Scoped surface-budget checks to tracked kernel files.
- Replaced legacy pre-push hooks without chaining retired lifecycle behavior.

## v0.2.0 - Minimal Kernel

- Reset Aru to one issue-to-safe-merge lifecycle.
- Removed the scheduler, loop, presence, handoff, telemetry, release, deploy,
  preview, incident, Slack, visualizer, compatibility, and historical
  factory-product surfaces.
- Reduced the active surface to small fail-closed commands, six runtime skills,
  and seven operating documents.
- Preserved the pre-reset repository at tag `pre-v0.2.0-2026-08-27`
  (`388a22b3183e523ee67f857979448d2124e1a854`).

Feature development is frozen through 2026-09-26; only security and correctness
fixes are admitted during the freeze.

## Historical releases

The full pre-v0.2 history and release notes remain available in Git and GitHub.
