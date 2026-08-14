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
- Confirm that the target commit is merged into `main` (or default branch).
- Identify the originating issue number linked to the merged PR (`Closes #X`).

### 2. Dispatch Configured CD Pipeline
- The helper dispatches the project's CD workflow (`.github/workflows/deploy-preview.yml`) for the **exact merged commit SHA** (`--ref <COMMIT_SHA>`).
- It watches the dispatched workflow run until completion (`gh run watch`) to ensure errors are captured deterministically.
- Never hardcode credentials, PATs, or ad-hoc shell deployment commands inside the skill.

### 3. Record Preview URL on Originating Issue
- Upon successful deployment, the helper captures the preview environment URL.
- It posts a formatted comment to the originating issue with the preview link and commit details.

### 4. Handle Deployment Failures (Issue-First Remediation)
- If the deployment pipeline fails:
  1. The helper files a governed GitHub issue describing the deployment failure:
     - Title: `fix(deploy): preview deployment failed for commit <COMMIT_SHA>`
     - Body: Includes run logs, target commit SHA, acceptance criteria, and verification commands.
     - Labels: `type:fix`, `priority:p1`.
  2. The new issue is immediately attached to the governed Project Board as `Ready` via `update_issue_status.py --require-board`.
  3. A notification comment is posted to the originating issue linking to the remediation issue.

---
## First-Stack Implementation & Extension Path

### 1. First-Stack Slice (Python / Static HTML)
- Default CD pipeline: `.github/workflows/deploy-preview.yml`.
- Scaffolding: Generated automatically during `init_project.py` bootstrap.
- Operation: Checks out the specific merged commit SHA ref, builds static/Python assets into `dist/`, outputs preview environment URL to `$GITHUB_OUTPUT` and `$GITHUB_STEP_SUMMARY`, which `deploy_preview.py` parses and posts to the originating issue.

### 2. Extension Path for Additional Stack Packs (Phase 5)
Different tech stacks (Node/Next.js on Vercel/Cloudflare, Python/FastAPI on Fly.io, Go on AWS ECS, Docker containers) declare their preview deployment configuration in `.github/workflows/deploy-preview.yml`.
This skill remains stack-agnostic by triggering the repository's `.github/workflows/deploy-preview.yml` workflow via `deploy_preview.py`, extracting the generated URL, and recording it on the originating issue.
