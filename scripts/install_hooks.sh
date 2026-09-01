#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd "${script_dir}/../hooks" && pwd)"
repo_root="$(git rev-parse --show-toplevel)"
git_common_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-common-dir)"
canonical_hooks_dir="${git_common_dir}/hooks"
hooks_dir="$(git -C "${repo_root}" rev-parse --path-format=absolute --git-path hooks)"
if [[ -L "${canonical_hooks_dir}" ]]; then
  echo "error: refusing symbolic-link hooks directory: ${canonical_hooks_dir}" >&2
  exit 1
fi
if [[ "${hooks_dir}" != "${canonical_hooks_dir}" ]]; then
  echo "error: refusing non-canonical core.hooksPath: ${hooks_dir}" >&2
  exit 1
fi
mkdir -p "${hooks_dir}"
legacy_marker="Aru_Agentic_SDLC pre-push hook"

for hook_name in pre-push pre-push.pre-aru enforce_touches.py touches.py; do
  hook_target="${hooks_dir}/${hook_name}"
  if [[ -L "${hook_target}" ]]; then
    echo "error: refusing symbolic-link hook target: ${hook_target}" >&2
    exit 1
  fi
  if [[ -e "${hook_target}" && ! -f "${hook_target}" ]]; then
    echo "error: refusing non-file hook target: ${hook_target}" >&2
    exit 1
  fi
done

if [[ -e "${hooks_dir}/pre-push" ]]; then
  if ! cmp -s "${hooks_dir}/pre-push" "${source_dir}/pre-push"; then
    if grep -Fq "${legacy_marker}" "${hooks_dir}/pre-push"; then
      rm -f "${hooks_dir}/pre-push"
    elif [[ ! -e "${hooks_dir}/pre-push.pre-aru" ]]; then
      mv "${hooks_dir}/pre-push" "${hooks_dir}/pre-push.pre-aru"
    else
      echo "error: existing pre-push and pre-push.pre-aru both need operator review" >&2
      exit 1
    fi
  fi
fi

if [[ -f "${hooks_dir}/pre-push.pre-aru" ]] &&
   grep -Fq "${legacy_marker}" "${hooks_dir}/pre-push.pre-aru"; then
  rm -f "${hooks_dir}/pre-push.pre-aru"
fi

cp "${source_dir}/pre-push" "${hooks_dir}/pre-push"
cp "${source_dir}/enforce_touches.py" "${hooks_dir}/enforce_touches.py"
cp "${script_dir}/touches.py" "${hooks_dir}/touches.py"
chmod +x "${hooks_dir}/pre-push" "${hooks_dir}/enforce_touches.py"
echo "installed Aru hooks in ${hooks_dir}"
