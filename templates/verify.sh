#!/usr/bin/env bash
# Repository-owned verification for the exact-head `aru-governed-pr` check.
#
# Keep this proportional: run the smallest set of checks the changed paths
# actually justify. Broad release, deployment, and production suites are
# consumer-owned and live outside the governed merge gate. This script must
# never deploy, restart a service, migrate or mutate the database, publish
# content, send email or applications, or take a trading action.
#
# Written for bash 3.2 so it runs unmodified on the operator-owned
# [self-hosted, macOS, ARM64, aru-ci] runner.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

section() { printf '\n=== %s ===\n' "$1"; }
fail() { printf '::error::%s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Scope: what actually changed on this head
# ---------------------------------------------------------------------------
base=""
if git rev-parse --verify --quiet refs/remotes/origin/HEAD >/dev/null 2>&1; then
  base_ref="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD || true)"
else
  base_ref=""
fi
if [ -z "${base_ref}" ] && git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null 2>&1; then
  base_ref="origin/main"
fi
if [ -n "${base_ref}" ]; then
  base="$(git merge-base "${base_ref}" HEAD 2>/dev/null || true)"
fi

if [ -n "${base}" ]; then
  scope="diff ${base_ref} (${base}) ...HEAD"
  changed="$(git diff --name-status --find-renames --diff-filter=ACDMRT "${base}...HEAD" -- | awk -F'\t' '{for (i=2; i<=NF; i++) print $i}' | sort -u || true)"
else
  # No trustworthy comparison base: fail upward to the whole tracked tree
  # rather than silently verifying nothing.
  scope="full tracked tree (no comparison base resolved)"
  changed="$(git ls-files | sort -u)"
fi

section "Verification scope"
echo "head:  $(git rev-parse HEAD)"
echo "scope: ${scope}"
if [ -z "${changed}" ]; then
  fail "no changed paths resolved; refusing to report a vacuous pass"
fi
printf '%s\n' "${changed}" | sed 's/^/  /'

touched() { printf '%s\n' "${changed}" | grep -Eq "$1"; }

governance_re='^(\.aru/|\.github/|AGENTS\.md$|\.gitignore$)'

# ---------------------------------------------------------------------------
# Always: no credential-shaped literal enters the repository
# ---------------------------------------------------------------------------
section "Secret scan"
secret_re="gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{32,}|sk-proj-[A-Za-z0-9_-]{20,}|(API_SECRET_KEY|JMC_API_SECRET)[[:space:]]*=[[:space:]]*['\"]?[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----"
if [ -n "${base}" ]; then
  scan="$(git diff "${base}...HEAD" -- | grep -E '^\+' || true)"
else
  scan="$(git grep -I -h -e '' -- . || true)"
fi
if printf '%s\n' "${scan}" | grep -Eq "${secret_re}"; then
  fail "credential-shaped literal found in the verified content"
fi
echo "no credential-shaped literal found"

# ---------------------------------------------------------------------------
# Governance / workflow / hook invariants
# ---------------------------------------------------------------------------
if touched "${governance_re}"; then
  section "Governance invariants"

  [ -x .aru/verify.sh ] || fail ".aru/verify.sh must be executable"
  [ -x .aru/hooks/pre-push ] || fail ".aru/hooks/pre-push must be executable"
  [ -x .aru/hooks/enforce_touches.py ] || fail ".aru/hooks/enforce_touches.py must be executable"
  [ -f .aru/lib/touches.py ] || fail ".aru/lib/touches.py (shared touches parser) is missing"
  python3 -m py_compile .aru/lib/touches.py .aru/hooks/enforce_touches.py
  bash -n .aru/verify.sh .aru/hooks/pre-push
  echo "hooks, shared parser, and verify.sh parse and are executable"

  workflow=".github/workflows/governed-pr.yml"
  [ -f "${workflow}" ] || fail "${workflow} is missing"
  grep -Fq 'runs-on: [self-hosted, macOS, ARM64, aru-ci]' "${workflow}" \
    || fail "governed workflow must target exactly [self-hosted, macOS, ARM64, aru-ci]"
  grep -Fq 'name: aru-governed-pr' "${workflow}" \
    || fail "governed workflow must publish the aru-governed-pr check name"
  grep -Fq 'bash .aru/verify.sh' "${workflow}" \
    || fail "governed workflow must run .aru/verify.sh"
  grep -Fq 'enforce_touches.py' "${workflow}" \
    || fail "governed workflow must enforce touches: against the actual diff"
  echo "governed workflow: self-hosted pool, check name, verify.sh, touches enforcement"

  for forbidden in \
    'runs-on:[[:space:]]*ubuntu' \
    'runs-on:[[:space:]]*macos-' \
    'runs-on:[[:space:]]*windows' \
    'pull_request_target' \
    'actions/cache' \
    'upload-artifact' \
    'download-artifact' \
    'environment:' \
    'secrets\.[A-Z_]*(TOKEN|KEY|PASSWORD|SECRET|DEPLOY)'
  do
    if grep -Eq "${forbidden}" "${workflow}"; then
      fail "governed workflow must not contain: ${forbidden}"
    fi
  done
  if grep -Eq '^[[:space:]]*(contents|issues|pull-requests|actions|checks|deployments|packages|id-token):[[:space:]]*(write|admin)' "${workflow}"; then
    fail "governed workflow permissions must stay read-only"
  fi
  echo "no hosted fallback, cache, artifact, deployment secret, or write permission"

  vendored="$(git ls-files -- '.aru/**' | grep -E '/(merge_pr|create_pr|create_branch|claim_issue|check_ci|fetch_next_work|fetch_pr_feedback|triage_backlog|init_project|cleanup_worktrees|revert_merge|review_policy|review_risk|review_evidence|reviewer_probe|merge_state|common)\.py$' || true)"
  [ -z "${vendored}" ] || fail "Factory lifecycle scripts must not be vendored: ${vendored}"
  if [ -e .aru/skills ]; then
    fail "Factory skills must not be vendored under .aru/skills"
  fi
  echo "no vendored Factory lifecycle scripts or skills"
fi

section "Result"
echo "proportional verification passed for ${scope}"
