#!/usr/bin/env bash
# install_agent_integration.sh — wire Aru_Agentic_SDLC skills and native
# adapters into the local coding agents (Cursor, Codex, Claude Code,
# Antigravity). Vendor-neutral: this is not a Cursor-only script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDLC_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Validate the discovered surface before delegating. Keeping this in the
# wrapper makes malformed installs fail before the delegated script can write
# a partial integration.
SKILLS=()
skill_count=0
for skill_dir in "${SDLC_HOME}"/skills/*/; do
  [[ -d "${skill_dir}" ]] || continue
  if [[ ! -f "${skill_dir}SKILL.md" || ! -r "${skill_dir}SKILL.md" ]]; then
    echo "error: ${skill_dir} has no readable SKILL.md" >&2
    echo "       every directory under skills/ must define one, or it is not a skill" >&2
    exit 1
  fi
  SKILLS+=("$(basename "${skill_dir}")")
  skill_count=$((skill_count + 1))
done
if [[ ${skill_count} -eq 0 ]]; then
  echo "error: no skills found under ${SDLC_HOME}/skills" >&2
  exit 1
fi

echo "discovered ${skill_count} skills under ${SDLC_HOME}/skills"

echo "Installing agent integration(s) from ${SDLC_HOME}"

# --agent <cursor|codex|antigravity|claude|all> (default all). A thin
# convenience layer over install_local_agent_integrations.sh's --{name}-only
# switches so operators target one surface without memorizing many flags.
# Plain arrays and case dispatch only: /bin/bash on macOS is 3.2, which has
# no associative arrays.
agent_flag() {
  case "$1" in
    cursor) echo "--cursor-only" ;;
    codex) echo "--codex-only" ;;
    antigravity) echo "--antigravity-only" ;;
    claude) echo "--claude-only" ;;
    all) echo "--all" ;;
    *) echo "__invalid__" ;;
  esac
}

TARGET_FLAGS=()
REST=()
AGENT_GIVEN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      if [[ $# -lt 2 ]]; then
        echo "error: --agent requires a value" >&2
        exit 1
      fi
      AGENT_GIVEN=true
      flag="$(agent_flag "$2")"
      if [[ "${flag}" == "__invalid__" ]]; then
        echo "error: unknown --agent '$2' (expected cursor|codex|antigravity|claude|all)" >&2
        exit 1
      fi
      [[ -n "${flag}" ]] && TARGET_FLAGS+=("${flag}")
      shift 2
      ;;
    --agent=*)
      AGENT_GIVEN=true
      value="${1#--agent=}"
      flag="$(agent_flag "${value}")"
      if [[ "${flag}" == "__invalid__" ]]; then
        echo "error: unknown --agent '${value}' (expected cursor|codex|antigravity|claude|all)" >&2
        exit 1
      fi
      [[ -n "${flag}" ]] && TARGET_FLAGS+=("${flag}")
      shift
      ;;
    *)
      REST+=("$1")
      shift
      ;;
  esac
done
if ! ${AGENT_GIVEN}; then
  TARGET_FLAGS+=("--all")
fi
# Deduplicate (e.g. --agent all --agent cursor) while preserving order.
ORDERED=()
for flag in "${TARGET_FLAGS[@]}"; do
  duplicate=false
  for existing in ${ORDERED[@]+"${ORDERED[@]}"}; do
    if [[ "${existing}" == "${flag}" ]]; then
      duplicate=true
      break
    fi
  done
  ${duplicate} || ORDERED+=("${flag}")
done

# bash 3.2 (macOS) treats expanding an empty array under `set -u` as an
# unbound variable; `${arr[@]+"${arr[@]}"}` is the safe expansion idiom.
exec "${SCRIPT_DIR}/install_local_agent_integrations.sh" \
  ${ORDERED[@]+"${ORDERED[@]}"} \
  --aru-home "${SDLC_HOME}" \
  ${REST[@]+"${REST[@]}"}
