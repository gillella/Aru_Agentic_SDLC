#!/usr/bin/env bash
# install_local_agent_integrations.sh — wire Aru_Agentic_SDLC skills and native adapters for local coding agents
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SDLC_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"

SDLC_HOME="${DEFAULT_SDLC_HOME}"
TARGET_HOME="${HOME}"

DRY_RUN=false
CHECK_ONLY=false
REPAIR=false

TARGET_CODEX=false
TARGET_CLAUDE=false
TARGET_CURSOR=false
TARGET_ANTIGRAVITY=false
EXPLICIT_AGENT=false
ALL_AGENTS=false

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        DRY_RUN=true
        shift
        ;;
      --check)
        CHECK_ONLY=true
        shift
        ;;
      --repair)
        REPAIR=true
        shift
        ;;
      --all)
        ALL_AGENTS=true
        shift
        ;;
      --codex-only)
        TARGET_CODEX=true
        EXPLICIT_AGENT=true
        shift
        ;;
      --claude-only)
        TARGET_CLAUDE=true
        EXPLICIT_AGENT=true
        shift
        ;;
      --cursor-only)
        TARGET_CURSOR=true
        EXPLICIT_AGENT=true
        shift
        ;;
      --antigravity-only)
        TARGET_ANTIGRAVITY=true
        EXPLICIT_AGENT=true
        shift
        ;;
      --aru-home)
        SDLC_HOME="$2"
        shift 2
        ;;
      --target-home)
        TARGET_HOME="$2"
        shift 2
        ;;
      -h|--help)
        cat <<EOF
Usage: $0 [options]

Options:
  --dry-run          Preview changes without writing to disk
  --check            Check status of agent integrations
  --repair           Repair broken or stale Aru symlinks
  --codex-only       Target Codex integration only
  --claude-only      Target Claude Code integration only
  --cursor-only      Target Cursor integration only
  --antigravity-only Target Antigravity integration only
  --aru-home <path>  Specify Aru_Agentic_SDLC repository root
  --target-home <path> Specify target home directory (overrides \$HOME)
EOF
        exit 0
        ;;
      *)
        echo "error: unknown option $1" >&2
        exit 1
        ;;
    esac
  done
}

parse_args "$@"

# Auto-detect agents if no explicit agent flag was passed
detect_agents() {
  if [[ "${EXPLICIT_AGENT}" == false ]]; then
    if [[ -d "${TARGET_HOME}/.codex" ]] || command -v codex >/dev/null 2>&1; then
      TARGET_CODEX=true
    fi
    if [[ -d "${TARGET_HOME}/.claude" ]] || command -v claude >/dev/null 2>&1; then
      TARGET_CLAUDE=true
    fi
    if [[ -d "${TARGET_HOME}/.cursor" ]] || command -v cursor >/dev/null 2>&1; then
      TARGET_CURSOR=true
    fi
    if [[ -d "${TARGET_HOME}/.gemini/antigravity" || -d "${TARGET_HOME}/.antigravity" ]] || command -v antigravity >/dev/null 2>&1; then
      TARGET_ANTIGRAVITY=true
    fi

    # If no specific agent directory/binary detected at all, default all to true only when ALL_AGENTS is set
    if [[ "${TARGET_CODEX}" == false && "${TARGET_CLAUDE}" == false && "${TARGET_CURSOR}" == false && "${TARGET_ANTIGRAVITY}" == false ]]; then
      if [[ "${ALL_AGENTS}" == true ]]; then
        TARGET_CODEX=true
        TARGET_CLAUDE=true
        TARGET_CURSOR=true
        TARGET_ANTIGRAVITY=true
      fi
    fi
  fi
}

detect_agents

# Gather available skills dynamically from skills/
SKILLS=()
for skill_dir in "${SDLC_HOME}/skills/"*/; do
  if [[ -d "${skill_dir}" ]]; then
    SKILLS+=("$(basename "${skill_dir}")")
  fi
done

link_skill() {
  local name="$1"
  local dest_dir="$2"
  local src="${SDLC_HOME}/skills/${name}"
  local dest="${dest_dir}/${name}"

  if [[ ! -d "${src}" ]]; then
    echo "error: missing skill directory ${src}" >&2
    return 1
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -L "${dest}" ]] || [[ "$(readlink "${dest}")" != "${src}" ]]; then
      echo "[CHECK FAILED] Missing or invalid link: ${dest}"
      return 1
    fi
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would link ${dest} -> ${src}"
    return 0
  fi

  mkdir -p "${dest_dir}"

  if [[ -L "${dest}" ]]; then
    local current_target
    current_target="$(readlink "${dest}")"
    if [[ "${REPAIR}" == true || "${current_target}" != "${src}" ]]; then
      rm -f "${dest}"
    else
      return 0
    fi
  elif [[ -d "${dest}" ]]; then
    local backup="${dest}.pre-aru.$(date +%Y%m%d%H%M%S)"
    mv "${dest}" "${backup}"
    echo "note: preserved existing ${name} skill as ${backup}" >&2
  elif [[ -e "${dest}" ]]; then
    echo "error: refusing to overwrite non-skill path ${dest}" >&2
    return 1
  fi

  ln -s "${src}" "${dest}"
  echo "linked ${dest} -> ${src}"
}

update_managed_block() {
  local target_file="$1"
  local begin_tag="<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
  local end_tag="<!-- END ARU_SDLC_GOVERNANCE -->"

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${target_file}" ]] || ! grep -Fq "${begin_tag}" "${target_file}" || ! grep -Fq "${end_tag}" "${target_file}"; then
      echo "[CHECK FAILED] Missing or malformed governance block in ${target_file}"
      return 1
    fi
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would update managed governance block in ${target_file}"
    return 0
  fi

  local block="$(cat <<EOF
${begin_tag}
# Aru_Agentic_SDLC Governance & Workflows

1. Confirm work originates from a tracked GitHub issue (Issue-First Law).
2. Read and follow matching skills under \`\$ARU_SDLC_HOME/skills/\`:
   - \`run-aru-factory\` — "please continue", work the board, loop mode
   - \`implement-next-issue\` — claim / worktree / implement / PR for an issue
   - \`create-github-issue\` — file work
   - \`code-review\` — review a PR in an isolated worktree
   - \`remediate-ci-failure\` — fix red CI
   - \`address-pr-feedback\` — resolve review comments
3. Execute Git & GitHub actions via \`python3 "\$ARU_SDLC_HOME/scripts/<script>.py"\`.
${end_tag}
EOF
)"

  if grep -Fq "${begin_tag}" "${target_file}"; then
    if ! grep -Fq "${end_tag}" "${target_file}"; then
      echo "[ERROR] ${target_file} contains '${begin_tag}' without matching '${end_tag}'; refusing to modify to prevent data loss." >&2
      return 1
    fi
    local tmp
    tmp="$(mktemp)"
    local inside=0
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == *"${begin_tag}"* ]]; then
        inside=1
        echo "${block}" >> "${tmp}"
      elif [[ "$line" == *"${end_tag}"* ]]; then
        inside=0
      elif [[ $inside -eq 0 ]]; then
        echo "$line" >> "${tmp}"
      fi
    done < "${target_file}"
    cat "${tmp}" > "${target_file}"
    rm -f "${tmp}"
    echo "updated managed governance block in ${target_file}"
  else
    {
      echo ""
      echo "${block}"
    } >> "${target_file}"
    echo "appended managed governance block to ${target_file}"
  fi
}

ensure_env_export() {
  local profile="$1"
  local line="export ARU_SDLC_HOME=\"${SDLC_HOME}\""

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${profile}" ]] || ! grep -Fq "ARU_SDLC_HOME=" "${profile}"; then
      echo "[CHECK FAILED] Missing ARU_SDLC_HOME in ${profile}"
      return 1
    fi
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would ensure ${line} in ${profile}"
    return 0
  fi

  if [[ -f "${profile}" ]]; then
    if ! grep -Fq "ARU_SDLC_HOME=" "${profile}"; then
      echo "" >> "${profile}"
      echo "# Aru_Agentic_SDLC environment" >> "${profile}"
      echo "${line}" >> "${profile}"
      echo "added ARU_SDLC_HOME export to ${profile}"
    fi
  fi
}

copy_commands() {
  local dest_dir="$1"

  if [[ "${CHECK_ONLY}" == true ]]; then
    for cmd in "${SDLC_HOME}/templates/cursor/commands/"*.md; do
      if [[ -f "${cmd}" ]]; then
        local base="$(basename "${cmd}")"
        if [[ ! -f "${dest_dir}/${base}" ]]; then
          echo "[CHECK FAILED] Missing command file ${dest_dir}/${base}"
          return 1
        fi
      fi
    done
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would copy command templates to ${dest_dir}"
    return 0
  fi

  mkdir -p "${dest_dir}"
  for cmd in "${SDLC_HOME}/templates/cursor/commands/"*.md; do
    if [[ -f "${cmd}" ]]; then
      local base="$(basename "${cmd}")"
      local dest_file="${dest_dir}/${base}"
      if [[ -f "${dest_file}" ]] && ! grep -Fq "ARU_SDLC" "${dest_file}"; then
        local backup="${dest_file}.pre-aru.$(date +%Y%m%d%H%M%S)"
        mv "${dest_file}" "${backup}"
        echo "note: preserved pre-existing user command ${dest_file} as ${backup}" >&2
      fi
      cp "${cmd}" "${dest_file}"
      echo "installed command /${base%.md} in ${dest_dir}"
    fi
  done
}

ERRORS=0

echo "=== Aru_Agentic_SDLC Multi-Agent Integration Installer ==="
echo "SDLC Home:   ${SDLC_HOME}"
echo "Target Home: ${TARGET_HOME}"
echo "Mode:        $(if ${DRY_RUN}; then echo "Dry-Run"; elif ${CHECK_ONLY}; then echo "Check-Only"; else echo "Install"; fi)"

# Shared ~/.agents/skills
AGENTS_SKILLS="${TARGET_HOME}/.agents/skills"
for skill in "${SKILLS[@]}"; do
  link_skill "${skill}" "${AGENTS_SKILLS}" || ERRORS=$((ERRORS + 1))
done

# 1. Codex Integration
if [[ "${TARGET_CODEX}" == true ]]; then
  echo "--- Codex Integration ---"
  CODEX_SKILLS="${TARGET_HOME}/.codex/skills"
  for skill in "${SKILLS[@]}"; do
    link_skill "${skill}" "${CODEX_SKILLS}" || ERRORS=$((ERRORS + 1))
  done
  update_managed_block "${TARGET_HOME}/.codex/instructions.md" || ERRORS=$((ERRORS + 1))
else
  echo "Skipping Codex (not requested/detected)"
fi

# 2. Claude Code Integration
if [[ "${TARGET_CLAUDE}" == true ]]; then
  echo "--- Claude Code Integration ---"
  CLAUDE_SKILLS="${TARGET_HOME}/.claude/skills"
  CLAUDE_COMMANDS="${TARGET_HOME}/.claude/commands"
  for skill in "${SKILLS[@]}"; do
    link_skill "${skill}" "${CLAUDE_SKILLS}" || ERRORS=$((ERRORS + 1))
  done
  copy_commands "${CLAUDE_COMMANDS}" || ERRORS=$((ERRORS + 1))
  update_managed_block "${TARGET_HOME}/.claude/CLAUDE.md" || ERRORS=$((ERRORS + 1))
else
  echo "Skipping Claude Code (not requested/detected)"
fi

# 3. Cursor Integration
if [[ "${TARGET_CURSOR}" == true ]]; then
  echo "--- Cursor Integration ---"
  CURSOR_SKILLS="${TARGET_HOME}/.cursor/skills"
  CURSOR_COMMANDS="${TARGET_HOME}/.cursor/commands"
  CURSOR_RULES="${TARGET_HOME}/.cursor/rules"
  for skill in "${SKILLS[@]}"; do
    link_skill "${skill}" "${CURSOR_SKILLS}" || ERRORS=$((ERRORS + 1))
  done
  copy_commands "${CURSOR_COMMANDS}" || ERRORS=$((ERRORS + 1))
  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${CURSOR_RULES}/aru-agentic-sdlc.mdc" ]]; then
      echo "[CHECK FAILED] Missing Cursor rule ${CURSOR_RULES}/aru-agentic-sdlc.mdc"
      ERRORS=$((ERRORS + 1))
    fi
  elif [[ "${DRY_RUN}" == false ]]; then
    mkdir -p "${CURSOR_RULES}"
    cp "${SDLC_HOME}/templates/cursor/rules/aru-agentic-sdlc.mdc" "${CURSOR_RULES}/aru-agentic-sdlc.mdc"
  fi
  update_managed_block "${TARGET_HOME}/.cursor/user-rules-aru-agentic-sdlc.md" || ERRORS=$((ERRORS + 1))
else
  echo "Skipping Cursor (not requested/detected)"
fi

# 4. Antigravity Integration
if [[ "${TARGET_ANTIGRAVITY}" == true ]]; then
  echo "--- Antigravity Integration ---"
  ANTIGRAVITY_SKILLS="${TARGET_HOME}/.gemini/antigravity/skills"
  for skill in "${SKILLS[@]}"; do
    link_skill "${skill}" "${ANTIGRAVITY_SKILLS}" || ERRORS=$((ERRORS + 1))
  done
  update_managed_block "${TARGET_HOME}/.gemini/antigravity/AGENTS.md" || ERRORS=$((ERRORS + 1))
  if [[ -d "${TARGET_HOME}/.antigravity" ]]; then
    for skill in "${SKILLS[@]}"; do
      link_skill "${skill}" "${TARGET_HOME}/.antigravity/skills" || ERRORS=$((ERRORS + 1))
    done
    update_managed_block "${TARGET_HOME}/.antigravity/AGENTS.md" || ERRORS=$((ERRORS + 1))
  fi
else
  echo "Skipping Antigravity (not requested/detected)"
fi

# Shell Environment
ensure_env_export "${TARGET_HOME}/.zshrc" || true
ensure_env_export "${TARGET_HOME}/.bashrc" || true
ensure_env_export "${TARGET_HOME}/.zprofile" || true

if [[ ${ERRORS} -gt 0 ]]; then
  echo "Finished with ${ERRORS} issue(s)."
  exit 1
else
  echo "Installation complete."
  exit 0
fi
