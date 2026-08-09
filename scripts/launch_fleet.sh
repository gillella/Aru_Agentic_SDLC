#!/usr/bin/env bash
# launch_fleet.sh - Prepares N isolated clones + per-agent prompts for a
# parallel SDLC fleet. Does not start the agents: you paste each prompt into
# its own agent session so every agent runs at its own pace.
#
#   ./launch_fleet.sh -n 3                    # 3 agents off the current repo
#   ./launch_fleet.sh -n 4 -d ~/fleet/unum    # custom fleet directory
set -euo pipefail

AGENTS=3
FLEET_DIR=""
SRC_REPO="$(pwd)"
# Round-robined across agents so a fleet is mixed-family by default: the picker
# can then route every PR to a reviewer whose blind spots differ from the
# author's. A single-family fleet still reviews, just more weakly.
FAMILIES=(anthropic openai anthropic openai)

while getopts "n:d:r:f:" opt; do
  case "$opt" in
    n) AGENTS="$OPTARG" ;;
    d) FLEET_DIR="$OPTARG" ;;
    r) SRC_REPO="$OPTARG" ;;
    f) IFS=',' read -r -a FAMILIES <<< "$OPTARG" ;;
    *) echo "usage: $0 [-n agents] [-d dir] [-r repo] [-f fam1,fam2]" >&2; exit 1 ;;
  esac
done

: "${ARU_SDLC_HOME:?ARU_SDLC_HOME must be set}"
PROMPT_SRC="$ARU_SDLC_HOME/prompts/fleet-worker.md"
[ -f "$PROMPT_SRC" ] || { echo "Missing $PROMPT_SRC" >&2; exit 1; }

cd "$SRC_REPO"
git rev-parse --git-dir >/dev/null 2>&1 || { echo "$SRC_REPO is not a git repo" >&2; exit 1; }
REPO_NAME="$(basename "$(git rev-parse --show-toplevel)")"
ORIGIN="$(git remote get-url origin)"
FLEET_DIR="${FLEET_DIR:-$HOME/.aru-fleet/$REPO_NAME}"

# Show the board before sizing the fleet: more agents than claimable issues
# just means idle sessions.
echo "=== Board check ==="
python3 "$ARU_SDLC_HOME/scripts/triage_backlog.py" --capacity 2>/dev/null || \
  python3 "$ARU_SDLC_HOME/scripts/fetch_next_issue.py" --agent fleet-probe
echo

mkdir -p "$FLEET_DIR"
for i in $(seq 1 "$AGENTS"); do
  AGENT_ID="agent-$i"
  DEST="$FLEET_DIR/$AGENT_ID"
  if [ -d "$DEST/.git" ]; then
    echo "↻ $AGENT_ID: reusing $DEST"
    git -C "$DEST" fetch origin --quiet
  else
    echo "⧉ $AGENT_ID: cloning into $DEST"
    git clone --quiet "$ORIGIN" "$DEST"
  fi
  # Materialize the prompt with this agent's id and family substituted.
  sed -e "s/<AGENT_ID>/$AGENT_ID/g" -e "s/<FAMILY>/${FAMILIES[$(( (i-1) % ${#FAMILIES[@]} ))]}/g" \
    "$PROMPT_SRC" > "$DEST/FLEET-PROMPT.md"
  # Enforcement that does not depend on which tool the agent is.
  ARU_SDLC_HOME="$ARU_SDLC_HOME" bash "$ARU_SDLC_HOME/scripts/install_hooks.sh" -r "$DEST" >/dev/null 2>&1 || \
    echo "  (hooks not installed for $AGENT_ID; run install_hooks.sh once they are merged)"
  if [ "$i" -eq 1 ]; then
    printf '\n> JANITOR: before step 1 of each cycle, run\n> `python3 "$ARU_SDLC_HOME/scripts/fetch_next_issue.py" --agent %s --reap-after 4`\n' \
      "$AGENT_ID" >> "$DEST/FLEET-PROMPT.md"
  fi
done

echo
echo "=== Launch ==="
for i in $(seq 1 "$AGENTS"); do
  fam="${FAMILIES[$(( (i-1) % ${#FAMILIES[@]} ))]}"
  echo "  Session $i ($fam):  cd $FLEET_DIR/agent-$i  # paste the block between the COPY markers in FLEET-PROMPT.md"
done
echo
echo "Track them:  gh issue list --label 'status:in-progress' && gh pr list"
