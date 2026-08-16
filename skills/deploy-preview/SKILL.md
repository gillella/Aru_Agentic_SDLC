---
name: deploy-preview
description: Deploys merged commits to preview and records evidence-backed, audit-only GitHub promotion state with a reversible issue trail.
triggers:
  - "deploy preview"
  - "preview environment"
  - "deploy merged commit"
  - "promote to staging"
  - "promote to production"
  - "reverse promotion"
---

# Deploy Preview Skill Procedure

This skill defines the declarative workflow for deploying a merged commit to a preview environment, recording the URL on the originating issue, and ensuring failures are attached to the Project Board for remediation.

---

## Governed Execution

All preview deployment operations MUST be executed via the governed helper script rather than raw, unvalidated `gh` CLI commands:

```bash
python3 "$ARU_SDLC_HOME/scripts/deploy_preview.py" --commit <COMMIT_SHA> [--issue <ISSUE_NUMBER>]
```

Audit-only promotion state after preview MUST use the governed promotion
helper. Raw workflow dispatches, deployment API calls, and console environment
changes are not a sanctioned promotion path:

```bash
python3 "$ARU_SDLC_HOME/scripts/promote.py" \
  --commit <MERGED_COMMIT_SHA> \
  --checkpoint <ckpt/PR-SHA7> \
  --issue <INCLUDED_ISSUE> [--issue <ANOTHER_INCLUDED_ISSUE> ...] \
  --from-environment <preview|staging> \
  --to-environment <staging|production> \
  --evidence-run <SUCCESSFUL_SOURCE_SMOKE_RUN> \
  [--dry-run]
```

Repeat `--issue` for every issue in the checkpoint annotation. Add `--dry-run`
to validate the complete contract without dispatching the promotion workflow.

> **Scope boundary:** issue #110 promotion means governed GitHub Environment
> and Deployment state plus its evidence trail. It does not deploy, copy,
> rebuild, or prove that a runnable build moved between hosting environments.
> Real immutable-artifact promotion through authoritative staging and
> production URLs is tracked by issue #234.

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
- Upon successful deployment, the helper downloads metadata uploaded by that exact run and verifies its run ID, commit SHA, repository identity, and URL against the authoritative Pages API base URL (including a configured custom domain). Logs and user-supplied arbitrary hosts are not trusted.
- It posts a formatted comment to the originating issue with the preview link and commit details.

### 4. Handle Deployment Failures (Issue-First Remediation)
- If the deployment pipeline fails:
  1. The helper files a governed GitHub issue describing the deployment failure:
     - Title: `fix(deploy): preview deployment failed for commit <COMMIT_SHA>`
     - Body: Includes the exact durable commit marker, failure stage, workflow-run URL when one exists, acceptance criteria, and verification commands. Raw logs and secrets are not copied.
     - Labels: `type:fix`, `priority:p1`.
  2. The new issue is immediately attached to the governed Project Board as `Ready` via `update_issue_status.py --require-board`.
  3. A notification comment is posted to the originating issue linking to the remediation issue.

### 5. Record Governed Environment State (Audit-Only)

- Record only adjacent forward states: `preview -> staging`, then
  `staging -> production`.
- Supply the published checkpoint tag and every included issue. The helper
  verifies that the checkpoint resolves to the exact merged commit and that its
  annotation names every supplied issue.
- Supply a successful prior-stage Actions run. A green `Deploy Preview` run,
  including the #111 smoke and E2E job, is required before recording staging.
  A successful prior audit-only staging record is required before recording
  production. Later audit-only evidence proves GitHub state and the exact
  commit; it is not hosted smoke evidence.
- The helper dispatches `.github/workflows/promote.yml`, correlates the exact
  new run, waits for terminal success, and only then records the audit-only
  scope, commit, checkpoint, included issues, prior-stage evidence, promotion
  run, and GitHub Deployment state on every issue.
- To reverse state, use the inverse adjacent transition (`production ->
  staging` or `staging -> preview`) for the previously known-good checkpoint
  and add `--reverse-of <PRIOR_PROMOTION_RUN>`. The referenced run must be a
  successful governed promotion into the state being reversed. If source
  history itself must be rolled back, first use the governed `revert_merge.py`
  path and record its resulting checkpoint; never rewrite GitHub history.
- The workflow run URL is an audit log link, not an application environment
  URL. No successful issue record may claim provider deployment, immutable
  artifact movement, or staging/production availability.

---
## First-Stack Implementation & Extension Path

### 1. First-Stack Preview Slice (Python / Static HTML / GitHub Pages)
- Default CD pipeline: `.github/workflows/deploy-preview.yml`.
- Scaffolding: Generated automatically during `init_project.py` bootstrap.
- Operation: The local governed helper ensures Pages is configured through the operator credential. A read-only job then checks out the trusted default branch, refreshes and validates the exact merged target SHA, and uses the trusted `build_preview.py` to copy only non-symlink public assets into a contained `dist/`. A separate Pages/OIDC job deploys the artifact and uploads exact-run metadata for `deploy_preview.py` to verify before commenting.
- GitHub Pages is the only runnable target in this slice. The `staging` and
  `production` names used by `promote.py` are governed GitHub state labels,
  not additional Pages sites or hosting environments.

### 2. Extension Path for Additional Stack Packs (Phase 5)
Different tech stacks (Node/Next.js on Vercel/Cloudflare, Python/FastAPI on Fly.io, Go on AWS ECS, Docker containers) declare their preview deployment configuration in `.github/workflows/deploy-preview.yml`.
This skill remains stack-agnostic by triggering the repository's `.github/workflows/deploy-preview.yml` workflow via `deploy_preview.py`, extracting the generated URL, and recording it on the originating issue.
