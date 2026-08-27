---
description: Recover and continue the Aru factory loop for the operator-selected project.
---

Run `$ARU_SDLC_HOME/skills/run-aru-factory/SKILL.md` in **loop** mode for the
current Antigravity project only.

1. If `$HOME/.aru/factory-loop.stop` applies to this project, stop. Do not
   `/schedule` a replacement.
2. Recover claims, worktrees, PRs, reviews, and CI from GitHub before claiming
   new work.
3. `/goal` may be used to finish the current task. `/schedule` starts a **new**
   project-scoped agent that must recover from GitHub — it is not this
   conversation.
4. Never replace this desktop task with another local loop runner or scheduler.
