# Aru Minimal Kernel

Use the canonical Aru commands from `$ARU_SDLC_HOME/scripts`.
GitHub Issues and the linked Project Board are the only lifecycle state.
Require an issue contract, Ready status, exclusive claim, declared
`touches:`, isolated worktree, focused tests, a PR with `Closes #N`,
current-head CI, one assigned external reviewer, and merge through
`merge_pr.py --expected-head`.

Do not add a scheduler, private queue, handoff file, dashboard, deployment
system, or repository-owned runtime.
