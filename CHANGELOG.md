# Changelog

## v1.0.1 - Self-Hosted Trust and Verification Hardening - 2026-09-01

### Included changes

- Hardened `templates/governed-pr.yml` to preserve raw provenance and fail closed
  when head-repository identity is empty or a fork.
- Expanded `templates/verify.sh` secret scanner to detect high-entropy
  `API_SECRET_KEY` / `JMC_API_SECRET` assignments and modern hyphenated
  `sk-proj-...` keys without self-match or placeholder false positives.
- Ensured deterministic rename collection (both source and destination) in
  `templates/verify.sh` and `hooks/enforce_touches.py`.
- Made `templates/verify.sh` workflow permission and runner checks portable
  across BSD/macOS grep using POSIX character classes.
- Used machine-safe NUL-delimited diff parsing (`--name-status -z`) in
  `hooks/enforce_touches.py` to handle paths with spaces, tabs, newlines,
  quotes, and unicode filenames deterministically without escaping errors.
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
