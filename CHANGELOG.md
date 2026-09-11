# Changelog

## Unreleased - Authorized reviewers

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

## Unreleased - Safer local cleanup

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

## Unreleased - Agent-independent Aru

- Removed the Hermes Project Driver (`integrations/hermes`), its Hermes plugin
  (`integrations/chopin`) and the Driver-only persona routing
  (`integrations/personas`). Aru's rules work for any agent in any project; an
  optional external Driver is any person, scheduler or agent outside this
  repository.
- Operating documents drop the Hermes project commands, installed-Driver
  operations, canary records and Driver recovery sections. Tests and the pytest
  path no longer reference the removed integrations.
- Installed Hermes runtimes are unaffected and are retired separately.

## Unreleased - One approval from another account (v3)

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

## Unreleased - Scope pinning and reviewer gaps

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

## Unreleased - Merge authority

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

## Unreleased - Manual audit corrections

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
