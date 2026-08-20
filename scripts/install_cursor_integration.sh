#!/usr/bin/env bash
# install_cursor_integration.sh — DEPRECATED shim.
#
# This script was renamed to install_agent_integration.sh: the installer has
# been vendor-neutral since it stopped being Cursor-only (it wires skills and
# adapters for Cursor, Codex, Claude Code and Antigravity). The old name is
# kept for exactly one release so existing docs, hooks, and muscle memory keep
# working. It forwards to the new name and warns.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "notice: install_cursor_integration.sh is deprecated; use install_agent_integration.sh" >&2

exec "${SCRIPT_DIR}/install_agent_integration.sh" "$@"