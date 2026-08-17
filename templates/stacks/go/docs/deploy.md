# Deployment and Promotion Guide

This repository is governed by **Aru_Agentic_SDLC**. Deployments and release promotions are managed through the governed SDLC factory skills rather than ad-hoc scripts or unversioned manual actions.

---

## Governed Workflows

### 1. Preview Deployment
Preview deployments for merged commits are dispatched using the `deploy-preview` skill:

```bash
python3 "$ARU_SDLC_HOME/scripts/deploy_preview.py" --commit <COMMIT_SHA> [--issue <ISSUE_NUMBER>]
```

The preview deployment workflow (`.github/workflows/deploy-preview.yml`) verifies:
- Merged state and commit ancestry on the default branch.
- Availability of required deployment credentials (`PREVIEW_DEPLOY_TOKEN`). If credentials are absent, the workflow fails closed.
- Automatic recording of the preview URL or remediation issue on the Project Board.

### 2. Release and Checkpoints
Releases and version tags are cut from verified merge checkpoints:

```bash
python3 "$ARU_SDLC_HOME/scripts/promote.py" \
  --commit <MERGED_COMMIT_SHA> \
  --checkpoint <ckpt/PR-SHA7> \
  --issue <ISSUE_NUMBER> \
  --from-environment preview \
  --to-environment staging \
  --evidence-run <PREVIEW_RUN_ID>
```

The release workflow (`.github/workflows/release.yml`) builds release artifacts for `go` upon pushing release tags (`v*.*.*`) or explicit dispatch.

---

## Required Secrets & Environment Variables

| Secret / Env Var | Purpose | Required For |
|---|---|---|
| `PREVIEW_DEPLOY_TOKEN` | Token for hosting/preview infrastructure | `.github/workflows/deploy-preview.yml` |
| `RELEASE_TOKEN` | Token for publishing releases / package registry | `.github/workflows/release.yml` |
| `GITHUB_TOKEN` | Repository-scoped token for releases and Pages | Preview & Release |

---

## Guardrails
- **No Direct Deployments**: Never deploy unmerged code or push untracked tags directly to production.
- **Fail-Closed Gate**: All deployment and release workflows fail closed if required credentials are missing.
- **Audit Trail**: Every preview and release event is recorded on the corresponding GitHub Issue and Project Board card.
