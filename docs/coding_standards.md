# Coding Standards & Commit Hygiene

This document defines code quality, testing requirements, git commit conventions, and pull request standards for **Aru_Agentic_SDLC**.

---

## 📝 Conventional Commit Specification

All git commit messages MUST follow Conventional Commits formatting:

```
<type>(<scope>): <short description>
```

### Supported Types:
- `feat`: A new feature added to the codebase.
- `fix`: A bug fix.
- `docs`: Documentation updates only.
- `style`: Formatting, missing semicolons, no code change.
- `refactor`: Code refactoring without functionality changes.
- `test`: Adding or correcting unit/integration tests.
- `chore`: Maintenance chores, build scripts, or dependency updates.

### Examples:
- `feat(issue-12): implement session context recovery in fetch_next_issue.py`
- `fix(issue-45): resolve null reference when parsing empty issue labels`

---

## 🧪 Testing & Quality Standards

1. **Local Test Execution**:
   Before committing, agents MUST run the full test suite and confirm 100% pass rate.
2. **Zero Masked Errors**:
   Never resolve failures by swallowing exceptions, adding dummy fallbacks, or deleting failing assertions. Always address the root cause.
3. **CI Pipeline Gatekeeper**:
   No code is merged without passing automated CI runs. If CI fails, inspect logs using `python3 "$ARU_SDLC_HOME/scripts/check_ci.py"` and submit fix commits.

---

## 🔍 Lint Contract

Install the toolchain once per checkout:

```bash
python -m pip install -r requirements-dev.txt
```

Then the two commands `prompts/fleet-worker.md` requires before every push run
as written:

```bash
ruff check .
pytest -q
```

**The toolchain is pinned, and that is the point.** Versions live in
`requirements-dev.txt`; the rule selection lives in `pyproject.toml`. Both are
committed so that a local run and a CI run reach the same verdict. An
unpinned linter is not a gate — agents run on different machines, resolve
different versions, and disagree about the same diff with no way to reproduce
either answer.

**Rules are named, never inherited.** `pyproject.toml` selects `E4`, `E7`,
`E9`, and `F`: the correctness core — import placement, likely mistakes,
syntax errors, and pyflakes. Ruff's built-in defaults widen between releases,
so a repository that relies on them is pinned to a tool version rather than to
a policy, and a routine upgrade silently changes the gate.

Formatting rules are deliberately excluded. This repository has no formatter,
and line-length findings say nothing about whether the code works.

**Pre-existing findings are frozen per file** in `[tool.ruff.lint.per-file-ignores]`
with the reason recorded inline, so the gate applies to new code immediately
rather than waiting for a tree-wide cleanup. Adding a file to that list is a
debt entry, not a fix: it needs a tracked burn-down issue. Removing an entry
should accompany the change that makes it unnecessary.
