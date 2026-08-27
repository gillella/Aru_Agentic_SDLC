# Aru Minimal Kernel

Use the canonical Aru commands from `$ARU_SDLC_HOME/scripts`.
GitHub Issues and the linked Project Board are the only lifecycle state.
Require an issue contract, Ready status, exclusive claim, declared
`touches:`, isolated worktree, focused tests, a PR with `Closes #N`,
current-head CI, exactly one authoritative reviewer distinct from the author,
and merge through
`merge_pr.py --expected-head`.

Prefer CodeRabbit, then Sourcery, then CodeAnt. On explicit unavailability or
15 minutes pending, use `create_pr.py --refresh-reviewer <PR>` to smoke-test and
assign a distinct Claude Code, OpenAI Codex, xAI Cursor, or Google Antigravity
reviewer. External providers require `reviewer-registered:<service>`; coding
identities require `reviewer-binding:<identity>=<github-login>` with an actor
distinct from the author. Never accept self-review or an attestation not bound
to the full current-head SHA; every push invalidates prior review evidence.
Use the newest trusted provider evidence and measure pending time from the
current authority's latest GitHub label-assignment event.

Do not add a scheduler, private queue, handoff file, dashboard, deployment
system, or repository-owned runtime.
