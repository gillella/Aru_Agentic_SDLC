#!/usr/bin/env bash
# install_hooks.sh - installs the Aru enforcement hooks into a target repo.
#
#   ./install_hooks.sh              # install into the current repo
#   ./install_hooks.sh -r ~/x/repo  # install into another repo
#   ./install_hooks.sh --check      # report status, change nothing
#
# Installs two independent layers:
#   1. .git/hooks/pre-push        - blocks pushes to main for every tool
#   2. .claude/settings.json      - PreToolUse touches enforcement for Claude Code
set -euo pipefail

TARGET="$(pwd)"
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    -r|--repo) TARGET="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    *) echo "usage: $0 [-r repo] [--check]" >&2; exit 1 ;;
  esac
done

: "${ARU_SDLC_HOME:?ARU_SDLC_HOME must be set}"
HOOK_SRC="$ARU_SDLC_HOME/hooks"

cd "$TARGET"
# Re-anchor TARGET to its physical absolute path. git reports --git-path
# relative to the *current* directory, so a relative -r composed
# "<repo>/<repo>/.git/hooks" below and installed where git never looks.
TARGET="$(pwd -P)"
git rev-parse --git-dir >/dev/null 2>&1 || { echo "$TARGET is not a git repo" >&2; exit 1; }
# --git-path hooks derives the effective hooks path (respecting core.hooksPath)
# across primary checkouts and worktrees.
HOOKS_DIR="$(git rev-parse --git-path hooks)"
case "$HOOKS_DIR" in /*) ;; *) HOOKS_DIR="$TARGET/$HOOKS_DIR" ;; esac

if [ "$CHECK_ONLY" = "1" ]; then
  echo "repo:       $TARGET"
  if [ -x "$HOOKS_DIR/pre-push" ]; then echo "pre-push:   installed"; else echo "pre-push:   MISSING"; fi
  if [ -x "$HOOKS_DIR/prepare-commit-msg" ] && grep -q "Aru_Agentic_SDLC prepare-commit-msg" "$HOOKS_DIR/prepare-commit-msg" 2>/dev/null; then
    echo "prepare-commit-msg: installed"
  else
    echo "prepare-commit-msg: MISSING"
  fi
  if grep -q enforce_touches .claude/settings.json 2>/dev/null; then
    echo "PreToolUse: installed"
  else
    echo "PreToolUse: MISSING"
  fi
  exit 0
fi

mkdir -p "$HOOKS_DIR"
EXISTING="$HOOKS_DIR/pre-push"

# Never clobber an existing hook. A repo's pre-push may already run tests,
# secret scanning, or policy checks, and silently deleting those while
# installing "enforcement" would remove more protection than it adds. The
# Claude settings are merged rather than overwritten for the same reason.
if [ -f "$EXISTING" ] && ! grep -q "Aru_Agentic_SDLC pre-push" "$EXISTING" 2>/dev/null; then
  PRESERVED="$HOOKS_DIR/pre-push.pre-aru"
  if [ ! -f "$PRESERVED" ]; then
    mv "$EXISTING" "$PRESERVED"
    chmod +x "$PRESERVED"
  fi
  # The Aru hook chains to pre-push.pre-aru itself, before its own exit -
  # appending the chain here put it after that exit, where it never ran.
  cp "$HOOK_SRC/pre-push" "$EXISTING"
  chmod +x "$EXISTING"
  echo "✅ pre-push installed; the previous hook was preserved as pre-push.pre-aru and is chained after it"
else
  cp "$HOOK_SRC/pre-push" "$EXISTING"
  chmod +x "$EXISTING"
  echo "✅ pre-push hook installed at $EXISTING"
fi

EXISTING_MSG="$HOOKS_DIR/prepare-commit-msg"
if [ -f "$EXISTING_MSG" ] && ! grep -q "Aru_Agentic_SDLC prepare-commit-msg" "$EXISTING_MSG" 2>/dev/null; then
  PRESERVED="$HOOKS_DIR/prepare-commit-msg.pre-aru"
  if [ -f "$PRESERVED" ]; then
    COUNTER=1
    while [ -f "$HOOKS_DIR/prepare-commit-msg.pre-aru.$COUNTER" ]; do
      COUNTER=$((COUNTER + 1))
    done
    PRESERVED="$HOOKS_DIR/prepare-commit-msg.pre-aru.$COUNTER"
  fi
  mv "$EXISTING_MSG" "$PRESERVED"
  chmod +x "$PRESERVED"
  cp "$HOOK_SRC/prepare-commit-msg" "$EXISTING_MSG"
  chmod +x "$EXISTING_MSG"
  cp "$HOOK_SRC/prepare_commit_msg.py" "$HOOKS_DIR/prepare_commit_msg.py"
  echo "✅ prepare-commit-msg installed; the previous hook was preserved as $(basename "$PRESERVED") and is chained after it"
else
  cp "$HOOK_SRC/prepare-commit-msg" "$EXISTING_MSG"
  chmod +x "$EXISTING_MSG"
  cp "$HOOK_SRC/prepare_commit_msg.py" "$HOOKS_DIR/prepare_commit_msg.py"
  echo "✅ prepare-commit-msg hook installed at $EXISTING_MSG"
fi

# Merge the PreToolUse entry into .claude/settings.json without clobbering
# whatever else the project already configures there.
mkdir -p .claude
python3 - "$HOOK_SRC/enforce_touches.py" <<'PY'
import json, os, sys

hook_cmd = f'python3 "{sys.argv[1]}"'
path = ".claude/settings.json"
try:
    with open(path) as fh:
        settings = json.load(fh)
except (OSError, ValueError):
    settings = {}

hooks = settings.setdefault("hooks", {})
entries = hooks.setdefault("PreToolUse", [])

# Idempotent: replace any prior Aru entry rather than appending a duplicate.
entries = [e for e in entries if "enforce_touches" not in json.dumps(e)]
entries.append({
    "matcher": "Edit|Write|NotebookEdit|MultiEdit|Bash",
    "hooks": [{"type": "command", "command": hook_cmd}],
})
hooks["PreToolUse"] = entries

with open(path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
print(f"✅ PreToolUse enforcement registered in {os.path.abspath(path)}")
PY

echo
echo "Verify:  $0 --check"
echo "Note:    .claude/settings.json is committed on purpose - it is project"
echo "         governance, not a personal preference. Do not move it to"
echo "         settings.local.json."
echo
echo "Server:  this hook is skippable. Enable the GitHub ruleset with"
echo "         python3 \"\$ARU_SDLC_HOME/scripts/enable_main_ruleset.py\" --apply"
echo "         (exits 3 until GitHub Pro or a public repo unlocks rulesets; #133)."
