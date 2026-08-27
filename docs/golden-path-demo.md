# Golden-path demo

Companion repository:
[gillella/aru-golden-path-demo](https://github.com/gillella/aru-golden-path-demo).
Board:
[aru-golden-path-demo Board](https://github.com/users/gillella/projects/6).

This playbook stays the factory. The demo is a thin consumer: its `AGENTS.md`
points at `$ARU_SDLC_HOME`. It does not vendor playbook `skills/` or lifecycle
helpers (`scripts/claim_issue.py`, `scripts/merge_pr.py`, and the rest).

## Pin (S1.4)

Consumer pinning is `ARU_SDLC_REF`. The demo README currently pins playbook
commit `8d0513b` (the `origin/main` tip this walk was written against). Prefer
an annotated `ckpt/<PR>-<sha7>` tag when you want a specific gated merge.
Installing and upgrading refs: `docs/cursor-integration.md`.

Do not pin the moving branch name `main` if you want a frozen walk.

## What `init_project.py` writes

Bootstrap:

```bash
python3 "$ARU_SDLC_HOME/scripts/init_project.py" \
  --name aru-golden-path-demo \
  --public \
  --create-board \
  --owner <github-login> \
  --target-dir /path/to/aru-golden-path-demo
```

`--owner` must be the GitHub login (not `@me`). `gh project link` rejects `@me`
when the repository owner is the login string.

`scripts/init_project.py` also writes the consumer governance helpers plus
generated `.github` review/touches helpers. Those are not a second factory.
The **demo** repository's `skills/` directory stays empty (`.gitkeep` only).
Happy-path procedures live in
`$ARU_SDLC_HOME/skills/`, including `idea-to-prd`, `prd-to-issues`,
`implement-next-issue`, and `code-review` at the pinned SHA.

## Happy path

Every step leaves an issue or PR on the **demo** board. Follow the playbook
skills under `$ARU_SDLC_HOME`, not the empty demo `skills/` directory.

1. File an idea. Skill: `$ARU_SDLC_HOME/skills/idea-to-prd/SKILL.md`. Seed idea:
   [aru-golden-path-demo#1](https://github.com/gillella/aru-golden-path-demo/issues/1).
2. After operator approval, decompose with `$ARU_SDLC_HOME/skills/prd-to-issues/SKILL.md`.
   Seed implementation:
   [aru-golden-path-demo#2](https://github.com/gillella/aru-golden-path-demo/issues/2).
3. Claim and implement in a worktree (`$ARU_SDLC_HOME/skills/implement-next-issue/SKILL.md`).
   Seed PR:
   [aru-golden-path-demo#3](https://github.com/gillella/aru-golden-path-demo/pull/3).
4. A **distinct** agent reviews (`$ARU_SDLC_HOME/skills/code-review/SKILL.md`). Authors never
   self-review.
5. `python3 "$ARU_SDLC_HOME/scripts/merge_pr.py" --pr <ID>`

## Runnable surface

The retained demo walk stops at governed merge. This repository no longer
claims a bundled preview builder or post-merge deploy helper as part of the
golden path.
