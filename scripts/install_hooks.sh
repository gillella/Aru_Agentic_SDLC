#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd "${script_dir}/../hooks" && pwd)"
repo_root="$(git rev-parse --show-toplevel)"
hooks_dir="$(git -C "${repo_root}" rev-parse --git-path hooks)"
[[ "${hooks_dir}" = /* ]] || hooks_dir="${repo_root}/${hooks_dir}"
mkdir -p "${hooks_dir}"
legacy_marker="Aru_Agentic_SDLC pre-push hook"

if [[ -e "${hooks_dir}/pre-push" && ! -L "${hooks_dir}/pre-push" ]]; then
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
chmod +x "${hooks_dir}/pre-push" "${hooks_dir}/enforce_touches.py"
echo "installed Aru hooks in ${hooks_dir}"
