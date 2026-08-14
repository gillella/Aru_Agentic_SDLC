---
name: prune-codebase
description: Reports dead Python code, unreferenced prompt files, and orphaned worktree directories without deleting them. Use when the user asks to prune a codebase, find dead code, audit prompts, or inspect stale worktrees.
triggers:
  - "prune the codebase"
  - "find dead code"
  - "audit unused prompts"
  - "find orphaned worktrees"
---

# Prune Codebase Procedure

This workflow gathers cleanup evidence. It is deliberately report-only:
Vulture is a static analyzer and an unregistered directory can still contain
user-owned work, so neither finding authorizes deletion.

## 1. Confirm the governed repository

Inspect `git status`, active worktrees, open pull requests, and the Project
Board before scanning. Preserve all uncommitted and unrelated work.

Install the repository's pinned development tools when needed:

```sh
python3 -m pip install -r requirements-dev.txt
```

## 2. Run the scanner

```sh
python3 scripts/prune_codebase.py --repo-dir .
```

Exit codes are meaningful:

- `0`: no candidates found.
- `3`: one or more reviewable candidates found; this is not a tool failure.
- `1`: Git or Vulture could not complete the scan.

Use `--json` for automation, `--min-confidence 100` for only Vulture's
highest-confidence candidates, and repeated `--python-target PATH` arguments
to restrict Python analysis. Tests are excluded unless `--include-tests` is
given.

## 3. Validate each candidate

- Dead code: search dynamic imports, string-based dispatch, decorators,
  framework registration, tests, and external consumers. Treat Vulture output
  as a lead, never proof by itself.
- Prompt files: confirm the path is not constructed dynamically or consumed by
  an external adapter.
- Worktree directories: inspect tracked, untracked, and ignored files; branch
  and PR ownership; registration state; and any retained checkpoint. Never
  force-remove or recursively delete an uninspected directory.

## 4. Route cleanup through Aru

If removal is warranted, create a tracked cleanup issue with exact `touches:`
metadata using `create-github-issue`, then implement it through the normal Aru
issue-first workflow in an isolated worktree. Re-run the scanner and the full
test suite after each cleanup slice. Do not delete candidates directly from
this skill.
