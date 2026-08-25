<!-- BEGIN ARU_SDLC_GOVERNANCE -->
# Aru_Agentic_SDLC Governance Directive
This environment is governed by Aru_Agentic_SDLC.

1. Confirm work originates from a tracked GitHub issue (Issue-First Law).
2. Read and follow matching skills under `$ARU_SDLC_HOME/skills/`:
   - `run-aru-factory` — `aru code` (synonyms `software`/`dev`/`sdlc`), "please continue", work the board, loop mode. `aru video` / `aru poem` are reserved for unbuilt factories: stop, do not improvise from Code Factory skills
   - `implement-next-issue` — claim / worktree / implement / PR for an issue
   - `create-github-issue` — file work
   - `code-review` — review only a preassigned `review:agent` emergency fallback; otherwise refuse and await external review
   - `remediate-ci-failure` — fix red CI
   - `address-pr-feedback` — resolve review comments
3. Execute Git & GitHub actions via `python3 "$ARU_SDLC_HOME/scripts/<script>.py"`.
4. In the desktop app, `aru code loop`, `continue`, and `keep going` keep the
   current project task working the board. Do not replace it with a CLI agent;
   stop only for an explicit operator stop or required human intervention.
5. Before continuing a loop, check `$HOME/.aru/factory-loop.stop`. If it applies
   to this project, stop and do not arm native wakes.
6. Same-task wake is the active Claude session (`/loop` while it lives). Desktop
   scheduled tasks start a **fresh** session — recover from GitHub, do not claim
   they resumed this conversation. See `$ARU_SDLC_HOME/docs/desktop-agent-continuity.md`.
<!-- END ARU_SDLC_GOVERNANCE -->
