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
global_agents="${HOME}/.codex/AGENTS.md"
governance_template="${aru_home}/templates/AGENTS.md"
managed_begin="<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
managed_end="<!-- END ARU_SDLC_GOVERNANCE -->"

retained() {
  local candidate="$1"
  local name
  for name in "${skills[@]}"; do
    [[ "${candidate}" == "${name}" ]] && return 0
  done
  return 1
}

install_global_guidance() {
  local target="$1"
  local template="$2"
  local target_dir
  local temporary

  target_dir="$(dirname "${target}")"
  mkdir -p "${target_dir}"

  if [[ ! -e "${target}" ]]; then
    cp "${template}" "${target}"
    echo "installed managed Aru guidance in ${target}"
    return
  fi

  if grep -Fq "${managed_begin}" "${target}"; then
    if ! grep -Fq "${managed_end}" "${target}"; then
      echo "error: refusing to edit malformed Aru block in ${target}" >&2
      exit 1
    fi
    temporary="$(mktemp)"
    awk -v begin="${managed_begin}" -v end="${managed_end}" -v template="${template}" '
      index($0, begin) {
        while ((getline line < template) > 0) print line
        close(template)
        inside = 1
        next
      }
      index($0, end) { inside = 0; next }
      !inside { print }
    ' "${target}" > "${temporary}"
    cat "${temporary}" > "${target}"
    rm -f "${temporary}"
    echo "updated managed Aru guidance in ${target}"
    return
  fi

  if grep -Fq "# Global Software Development Governance: Aru_Agentic_SDLC" "${target}"; then
    local backup="${target}.pre-aru-v0.2.8.$(date +%Y%m%d%H%M%S)"
    cp -p "${target}" "${backup}"
    cp "${template}" "${target}"
    echo "migrated legacy Aru guidance in ${target}; preserved ${backup}"
    return
  fi

  temporary="$(mktemp)"
  awk 'FNR == 1 && NR != 1 { print "" } { print }' "${target}" "${template}" > "${temporary}"
  cat "${temporary}" > "${target}"
  rm -f "${temporary}"
  echo "appended managed Aru guidance to ${target}"
}

install_global_guidance "${global_agents}" "${governance_template}"

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
