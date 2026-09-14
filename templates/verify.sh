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
# [self-hosted, macOS, ARM64, aru-ci] runners and on GitHub-hosted runners.
# The governed workflow declares which runner profile this repository uses; the
# governance section below refuses any workflow that contradicts it. The first
# section runs before anything else and regardless of what changed: it verifies
# every Factory-managed file against `.aru/manifest.json`.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

section() { printf '\n=== %s ===\n' "$1"; }
fail() { printf '::error::%s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Always, first: Factory-managed files match the committed manifest
# ---------------------------------------------------------------------------
section "Managed file integrity"
[ -f .aru/manifest.json ] \
  || fail ".aru/manifest.json is missing; regenerate it with init_project.py --sync from the Factory checkout, never by hand"
integrity_status=0
python3 - <<'PY' || integrity_status=$?
import hashlib, json, os, stat, sys
from pathlib import Path

SCHEMA = "aru.managed-files/v1"
LIMIT = 2 * 1024 * 1024
MALFORMED, MISMATCH = 2, 3

def refuse(code, message):
    print(message, file=sys.stderr)
    raise SystemExit(code)

def read(relative):
    path = Path(relative)
    if any(p.is_symlink() for p in (path, *path.parents) if str(p) not in ("", ".")):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        meta = os.fstat(handle.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > LIMIT:
            return None
        data = handle.read(LIMIT + 1)
        return data if len(data) <= LIMIT else None

try:
    manifest = json.loads(Path(".aru/manifest.json").read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    refuse(MALFORMED, f"manifest is unreadable or not JSON: {exc}")
files = manifest.get("files") if isinstance(manifest, dict) else None
if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
        or not isinstance(files, dict) or not files
        or not isinstance(manifest.get("factory_version"), str)
        or not isinstance(manifest.get("runner_profile"), str)):
    refuse(MALFORMED, "manifest is malformed")
def is_hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))

def forbid(relative):
    if (not isinstance(relative, str) or relative == ".aru/manifest.json"
            or relative.startswith(("/", "../")) or "/../" in relative):
        refuse(MALFORMED, f"manifest names a path it may not: {relative!r}")

def extract_block(text, begin, end):
    # Same rules as scripts/manifest.py::extract_block: exactly one begin line and
    # one end line, begin first; both lines included; one trailing newline.
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == begin]
    ends = [i for i, line in enumerate(lines) if line == end]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    return "\n".join(lines[starts[0]:ends[0] + 1]) + "\n"

findings = []
for relative in sorted(files):
    expected = files[relative]
    if not isinstance(relative, str) or not is_hash(expected):
        refuse(MALFORMED, f"manifest entry for {relative!r} is malformed")
    forbid(relative)
    data = read(relative)
    if data is None:
        findings.append(f"{relative}: missing, unreadable, not a regular file, or oversized")
    elif hashlib.sha256(data).hexdigest() != expected:
        findings.append(f"{relative}: content differs from the manifest")
blocks = manifest.get("blocks", {})
if not isinstance(blocks, dict):
    refuse(MALFORMED, "manifest blocks is malformed")
for relative in sorted(blocks):
    entry = blocks[relative]
    if (not isinstance(relative, str) or not isinstance(entry, dict)
            or set(entry) != {"begin", "end", "sha256"} or not is_hash(entry["sha256"])
            or not all(isinstance(entry[k], str) and entry[k].strip() for k in ("begin", "end"))
            or entry["begin"] == entry["end"] or relative in files):
        refuse(MALFORMED, f"manifest block entry for {relative!r} is malformed")
    forbid(relative)
    data = read(relative)
    if data is None:
        findings.append(f"{relative}: missing, unreadable, not a regular file, or oversized")
        continue
    block = extract_block(data.decode("utf-8", "replace"), entry["begin"], entry["end"])
    if block is None:
        findings.append(f"{relative}: managed block markers are missing or duplicated")
    elif hashlib.sha256(block.encode("utf-8")).hexdigest() != entry["sha256"]:
        findings.append(f"{relative}: managed block differs from the manifest")
for finding in findings:
    print(finding, file=sys.stderr)
if findings:
    raise SystemExit(MISMATCH)
print(f"{len(files)} managed files and {len(blocks)} managed blocks match .aru/manifest.json "
      f"(factory {manifest['factory_version']}, profile {manifest['runner_profile']})")
PY
case "${integrity_status}" in
  0) ;;
  2) fail ".aru/manifest.json is malformed; regenerate it with init_project.py --sync from the Factory checkout, never by hand" ;;
  3) fail "Factory-managed files diverge from .aru/manifest.json (listed above). Regenerate both with init_project.py --sync from the Factory checkout; a hand-edited manifest is a failing check, not a customization. This detects drift and accidental edits; it cannot stop a head that rewrites .aru/verify.sh itself, which aru-merge-policy and the Factory's merge_pr.py judge separately" ;;
  *) fail "managed file integrity check did not complete (exit ${integrity_status})" ;;
esac

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

scope_dir="$(mktemp -d "${TMPDIR:-/tmp}/aru-verify.XXXXXX")" \
  || fail "could not create changed-path workspace"
cleanup_scope() { rm -rf -- "${scope_dir}"; }
trap cleanup_scope EXIT
evidence_file="${scope_dir}/changed-paths.z"
display_file="${scope_dir}/changed-paths.display"
governance_flag="${scope_dir}/governance-touched"

if [ -n "${base}" ]; then
  scope="diff ${base_ref} (${base}) ...HEAD"
  evidence_kind="diff"
  if ! git -c core.fsmonitor=false diff --name-status -z --find-renames \
    --find-copies-harder --diff-filter=ACDMRT "${base}...HEAD" -- > "${evidence_file}"
  then
    fail "changed-path evidence is unavailable"
  fi
else
  # No trustworthy comparison base: fail upward to the whole tracked tree
  # rather than silently verifying nothing.
  scope="full tracked tree (no comparison base resolved)"
  evidence_kind="tracked"
  if ! git -c core.fsmonitor=false ls-files -z > "${evidence_file}"; then
    fail "tracked-tree evidence is unavailable"
  fi
fi

if ! python3 - "${evidence_kind}" "${evidence_file}" "${display_file}" "${governance_flag}" <<'PY'
import json
import os
import sys
from pathlib import Path


def malformed():
    raise SystemExit("changed-path evidence is malformed")


kind, evidence_name, display_name, governance_name = sys.argv[1:]
data = Path(evidence_name).read_bytes()
if not data or not data.endswith(b"\0"):
    malformed()
tokens = data[:-1].split(b"\0")

paths = []
if kind == "tracked":
    if any(not path for path in tokens):
        malformed()
    paths = tokens
elif kind == "diff":
    index = 0
    while index < len(tokens):
        status = tokens[index]
        code = status[:1]
        if code in {b"R", b"C"}:
            if len(status) == 1 or not status[1:].isdigit():
                malformed()
            arity = 2
        elif code in {b"A", b"D", b"M", b"T"}:
            if len(status) != 1:
                malformed()
            arity = 1
        else:
            malformed()
        record_paths = tokens[index + 1 : index + 1 + arity]
        if len(record_paths) != arity or any(not path for path in record_paths):
            malformed()
        paths.extend(record_paths)
        index += 1 + arity
else:
    malformed()

paths = sorted(set(paths))
if not paths:
    raise SystemExit("no changed paths resolved; refusing to report a vacuous pass")

governance = any(
    path.startswith((b".aru/", b".github/"))
    or path in {b"AGENTS.md", b".gitignore"}
    for path in paths
)
with Path(display_name).open("w", encoding="ascii", newline="\n") as display:
    for path in paths:
        display.write(json.dumps(os.fsdecode(path), ensure_ascii=True) + "\n")
if governance:
    Path(governance_name).touch()
PY
then
  fail "changed-path evidence is malformed"
fi

section "Verification scope"
echo "head:  $(git rev-parse HEAD)"
echo "scope: ${scope}"
sed 's/^/  /' "${display_file}"

# ---------------------------------------------------------------------------
# Always: no credential-shaped literal enters the repository
# ---------------------------------------------------------------------------
section "Secret scan"
secret_re="gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{32,}|sk-proj-[A-Za-z0-9_-]{20,}|[A-Z0-9_]*API_SECRET[A-Z0-9_]*[[:space:]]*=[[:space:]]*['\"]?[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----"
scan_file="${scope_dir}/secret-scan.tmp"
if [ -n "${base}" ]; then
  # Collect first so an empty filter result cannot conceal a Git read failure.
  diff_file="${scope_dir}/secret-diff.tmp"
  if ! git -c core.fsmonitor=false diff -a "${base}...HEAD" -- > "${diff_file}"; then
    fail "secret-scan content is unavailable (git diff failed)"
  fi
  if LC_ALL=C grep -a -E '^\+' "${diff_file}" > "${scan_file}"; then
    :
  else
    scan_status="$?"
    # A mode-only change legitimately has no added lines.
    [ "${scan_status}" -eq 1 ] || fail "secret-scan added-line extraction failed (exit ${scan_status})"
  fi
else
  # Inspect the verified commit even if local files or the index have changed.
  if git -c core.fsmonitor=false grep -a -h -e '' HEAD -- . > "${scan_file}"; then
    :
  else
    scan_status="$?"
    # A tracked tree containing only empty files has no matching lines.
    [ "${scan_status}" -eq 1 ] || fail "secret-scan content is unavailable (git grep exit ${scan_status})"
  fi
fi
if LC_ALL=C grep -a -Eq "${secret_re}" "${scan_file}"; then
  fail "credential-shaped literal found in the verified content"
else
  scan_status="$?"
  [ "${scan_status}" -eq 1 ] || fail "secret-scan failed (exit ${scan_status})"
fi
echo "no credential-shaped literal found"

# ---------------------------------------------------------------------------
# Governance / workflow / hook invariants
# ---------------------------------------------------------------------------
if [ -f "${governance_flag}" ]; then
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
  profile_count="$(grep -c '^# aru-runner-profile: ' "${workflow}" || true)"
  [ "${profile_count}" = "1" ] \
    || fail "governed workflow must declare exactly one '# aru-runner-profile:' line"
  profile="$(sed -n 's/^# aru-runner-profile: //p' "${workflow}")"
  case "${profile}" in
    self-hosted-mac)
      expected_runs_on='runs-on: [self-hosted, macOS, ARM64, aru-ci]'
      profile_forbidden=(
        'runs-on:[[:space:]]*ubuntu'
        'runs-on:[[:space:]]*macos-'
        'runs-on:[[:space:]]*windows'
      )
      ;;
    github-hosted)
      expected_runs_on='runs-on: ubuntu-latest'
      # A hosted-account repository must never reach a personal machine.
      profile_forbidden=(
        'self-hosted'
        'runs-on:[[:space:]]*macos-'
        'runs-on:[[:space:]]*windows'
      )
      ;;
    *)
      fail "governed workflow declares an unknown runner profile: ${profile}"
      ;;
  esac
  # Validate the active runs-on value, not any text occurrence: a commented copy
  # of the expected target must not license a different or additional runner.
  active_runs_on="$(sed -E 's/[[:space:]]*#.*$//' "${workflow}" \
    | grep -E '^[[:space:]]*runs-on:' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | sort -u || true)"
  [ "${active_runs_on}" = "${expected_runs_on}" ] \
    || fail "the ${profile} runner profile requires exactly one active ${expected_runs_on}"
  grep -Fq 'name: aru-governed-pr' "${workflow}" \
    || fail "governed workflow must publish the aru-governed-pr check name"
  grep -Fq 'bash .aru/verify.sh' "${workflow}" \
    || fail "governed workflow must run .aru/verify.sh"
  grep -Fq 'enforce_touches.py' "${workflow}" \
    || fail "governed workflow must enforce touches: against the actual diff"
  echo "governed workflow: ${profile} profile, check name, verify.sh, touches enforcement"

  for forbidden in \
    "${profile_forbidden[@]}" \
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
  echo "no cross-profile runner, cache, artifact, deployment secret, or write permission"

  vendored="$(git ls-files -- '.aru/**' | grep -E '/(merge_pr|create_pr|create_branch|claim_issue|check_ci|fetch_next_work|fetch_pr_feedback|triage_backlog|init_project|cleanup_worktrees|revert_merge|merge_state|common)\.py$' || true)"
  [ -z "${vendored}" ] || fail "Factory lifecycle scripts must not be vendored: ${vendored}"
  if [ -e .aru/skills ]; then
    fail "Factory skills must not be vendored under .aru/skills"
  fi
  echo "no vendored Factory lifecycle scripts or skills"
fi

section "Consumer verification"
[ -f .aru/verify-project.sh ] && [ -x .aru/verify-project.sh ] \
  || fail ".aru/verify-project.sh must exist and be executable; preserve the project's checks when upgrading"
if ! ./.aru/verify-project.sh; then
  fail "consumer verification failed"
fi

section "Result"
echo "proportional verification passed for ${scope}"
