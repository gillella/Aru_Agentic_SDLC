#!/usr/bin/env bash
# line-ceiling: 779
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
STOP_LOOP=false
RESUME_LOOP=false
ENABLE_NATIVE_WAKE=false
DISABLE_NATIVE_WAKE=false
PROJECT_PATH=""
REASON=""

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
      --stop-loop)
        STOP_LOOP=true
        shift
        ;;
      --resume-loop)
        RESUME_LOOP=true
        shift
        ;;
      --reason)
        REASON="$2"
        shift 2
        ;;
      --enable-native-wake)
        ENABLE_NATIVE_WAKE=true
        shift
        ;;
      --disable-native-wake)
        DISABLE_NATIVE_WAKE=true
        shift
        ;;
      --project)
        PROJECT_PATH="$2"
        shift 2
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
  --stop-loop        Persist explicit operator stop (optionally --project, --reason)
  --resume-loop      Clear explicit operator stop (optionally --project, --reason)
  --reason <token>   Bounded pause reason token for stop/resume
  --enable-native-wake  Opt-in vendor wake for --project (absolute path)
  --disable-native-wake Remove opt-in wake for --project
  --project <path>   Absolute project path for stop/wake scoping
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
  if [[ "${STOP_LOOP}" == true && "${RESUME_LOOP}" == true ]]; then
    echo "error: cannot specify both --stop-loop and --resume-loop" >&2
    exit 1
  fi
  if [[ "${ENABLE_NATIVE_WAKE}" == true && "${DISABLE_NATIVE_WAKE}" == true ]]; then
    echo "error: cannot specify both --enable-native-wake and --disable-native-wake" >&2
    exit 1
  fi
}

parse_args "$@"

# `--all` is an explicit contract, not a fallback for failed detection.  The
# wrapper's documented no-argument mode passes this flag, so host binaries must
# never narrow an all-agent install to whichever products happen to be found.
detect_agents() {
  if [[ "${ALL_AGENTS}" == true ]]; then
    TARGET_CODEX=true
    TARGET_CLAUDE=true
    TARGET_CURSOR=true
    TARGET_ANTIGRAVITY=true
  elif [[ "${EXPLICIT_AGENT}" == false ]]; then
    if [[ -d "${TARGET_HOME}/.codex" ]] || command -v codex >/dev/null 2>&1; then
      TARGET_CODEX=true
    fi
    if [[ -d "${TARGET_HOME}/.claude" ]] || command -v claude >/dev/null 2>&1; then
      TARGET_CLAUDE=true
    fi
    if [[ -d "${TARGET_HOME}/.cursor" ]] || command -v cursor >/dev/null 2>&1 || command -v cursor-agent >/dev/null 2>&1; then
      TARGET_CURSOR=true
    fi
    if [[ -d "${TARGET_HOME}/.gemini/antigravity" || -d "${TARGET_HOME}/.antigravity" ]] || command -v antigravity >/dev/null 2>&1 || command -v agy >/dev/null 2>&1; then
      TARGET_ANTIGRAVITY=true
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
  local template_file="${2:-}"
  local begin_tag="<!-- BEGIN ARU_SDLC_GOVERNANCE -->"
  local end_tag="<!-- END ARU_SDLC_GOVERNANCE -->"

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${target_file}" ]] || ! grep -Fq "${begin_tag}" "${target_file}" || ! grep -Fq "${end_tag}" "${target_file}"; then
      echo "[CHECK FAILED] Missing or malformed governance block in ${target_file}"
      return 1
    fi
    if ! grep -Fq "factory-loop.stop" "${target_file}"; then
      echo "[CHECK FAILED] Governance block missing stop-file contract in ${target_file}"
      return 1
    fi
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would update managed governance block in ${target_file}"
    return 0
  fi

  local block
  if [[ -n "${template_file}" && -f "${template_file}" ]]; then
    block="$(cat "${template_file}")"
  else
    echo "error: missing governance template ${template_file}" >&2
    return 1
  fi

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
  local ref_line="export ARU_SDLC_REF=\"${ARU_SDLC_REF:-}\""

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${profile}" ]] || ! grep -Fq "ARU_SDLC_HOME=" "${profile}"; then
      echo "[CHECK FAILED] Missing ARU_SDLC_HOME in ${profile}"
      return 1
    fi
    return 0
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would ensure ${line} in ${profile}"
    if [[ -n "${ARU_SDLC_REF:-}" ]]; then
      echo "[DRY-RUN] Would ensure ${ref_line} in ${profile}"
    fi
    return 0
  fi

  if [[ -f "${profile}" ]]; then
    if ! grep -Fq "ARU_SDLC_HOME=" "${profile}"; then
      echo "" >> "${profile}"
      echo "# Aru_Agentic_SDLC environment" >> "${profile}"
      echo "${line}" >> "${profile}"
      echo "added ARU_SDLC_HOME export to ${profile}"
    fi
    if [[ -n "${ARU_SDLC_REF:-}" ]]; then
      if grep -Fq "ARU_SDLC_REF=" "${profile}"; then
        local tmp
        tmp="$(mktemp)"
        awk -v repl="${ref_line}" '
          BEGIN { done=0 }
          /^export ARU_SDLC_REF=/ {
            if (!done) { print repl; done=1 }
            next
          }
          { print }
          END { if (!done) print repl }
        ' "${profile}" > "${tmp}"
        cat "${tmp}" > "${profile}"
        rm -f "${tmp}"
        echo "updated ARU_SDLC_REF in ${profile}"
      else
        echo "${ref_line}" >> "${profile}"
        echo "added ARU_SDLC_REF export to ${profile}"
      fi
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

install_antigravity_workflow() {
  local dest_dir="$1"
  local src="${SDLC_HOME}/templates/integrations/antigravity/workflows/aru-code-loop.md"
  local dest="${dest_dir}/aru-code-loop.md"

  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -f "${dest}" ]]; then
      echo "[CHECK FAILED] Missing Antigravity workflow ${dest}"
      return 1
    fi
    return 0
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would install Antigravity workflow ${dest}"
    return 0
  fi
  mkdir -p "${dest_dir}"
  cp "${src}" "${dest}"
  echo "installed Antigravity workflow ${dest}"
}

codex_automation_id() {
  python3 -c 'import hashlib, sys; print("aru-code-loop-" + hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$1"
}

aru_python_json() {
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would $1 under ${2}/.aru for ${3:-all-projects}"
    return 0
  fi
  python3 - "$@" <<'PY'
import hashlib, json, os, sys
from datetime import datetime, timezone

action, target_home, project = sys.argv[1], sys.argv[2], sys.argv[3]
aru_dir = os.path.join(target_home, ".aru")
os.makedirs(aru_dir, exist_ok=True)
wake_path = os.path.join(aru_dir, "native-wake.json")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def dump(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)

if action in {"enable-wake", "disable-wake"}:
    if not project:
        raise SystemExit("enable/disable wake requires an absolute --project")
    data = load(wake_path, {"projects": {}})
    projects = data.setdefault("projects", {})
    if action == "enable-wake":
        auto_id = "aru-code-loop-" + hashlib.sha256(project.encode()).hexdigest()[:12]
        projects[project] = {
            "enabled": True,
            "updated_at": now,
            "automation_id": auto_id,
            "codex": "thread_heartbeat_template",
            "antigravity": "goal_or_schedule_operator",
            "claude": "session_loop_only",
            "cursor": "session_loop_only",
        }
        dump(wake_path, data)
        print(f"enabled native wake for {project}")
    else:
        projects.pop(project, None)
        dump(wake_path, data)
        print(f"disabled native wake for {project}")
else:
    raise SystemExit(f"unknown json action {action}")
PY
}

write_codex_wake_prompt() {
  local project="$1"
  local auto_id
  auto_id="$(codex_automation_id "${project}")"
  local dest_dir="${TARGET_HOME}/.codex/automations/${auto_id}"
  local dest="${dest_dir}/PROMPT.md"
  local src="${SDLC_HOME}/templates/integrations/codex/aru-code-loop.prompt.md"
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would write Codex wake prompt ${dest} for ${project}"
    return 0
  fi
  mkdir -p "${dest_dir}"
  python3 - "$src" "$dest" "$project" <<'PY'
import sys
src, dest, project = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src, encoding="utf-8").read().replace("{{PROJECT}}", project)
if project not in text:
    raise SystemExit("codex wake prompt lost its project scope")
open(dest, "w", encoding="utf-8").write(text)
print(f"wrote {dest}")
PY
}

set_managed_codex_status() {
  local toml="$1"
  local status="$2"
  if [[ ! -f "${toml}" ]]; then
    return 0
  fi
  if ! grep -Eq '^id = "aru-code-loop(-[0-9a-f]+)?"' "${toml}"; then
    echo "note: refusing to mutate unmanaged ${toml}" >&2
    return 0
  fi
  if [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would set ${toml} status=${status}"
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  awk -v status="${status}" '
    BEGIN { done=0 }
    /^status = "/ {
      if (!done) { print "status = \"" status "\""; done=1; next }
    }
    { print }
    END { if (!done) print "status = \"" status "\"" }
  ' "${toml}" > "${tmp}"
  cat "${tmp}" > "${toml}"
  rm -f "${tmp}"
  echo "set ${toml} status=${status}"
}

managed_codex_tomls_for_project() {
  local project="$1"
  local root="${TARGET_HOME}/.codex/automations"
  if [[ ! -d "${root}" ]]; then
    return 0
  fi
  if [[ -n "${project}" ]]; then
    local auto_id
    auto_id="$(codex_automation_id "${project}")"
    printf '%s\n' "${root}/${auto_id}/automation.toml"
    return 0
  fi
  local toml
  for toml in "${root}"/aru-code-loop/automation.toml "${root}"/aru-code-loop-*/automation.toml; do
    [[ -f "${toml}" ]] && printf '%s\n' "${toml}"
  done
}

pause_managed_codex_heartbeat() {
  local toml
  while IFS= read -r toml; do
    [[ -n "${toml}" ]] && set_managed_codex_status "${toml}" "PAUSED"
  done < <(managed_codex_tomls_for_project "${1:-}")
}

codex_wake_enabled() {
  python3 - "$TARGET_HOME" "$1" <<'PY'
import json, os, sys
home, project = sys.argv[1], sys.argv[2]
path = os.path.join(home, ".aru", "native-wake.json")
if not os.path.isfile(path):
    raise SystemExit(1)
data = json.load(open(path, encoding="utf-8"))
entry = (data.get("projects") or {}).get(project) or {}
raise SystemExit(0 if entry.get("enabled") else 1)
PY
}

enabled_wake_projects() {
  python3 - "$TARGET_HOME" <<'PY'
import json, os, sys
path = os.path.join(sys.argv[1], ".aru", "native-wake.json")
if not os.path.isfile(path):
    raise SystemExit(0)
data = json.load(open(path, encoding="utf-8"))
for project, entry in (data.get("projects") or {}).items():
    if entry.get("enabled"):
        print(project)
PY
}

resume_managed_codex_heartbeat() {
  local project="${1:-}"
  local toml proj
  if [[ -z "${project}" ]]; then
    while IFS= read -r proj; do
      [[ -n "${proj}" ]] && resume_managed_codex_heartbeat "${proj}"
    done < <(enabled_wake_projects)
    return 0
  fi
  if ! codex_wake_enabled "${project}"; then
    echo "note: leaving Codex heartbeat paused for ${project}; native wake is not enabled"
    return 0
  fi
  while IFS= read -r toml; do
    [[ -n "${toml}" ]] && set_managed_codex_status "${toml}" "ACTIVE"
  done < <(managed_codex_tomls_for_project "${project}")
}

apply_continuity_actions() {
  if [[ "${ENABLE_NATIVE_WAKE}" == true || "${DISABLE_NATIVE_WAKE}" == true ]]; then
    if [[ -z "${PROJECT_PATH}" || "${PROJECT_PATH}" != /* ]]; then
      echo "error: --enable-native-wake/--disable-native-wake requires --project <absolute-path>" >&2
      return 1
    fi
  fi
  if [[ -n "${PROJECT_PATH}" && "${PROJECT_PATH}" != /* ]]; then
    echo "error: --project must be an absolute path" >&2
    return 1
  fi
  if [[ "${CHECK_ONLY}" == true ]]; then
    return 0
  fi
  if [[ "${ENABLE_NATIVE_WAKE}" == true ]]; then
    aru_python_json enable-wake "${TARGET_HOME}" "${PROJECT_PATH}" || return 1
    write_codex_wake_prompt "${PROJECT_PATH}" || return 1
  fi
  if [[ "${DISABLE_NATIVE_WAKE}" == true ]]; then
    pause_managed_codex_heartbeat "${PROJECT_PATH}" || return 1
    aru_python_json disable-wake "${TARGET_HOME}" "${PROJECT_PATH}" || return 1
  fi
  if [[ "${STOP_LOOP}" == true ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      echo "[DRY-RUN] Would stop under ${TARGET_HOME}/.aru for ${PROJECT_PATH:-all-projects}"
    else
      local stop_cmd=(python3 "${SDLC_HOME}/scripts/loop_control.py" stop --target-home "${TARGET_HOME}")
      if [[ -n "${PROJECT_PATH}" ]]; then
        stop_cmd+=(--project "${PROJECT_PATH}")
      fi
      if [[ -n "${REASON}" ]]; then
        stop_cmd+=(--reason "${REASON}")
      fi
      "${stop_cmd[@]}" || return 1
    fi
    pause_managed_codex_heartbeat "${PROJECT_PATH}" || return 1
  fi
  if [[ "${RESUME_LOOP}" == true ]]; then
    if [[ "${DRY_RUN}" == true ]]; then
      echo "[DRY-RUN] Would resume under ${TARGET_HOME}/.aru for ${PROJECT_PATH:-all-projects}"
    else
      local resume_cmd=(python3 "${SDLC_HOME}/scripts/loop_control.py" resume --target-home "${TARGET_HOME}")
      if [[ -n "${PROJECT_PATH}" ]]; then
        resume_cmd+=(--project "${PROJECT_PATH}")
      fi
      if [[ -n "${REASON}" ]]; then
        resume_cmd+=(--reason "${REASON}")
      fi
      "${resume_cmd[@]}" || return 1
    fi
    resume_managed_codex_heartbeat "${PROJECT_PATH}" || return 1
  fi
}

ERRORS=0

echo "=== Aru_Agentic_SDLC Multi-Agent Integration Installer ==="
echo "SDLC Home:   ${SDLC_HOME}"
echo "Target Home: ${TARGET_HOME}"
echo "Mode:        $(if ${DRY_RUN}; then echo "Dry-Run"; elif ${CHECK_ONLY}; then echo "Check-Only"; else echo "Install"; fi)"

if [[ -n "${ARU_SDLC_REF:-}" ]]; then
  if [[ "${ARU_SDLC_REF}" =~ ^- || "${ARU_SDLC_REF}" =~ [[:space:]] ]]; then
    echo "[ERROR] Invalid ARU_SDLC_REF '${ARU_SDLC_REF}': ref cannot start with '-' or contain whitespace." >&2
    exit 1
  fi
  if [[ "${CHECK_ONLY}" == true ]]; then
    echo "[INFO] ARU_SDLC_REF is set to '${ARU_SDLC_REF}'"
  elif [[ "${DRY_RUN}" == true ]]; then
    echo "[DRY-RUN] Would checkout ref '${ARU_SDLC_REF}' in ${SDLC_HOME}"
  else
    echo "Pinning Aru_Agentic_SDLC at ${SDLC_HOME} to ref '${ARU_SDLC_REF}'..."
    if ! git -C "${SDLC_HOME}" rev-parse --verify "${ARU_SDLC_REF}^{commit}" >/dev/null 2>&1; then
      git -C "${SDLC_HOME}" fetch --tags origin 2>/dev/null || true
      if ! git -C "${SDLC_HOME}" rev-parse --verify "${ARU_SDLC_REF}^{commit}" >/dev/null 2>&1; then
        echo "[ERROR] Could not resolve ref '${ARU_SDLC_REF}' in ${SDLC_HOME}." >&2
        exit 1
      fi
    fi
    if ! git -C "${SDLC_HOME}" checkout "${ARU_SDLC_REF}" 2>/dev/null && ! git -C "${SDLC_HOME}" checkout --detach "${ARU_SDLC_REF}" 2>/dev/null; then
      echo "[ERROR] Could not checkout ref '${ARU_SDLC_REF}' in ${SDLC_HOME}." >&2
      exit 1
    fi
    if [[ "${ARU_SDLC_REEXEC:-0}" != "1" ]]; then
      export ARU_SDLC_REEXEC=1
      echo "Re-executing installer from checked-out ref '${ARU_SDLC_REF}'..."
      exec "${SDLC_HOME}/scripts/install_local_agent_integrations.sh" "$@"
    fi
  fi
fi

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
  update_managed_block "${TARGET_HOME}/.codex/instructions.md" \
    "${SDLC_HOME}/templates/integrations/codex/instructions.md" || ERRORS=$((ERRORS + 1))
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
  update_managed_block "${TARGET_HOME}/.claude/CLAUDE.md" \
    "${SDLC_HOME}/templates/integrations/claude/CLAUDE.md" || ERRORS=$((ERRORS + 1))
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
  update_managed_block "${TARGET_HOME}/.cursor/user-rules-aru-agentic-sdlc.md" \
    "${SDLC_HOME}/templates/integrations/cursor/governance.md" || ERRORS=$((ERRORS + 1))
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
  update_managed_block "${TARGET_HOME}/.gemini/antigravity/AGENTS.md" \
    "${SDLC_HOME}/templates/integrations/antigravity/AGENTS.md" || ERRORS=$((ERRORS + 1))
  install_antigravity_workflow "${TARGET_HOME}/.gemini/antigravity/workflows" || ERRORS=$((ERRORS + 1))
  if [[ -d "${TARGET_HOME}/.antigravity" ]]; then
    for skill in "${SKILLS[@]}"; do
      link_skill "${skill}" "${TARGET_HOME}/.antigravity/skills" || ERRORS=$((ERRORS + 1))
    done
    update_managed_block "${TARGET_HOME}/.antigravity/AGENTS.md" \
      "${SDLC_HOME}/templates/integrations/antigravity/AGENTS.md" || ERRORS=$((ERRORS + 1))
    install_antigravity_workflow "${TARGET_HOME}/.antigravity/workflows" || ERRORS=$((ERRORS + 1))
  fi
else
  echo "Skipping Antigravity (not requested/detected)"
fi

# Shell Environment
if [[ "${CHECK_ONLY}" != true && "${DRY_RUN}" != true ]]; then
  if [[ ! -f "${TARGET_HOME}/.zshrc" && ! -f "${TARGET_HOME}/.bashrc" && ! -f "${TARGET_HOME}/.zprofile" ]]; then
    if [[ "${SHELL:-}" == *"bash"* ]]; then
      touch "${TARGET_HOME}/.bashrc"
    else
      touch "${TARGET_HOME}/.zshrc"
    fi
  fi
fi

ensure_env_export "${TARGET_HOME}/.zshrc" || true
ensure_env_export "${TARGET_HOME}/.bashrc" || true
ensure_env_export "${TARGET_HOME}/.zprofile" || true

apply_continuity_actions || ERRORS=$((ERRORS + 1))

if [[ ${ERRORS} -gt 0 ]]; then
  echo "Finished with ${ERRORS} issue(s)."
  exit 1
else
  echo "Installation complete."
  exit 0
fi
