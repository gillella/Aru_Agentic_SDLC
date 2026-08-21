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
12. `scripts/install_local_agent_integrations.sh` / `scripts/install_agent_integration.sh`
13. `scripts/release.py`
14. `scripts/increment_release.py`

---

## 🏷️ Sprint Checkpoint Releases (`ckpt/*`)

Durably accepted Delivery Increments (sprints) are tagged and recorded via:

```bash
python3 scripts/increment_release.py \
  --increment <increment_id> \
  --commit <40-character default-branch SHA> \
  --checkpoint-run-url <canonical GitHub Actions run URL>
```

- **Deterministic Tagging**: Tagged as `ckpt/<project_id>/<increment_id>` targeting the exact accepted commit on the default branch.
- **Immutable Evidence**: Includes increment ID, committed issue scope, durable Slack operator decision URL, timestamp, demo artifacts, and known limitations.
- **Deployment Separation**: Tagging an accepted sprint checkpoint creates an immutable release record but does **not** authorize or trigger production deployment.

---

## ✅ Qualifying Full-Suite Checkpoint Evidence

Release tags require a successful manual or scheduled run of the repository's
`.github/workflows/ci.yml` at the exact current default-branch commit. A green
pull-request run is not qualifying evidence because pull requests intentionally
run focused and static checks instead of the complete unit suite.

For a manual checkpoint, resolve the current default branch and commit, then
dispatch the workflow by its file identity:

```bash
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)"
TARGET_COMMIT="$(gh api "repos/$REPO/commits/$DEFAULT_BRANCH" --jq .sha)"
gh workflow run ci.yml --ref "$DEFAULT_BRANCH"
```

After the run succeeds, obtain its canonical URL and confirm that GitHub reports
the same commit and workflow database identity:

```bash
gh run list \
  --workflow ci.yml \
  --event workflow_dispatch \
  --branch "$DEFAULT_BRANCH" \
  --commit "$TARGET_COMMIT" \
  --status success \
  --limit 1 \
  --json url,headSha,workflowDatabaseId
```

Copy the returned `url` as the checkpoint run URL. The release helpers
independently re-resolve the live default-branch head and the stable workflow ID
for `.github/workflows/ci.yml`; if the branch advanced, the run is ambiguous, or
any identity differs, they refuse the release and a fresh checkpoint is needed.

---

## 🚀 Release Procedure for Factory Agents

To generate `CHANGELOG.md` and cut a SemVer release tag:

1. **Generate `CHANGELOG.md`**:
   ```bash
   python3 scripts/release.py --generate-changelog
   ```

2. **Preview Release Tag**:
   ```bash
   python3 scripts/release.py \
     --tag v0.1.0 \
     --commit <40-character default-branch SHA> \
     --checkpoint-run-url <canonical GitHub Actions run URL> \
     --dry-run
   ```

3. **Cut Release Tag**:
   ```bash
   python3 scripts/release.py \
     --tag v0.1.0 \
     --commit <40-character default-branch SHA> \
     --checkpoint-run-url <canonical GitHub Actions run URL>
   ```

4. **Verify Release Hygiene**:
   ```bash
   python3 scripts/release.py --check
   ```
