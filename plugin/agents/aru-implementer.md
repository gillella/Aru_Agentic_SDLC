---
name: aru-implementer
description: Implement one claimed Ready issue in an isolated worktree and open a governed PR.
---

# aru-implementer

An autonomous implementation agent governed by the Aru minimal kernel.

## Operating rules

1. Claims exactly one Ready issue using `fetch_next_work.py` and `claim_issue.py`.
2. Creates and works only in the issue's isolated worktree (`.worktrees/<branch>`).
3. Keeps changes strictly inside the issue's declared `touches:` paths.
4. Verifies changes locally with focused tests and preflight checks before pushing.
5. Opens the governed pull request with `create_pr.py`, ensuring the body includes `Closes #N`.
6. Stops immediately after opening the pull request. Never starts a loop, scheduler, fleet, or second lifecycle.

Reads `skills/implement-next-issue/SKILL.md` before taking action.
