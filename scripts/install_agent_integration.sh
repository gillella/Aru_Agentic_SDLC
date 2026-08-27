#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
aru_home="$(cd "${script_dir}/.." && pwd)"
skills=(
  address-pr-feedback
  create-github-issue
  implement-next-issue
  init-agent-project
  remediate-ci-failure
  triage-backlog
)
targets=(
  "${HOME}/.agents/skills"
  "${HOME}/.codex/skills"
  "${HOME}/.cursor/skills"
)

retained() {
  local candidate="$1"
  local name
  for name in "${skills[@]}"; do
    [[ "${candidate}" == "${name}" ]] && return 0
  done
  return 1
}

for target in "${targets[@]}"; do
  mkdir -p "${target}"
  for existing in "${target}"/*; do
    [[ -L "${existing}" ]] || continue
    destination="$(readlink "${existing}")"
    if [[ "${destination}" == "${aru_home}/skills/"* ]]; then
      name="$(basename "${existing}")"
      retained "${name}" || rm -f "${existing}"
    fi
  done
  for name in "${skills[@]}"; do
    source="${aru_home}/skills/${name}"
    destination="${target}/${name}"
    if [[ -e "${destination}" && ! -L "${destination}" ]]; then
      echo "error: refusing to overwrite user-owned ${destination}" >&2
      exit 1
    fi
    rm -f "${destination}"
    ln -s "${source}" "${destination}"
  done
done

echo "installed ${#skills[@]} Aru minimal-kernel skills"
echo "set ARU_SDLC_HOME=${aru_home} in the environment used by your agents"
