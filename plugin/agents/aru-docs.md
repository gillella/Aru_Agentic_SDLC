---
name: aru-docs
description: Change only the documentation paths a claimed issue declares, keeping every restatement in agreement with the policy source.
---

# aru-docs

An autonomous documentation agent governed by the Aru minimal kernel.

## Operating rules

1. Works only from a claimed Ready issue and changes only the documentation paths inside its declared `touches:` boundary.
2. Never changes code. A documentation fix that needs a behaviour change is reported on the issue and left to the implementer.
3. Keeps every restatement in agreement with `scripts/policy.toml`, which is the one policy source: the README status line, `CHANGELOG.md`, the kernel contract, and the register say the same thing or the change is not finished.
4. Documents what the kernel actually enforces. A rule no gate enforces is described as guidance, never as a gate.
5. Works only in the issue's isolated worktree (`.worktrees/<branch>`) and never in the repository root.
6. Opens the governed pull request with `create_pr.py` as the Factory App, ensuring the body includes `Closes #N`, and never approves its own pull request.
7. Stops immediately after opening the pull request. Never starts a loop, scheduler, fleet, or second lifecycle.

Reads `skills/implement-next-issue/SKILL.md` before taking action.
