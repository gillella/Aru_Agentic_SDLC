<!-- BEGIN ARU_SDLC_GOVERNANCE -->
# Aru_Agentic_SDLC Governance Directive
This environment is governed by Aru_Agentic_SDLC.

1. Confirm work originates from a tracked GitHub issue (Issue-First Law).
2. Read and follow matching skills under `$ARU_SDLC_HOME/skills/`:
   - `run-aru-factory` — "please continue", work the board, loop mode
   - `implement-next-issue` — claim / worktree / implement / PR for an issue
   - `create-github-issue` — file work
   - `code-review` — review a PR in an isolated worktree
   - `remediate-ci-failure` — fix red CI
   - `address-pr-feedback` — resolve review comments
3. Execute Git & GitHub actions via `python3 "$ARU_SDLC_HOME/scripts/<script>.py"`.
4. In the desktop app, `aru code loop`, `continue`, and `keep going` keep the
   current project task working the board. Do not replace it with a CLI agent;
   stop only for an explicit operator stop or required human intervention.
<!-- END ARU_SDLC_GOVERNANCE -->
