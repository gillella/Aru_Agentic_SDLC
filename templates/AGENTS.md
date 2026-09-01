# Aru Minimal Kernel

Use the canonical Aru commands from `$ARU_SDLC_HOME/scripts`.
GitHub Issues and the linked Project Board are the only lifecycle state.
Require an issue contract, Ready status, exclusive claim, declared
`touches:`, isolated worktree, focused tests, a PR with `Closes #N`,
exact-head focused local verification evidence refreshed through
`create_pr.py --refresh-verification`, exactly one authoritative reviewer
distinct from the author, and merge through
`merge_pr.py --expected-head`.

Follow the validated repository `review-policy:primary=<authority>`, contiguous
`review-policy:fallback-N=<authority>`, and
`review-policy:timeout=<seconds>` label definitions. Without them, use the first
registered external provider, then Claude Code, OpenAI Codex, xAI Cursor, and
Google Antigravity with a 120-second timeout. Inspect the effective policy with
`create_pr.py --reviewer-status --json`. On explicit unavailability or timeout,
invoke `create_pr.py --refresh-reviewer <PR>` (the kernel itself has no
scheduler) to evaluate fallbacks and smoke-test coding capacity (verifying
liveness only). If substantive review execution hits quota, recover with
`create_pr.py --refresh-reviewer <PR> --coding-reviewer-unavailable <reason>`.
External providers require `reviewer-registered:<service>`; coding
identities require `reviewer-binding:<identity>=<github-login>` with an actor
distinct from the author. Never accept self-review or an attestation not bound
to the full current-head SHA; every push invalidates prior review evidence.
Use the newest trusted provider evidence and measure pending time from the
current authority's latest GitHub label-assignment event.

Do not add a scheduler, private queue, handoff file, dashboard, deployment
system, or repository-owned runtime.
