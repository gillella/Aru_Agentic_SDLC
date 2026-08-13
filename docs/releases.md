# Release Procedure & SemVer Governance

This document defines the versioning policies, release procedure, and consumed-CLI surface for **Aru_Agentic_SDLC**.

---

## 📌 SemVer Policy & Major Bump Rule

Aru_Agentic_SDLC follows Semantic Versioning (`v0.X.Y` / `vX.Y.Z`).

- **MAJOR**: Increment when any breaking change is introduced to the **Consumed-CLI Surface** (newly required flags, script renames, changed exit codes, removed subcommands, or breaking parameter contracts).
- **MINOR**: Increment when backwards-compatible features or new skills/helpers are added.
- **PATCH**: Increment for backwards-compatible bug fixes or documentation updates.

> **Note**: Versioning is derived strictly from git release tags (`vX.Y.Z`). Hand-maintained version files are prohibited to prevent version drift.

---

## 🛠️ Consumed-CLI Surface

The following scripts constitute the supported consumer-facing CLI surface:

1. `scripts/init_project.py`
2. `scripts/fetch_next_work.py` / `scripts/fetch_next_issue.py`
3. `scripts/claim_issue.py`
4. `scripts/create_branch.py`
5. `scripts/create_pr.py`
6. `scripts/check_ci.py`
7. `scripts/fetch_pr_feedback.py`
8. `scripts/update_issue_status.py`
9. `scripts/merge_pr.py`
10. `scripts/triage_backlog.py`
11. `scripts/enable_main_ruleset.py`
12. `scripts/install_local_agent_integrations.sh` / `scripts/install_cursor_integration.sh`
13. `scripts/release.py`

---

## 🚀 Release Procedure for Factory Agents

To generate `CHANGELOG.md` and cut a SemVer release tag:

1. **Generate `CHANGELOG.md`**:
   ```bash
   python3 scripts/release.py --generate-changelog
   ```

2. **Preview Release Tag**:
   ```bash
   python3 scripts/release.py --tag v0.1.0 --dry-run
   ```

3. **Cut Release Tag**:
   ```bash
   python3 scripts/release.py --tag v0.1.0
   ```

4. **Verify Release Hygiene**:
   ```bash
   python3 scripts/release.py --check
   ```
