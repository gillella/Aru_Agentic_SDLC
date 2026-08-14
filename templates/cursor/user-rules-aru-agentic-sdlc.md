# Paste into Cursor → Customize → Rules → User Rules
# (append below any existing user rules you want to keep)

## Aru Agentic SDLC (global)

For software engineering work in repositories that use (or should use)
Aru_Agentic_SDLC governance:

1. Treat `$ARU_SDLC_HOME` (default `/Users/aravindgillella/projects/Aru_Agentic_SDLC`)
   as the single source of truth for skills and helper scripts. Do not copy
   those skills into application repos.
2. Obey the Issue-First Law: no code change, refactor, or feature work
   without a tracked GitHub issue on the project board.
3. When the task matches an SDLC skill, read that skill's `SKILL.md` from
   `$ARU_SDLC_HOME/skills/` (or the symlinked Cursor skill of the same name)
   and follow it before improvising.
4. Default skill routing:
   - please continue / continue / keep going / work the board → `run-aru-factory` (loop)
   - implement next issue / a named issue → `implement-next-issue`
   - new governed repo → `init-agent-project`
   - file bug/feature/task → `create-github-issue`
   - review a PR → `code-review`
   - red CI → `remediate-ci-failure`
   - PR review comments → `address-pr-feedback`
5. Use worktrees under `.worktrees/`, never push straight to main/master,
   run local tests before commit, and put `Closes #<n>` in every PR body.
6. Run GitHub/git automation via
   `python3 "$ARU_SDLC_HOME/scripts/<script>.py"` with a stable `--agent`
   id (for example `cursor-1`).
7. If a project lacks `AGENTS.md` but the user wants this process, offer to
   bootstrap with `init-agent-project` rather than inventing a parallel workflow.
8. Before continuing a factory loop, check `$HOME/.aru/factory-loop.stop`. If it
   applies to this project, stop. Cursor's loop skill can wake the same turn
   while the session lives; Cursor Automations start a new agent and are not
   same-task wake. App quit, sleep, and reboot are not software guarantees.
