# Changelog

## v0.2.0 - Minimal Kernel

- Reset Aru to one issue-to-safe-merge lifecycle.
- Removed scheduler, loop, presence, handoff, telemetry, review-capacity,
  reassignment-lock, release, deploy, preview, incident, Slack, visualizer,
  compatibility, and historical factory-product surfaces.
- Replaced the oversized core with small fail-closed commands.
- Reduced active documentation to seven operating files and runtime skills to
  six.
- Made reviewer assignment stable and deterministic without an open-PR
  inventory or distributed lock.
- Added hard surface-budget and forbidden-surface tests.
- Preserved the pre-reset repository at
  `pre-v0.2.0-2026-08-27` (commit
  `388a22b3183e523ee67f857979448d2124e1a854`).

Feature development is frozen through 2026-09-26; only security and correctness
fixes are admitted during the freeze.

## Historical releases

The full pre-v0.2 history and release notes remain available in Git and GitHub.
