#!/usr/bin/env bash
# install_cursor_integration.sh — wire Aru_Agentic_SDLC into the local Cursor install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDLC_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"
CURSOR_HOME="${HOME}/.cursor"
AGENTS_SKILLS="${HOME}/.agents/skills"
DEFAULT_ARU_PATH="${SDLC_HOME}"

SKILLS=(
  aru-agentic-sdlc
  implement-next-issue
  init-agent-project
  create-github-issue
  code-review
  remediate-ci-failure
  address-pr-feedback
)

link_skill() {
  local name="$1"
  local src="${SDLC_HOME}/skills/${name}"
  local dest="$2/${name}"

  if [[ ! -d "${src}" ]]; then
    echo "error: missing skill directory ${src}" >&2
    exit 1
  fi

  mkdir -p "$(dirname "${dest}")"
  # Only replace our own symlink or an exact same-named skill dir.
  # Never touch siblings (.skill-lock.json, nested marketplace trees, etc.).
  if [[ -L "${dest}" ]]; then
    rm -f "${dest}"
  elif [[ -d "${dest}" ]]; then
    rm -rf "${dest}"
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
    mv "${tmp}" "${profile}"
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

mkdir -p \
  "${CURSOR_HOME}/skills" \
  "${CURSOR_HOME}/commands" \
  "${CURSOR_HOME}/rules" \
  "${AGENTS_SKILLS}"

for skill in "${SKILLS[@]}"; do
  link_skill "${skill}" "${CURSOR_HOME}/skills"
  link_skill "${skill}" "${AGENTS_SKILLS}"
done

# Slash commands
for cmd in "${SDLC_HOME}/templates/cursor/commands/"*.md; do
  base="$(basename "${cmd}")"
  cp "${cmd}" "${CURSOR_HOME}/commands/${base}"
  echo "installed command /${base%.md}"
done

# Paste-ready user rules + optional file-backed global rule
cp "${SDLC_HOME}/templates/cursor/user-rules-aru-agentic-sdlc.md" \
  "${CURSOR_HOME}/user-rules-aru-agentic-sdlc.md"
cp "${SDLC_HOME}/templates/cursor/rules/aru-agentic-sdlc.mdc" \
  "${CURSOR_HOME}/rules/aru-agentic-sdlc.mdc"
echo "wrote ${CURSOR_HOME}/user-rules-aru-agentic-sdlc.md"
echo "wrote ${CURSOR_HOME}/rules/aru-agentic-sdlc.mdc"

# Shell env for interactive + login shells
if [[ -f "${HOME}/.zshrc" || "${SHELL:-}" == *zsh* ]]; then
  ensure_env_export "${HOME}/.zshrc"
fi
if [[ -f "${HOME}/.bashrc" || "${SHELL:-}" == *bash* ]]; then
  ensure_env_export "${HOME}/.bashrc"
fi
# Always ensure .zprofile for GUI-launched apps that read login env on macOS
ensure_env_export "${HOME}/.zprofile"

export ARU_SDLC_HOME="${DEFAULT_ARU_PATH}"

cat <<EOF

Done.

Next steps (required once):
  1. Open Cursor → Customize → Rules → User Rules
  2. Paste contents of:
       ${CURSOR_HOME}/user-rules-aru-agentic-sdlc.md
  3. Open a new Agent chat (skill discovery refreshes on new sessions)
  4. In a governed repo, run /implement-next-issue or say "implement next issue"

Verify:
  echo \$ARU_SDLC_HOME
  ls -l ${CURSOR_HOME}/skills
  ls -l ${AGENTS_SKILLS} | head

Docs: ${SDLC_HOME}/docs/cursor-integration.md
EOF
