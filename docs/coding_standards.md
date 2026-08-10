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
