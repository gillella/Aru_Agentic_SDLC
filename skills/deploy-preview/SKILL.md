---
name: deploy-preview
description: Deploys merged commits to a preview environment, records the resulting preview URL on the originating issue, and files a governed remediation issue if deployment fails.
triggers:
  - "deploy preview"
  - "preview environment"
  - "deploy merged commit"
do_not_trigger_for:
  - "staging or production releases (use release skill instead)"
---

# Deploy Preview Skill Procedure

This skill defines the declarative workflow for deploying a merged commit to a preview environment and recording the URL on the originating issue.

---

## Procedure Steps

### 1. Verify Merged State
- Confirm that the target commit is merged into `main` (or default branch).
- Retrieve the originating issue number linked to the merged PR (`Closes #X`).

### 2. Trigger Configured CD Pipeline
- Invoke the project's configured CD pipeline (e.g. GitHub Actions `workflow_dispatch` or deployment webhook).
- Never hardcode credentials, PATs, or ad-hoc shell deployment commands inside the skill.

### 3. Record Preview URL on Originating Issue
- Upon successful deployment, capture the preview environment URL.
- Post an issue comment to the originating issue:
  ```bash
  gh issue comment <ISSUE_NUMBER> --body "🚀 **Preview Environment Deployed**: [View Preview](<PREVIEW_URL>)"
  ```

### 4. Handle Deployment Failures (Issue-First Remediation)
- If the deployment pipeline fails:
  1. Do NOT swallow the error or mark deployment as complete.
  2. Immediately file a governed GitHub issue describing the deployment failure:
     - Title: `fix(deploy): preview deployment failed for commit <COMMIT_SHA>`
     - Body: Include run logs, target commit SHA, and link to the failing CD workflow run.
     - Add labels: `type:fix`, `status:ready`, `priority:p1`.
  3. Post a notification on the originating issue referencing the remediation issue.

---

## Extension Path for Stack Packs (Phase 5)

Different tech stacks (Node/Next.js, Python/FastAPI, Go, Rust, static sites) declare their preview deployment configuration in `.github/workflows/deploy-preview.yml`.
This skill remains stack-agnostic by triggering the repository's `.github/workflows/deploy-preview.yml` workflow via `gh workflow run deploy-preview.yml`.
