---
name: deploy-preview
description: Deploys merged commits to a preview environment, records the resulting preview URL on the originating issue, and files a governed remediation issue on the Project Board if deployment fails.
triggers:
  - "deploy preview"
  - "preview environment"
  - "deploy merged commit"
do_not_trigger_for:
  - "staging or production releases (use release skill instead)"
---

# Deploy Preview Skill Procedure

This skill defines the declarative workflow for deploying a merged commit to a preview environment, recording the URL on the originating issue, and ensuring failures are attached to the Project Board for remediation.

---

## Governed Execution

All preview deployment operations MUST be executed via the governed helper script rather than raw, unvalidated `gh` CLI commands:

```bash
python3 "$ARU_SDLC_HOME/scripts/deploy_preview.py" --commit <COMMIT_SHA> [--issue <ISSUE_NUMBER>]
```

---

## Procedure Steps

### 1. Verify Merged State
- Resolve the repository and default branch from GitHub, refresh that exact remote branch, and confirm that the target commit is its ancestor. A failed refresh is a hard stop; stale local refs are never authoritative.
- Identify the originating issue number linked to the merged PR (`Closes #X`).

### 2. Dispatch Configured CD Pipeline
- The helper dispatches the project's CD workflow (`.github/workflows/deploy-preview.yml`) on the default branch passing the merged commit as input (`-f commit_sha=<COMMIT_SHA>`).
- The workflow validates a full 40-character SHA from a trusted default-branch checkout before checking out target content. Only the read-only build job handles target files; Pages/OIDC permissions exist solely in the later deployment job.
- Before dispatch, the local helper verifies or enables GitHub Pages in workflow mode through the operator's existing `gh` credential. The credential is never written to a generated file or passed to target content.
- The helper polls the newly correlated workflow run with a hard deadline and distinguishes success, failure, cancellation, query failure, and timeout.
- Never hardcode credentials, PATs, or ad-hoc shell deployment commands inside the skill.

### 3. Record Preview URL on Originating Issue
- Upon successful deployment, the helper downloads metadata uploaded by that exact run and verifies its run ID, commit SHA, repository identity, and canonical GitHub Pages URL. Logs and user-supplied arbitrary hosts are not trusted.
- It posts a formatted comment to the originating issue with the preview link and commit details.

### 4. Handle Deployment Failures (Issue-First Remediation)
- If the deployment pipeline fails:
  1. The helper files a governed GitHub issue describing the deployment failure:
     - Title: `fix(deploy): preview deployment failed for commit <COMMIT_SHA>`
     - Body: Includes the exact durable commit marker, failure stage, workflow-run URL when one exists, acceptance criteria, and verification commands. Raw logs and secrets are not copied.
     - Labels: `type:fix`, `priority:p1`.
  2. The new issue is immediately attached to the governed Project Board as `Ready` via `update_issue_status.py --require-board`.
  3. A notification comment is posted to the originating issue linking to the remediation issue.

---
## First-Stack Implementation & Extension Path

### 1. First-Stack Slice (Python / Static HTML / GitHub Pages)
- Default CD pipeline: `.github/workflows/deploy-preview.yml`.
- Scaffolding: Generated automatically during `init_project.py` bootstrap.
- Operation: The local governed helper ensures Pages is configured through the operator credential. A read-only job then checks out the trusted default branch, refreshes and validates the exact merged target SHA, and uses the trusted `build_preview.py` to copy only non-symlink public assets into a contained `dist/`. A separate Pages/OIDC job deploys the artifact and uploads exact-run metadata for `deploy_preview.py` to verify before commenting.

### 2. Extension Path for Additional Stack Packs (Phase 5)
Different tech stacks (Node/Next.js on Vercel/Cloudflare, Python/FastAPI on Fly.io, Go on AWS ECS, Docker containers) declare their preview deployment configuration in `.github/workflows/deploy-preview.yml`.
This skill remains stack-agnostic by triggering the repository's `.github/workflows/deploy-preview.yml` workflow via `deploy_preview.py`, extracting the generated URL, and recording it on the originating issue.
