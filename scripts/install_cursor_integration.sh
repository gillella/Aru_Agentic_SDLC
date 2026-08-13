#!/usr/bin/env bash
# install_cursor_integration.sh — wire Aru_Agentic_SDLC into the local Cursor install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDLC_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"
CURSOR_HOME="${HOME}/.cursor"
AGENTS_SKILLS="${HOME}/.agents/skills"
DEFAULT_ARU_PATH="${SDLC_HOME}"

# Derived from skills/ on disk rather than hand-maintained. The previous
# hardcoded list carried a comment telling the reader to keep it in step with
# skills/; that invariant held only as long as someone remembered it, and it
# failed silently. `run-aru-factory` — the entrypoint every other skill is
# dispatched from — was never added, so no external agent could reach the door
# the docs pointed at (#163). Enumerating makes a new skill reachable by
# existing, which is the only version of this rule that cannot rot.
#
# Built in the main shell, not a subshell: a pipeline or process substitution
# would make the `exit` below terminate the subshell and let the install
# continue with a partial list, which is the failure this whole block exists
# to prevent.
SKILLS=()
skill_count=0
for skill_dir in "${SDLC_HOME}"/skills/*/; do
  [[ -d "${skill_dir}" ]] || continue
  if [[ ! -f "${skill_dir}SKILL.md" ]]; then
    echo "error: ${skill_dir} has no SKILL.md" >&2
    echo "       every directory under skills/ must define one, or it is not a skill" >&2
    exit 1
  fi
  SKILLS+=("$(basename "${skill_dir}")")
  skill_count=$((skill_count + 1))
done

# Counted separately rather than via ${#SKILLS[@]}: under `set -u`, bash 3.2
# (still the /bin/bash on macOS) treats an empty array as unset and aborts
# with "unbound variable" instead of reporting the real problem. A maintainer
# on macOS would see a bash internals error where CI sees a clean message.
if [[ ${skill_count} -eq 0 ]]; then
  echo "error: no skills found under ${SDLC_HOME}/skills" >&2
  exit 1
fi

echo "discovered ${skill_count} skills under ${SDLC_HOME}/skills"

link_skill() {
  local name="$1"
  local src="${SDLC_HOME}/skills/${name}"
  local dest="$2/${name}"

  if [[ ! -d "${src}" ]]; then
    echo "error: missing skill directory ${src}" >&2
    exit 1
  fi

  mkdir -p "$(dirname "${dest}")"
  # Only ever remove a symlink outright. A real directory at this path is
  # somebody's own skill - possibly hand-edited and unversioned - and deleting
  # it to install ours would be irreversible data loss, so it is moved aside
  # and reported instead. Never touch siblings (.skill-lock.json, nested
  # marketplace trees, etc.).
  if [[ -L "${dest}" ]]; then
    rm -f "${dest}"
  elif [[ -d "${dest}" ]]; then
    local backup="${dest}.pre-aru.$(date +%Y%m%d%H%M%S)"
    mv "${dest}" "${backup}"
    echo "note: preserved your existing ${name} skill as ${backup}" >&2
  elif [[ -e "${dest}" ]]; then
    echo "error: refusing to overwrite non-skill path ${dest}" >&2
    exit 1
  fi
  ln -s "${src}" "${dest}"
  echo "linked ${dest} -> ${src}"
}

ensure_env_export() {
  local profile="$1"
  local line="export ARU_SDLC_HOME=\"${DEFAULT_ARU_PATH}\""
  touch "${profile}"
  if grep -Fq "ARU_SDLC_HOME=" "${profile}"; then
    # Replace existing assignment to keep a single source of truth.
    local tmp
    tmp="$(mktemp)"
    awk -v repl="${line}" '
      BEGIN { done=0 }
      /^export ARU_SDLC_HOME=/ {
        if (!done) { print repl; done=1 }
        next
      }
      { print }
      END { if (!done) print repl }
    ' "${profile}" > "${tmp}"
    # Write through the path rather than mv onto it. A dotfile manager often
    # symlinks .zshrc/.bashrc at its own store; mv would replace the symlink
    # with a regular file, silently detaching the profile from the manager and
    # leaving the real file unchanged. Redirection follows the link and keeps
    # the existing inode and permissions.
    cat "${tmp}" > "${profile}"
    rm -f "${tmp}"
    echo "updated ARU_SDLC_HOME in ${profile}"
  else
    {
      echo ""
      echo "# Aru_Agentic_SDLC — Cursor / agent playbook home"
      echo "${line}"
    } >> "${profile}"
    echo "appended ARU_SDLC_HOME to ${profile}"
  fi
}

echo "Installing Cursor integration from ${SDLC_HOME}"

exec "${SCRIPT_DIR}/install_local_agent_integrations.sh" --cursor-only --aru-home "${SDLC_HOME}" "$@"

