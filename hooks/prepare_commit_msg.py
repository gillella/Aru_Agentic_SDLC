#!/usr/bin/env python3
"""prepare_commit_msg.py - stamps the Agent: <id> trailer on commits in governed repositories.

Runs as a git prepare-commit-msg hook.
Git passes:
  $1: path to file containing commit message buffer
  $2: message source (message, template, merge, squash, commit, or empty)
  $3: commit SHA (when amending or using -c/-C)
"""

import os
import sys
from pathlib import Path

# Search candidate locations for common.py across canonical home and consumer repos
SCRIPT_DIR = Path(__file__).resolve().parent
candidates = [
    Path(os.environ.get("ARU_SDLC_HOME", "")) / "scripts",
    SCRIPT_DIR.parent / "scripts",
    SCRIPT_DIR.parents[1] / "scripts" if len(SCRIPT_DIR.parents) > 1 else None,
    SCRIPT_DIR / "scripts",
]
for candidate in candidates:
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

try:
    import common
except ImportError:
    common = None


def stamp_commit_message_file(msg_file_path: str, agent: str = None) -> bool:
    """Reads commit message file, applies Agent trailer if appropriate, and writes back."""
    path = Path(msg_file_path)
    if not path.is_file():
        return False

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False

    if not content.strip():
        return False

    agent_id = agent or (common.get_agent_id() if common else None)
    if not agent_id:
        for var in ("ARU_AGENT_ID", "AGENT_ID", "ARU_AGENT", "AGENT"):
            val = os.environ.get(var, "").strip()
            if val:
                agent_id = val
                break

    if not agent_id:
        return False

    if not common:
        return False

    formatted = common.format_commit_message(content, agent=agent_id)

    if formatted.rstrip() != content.rstrip():
        try:
            path.write_text(formatted.rstrip() + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    return False


def main() -> int:
    if len(sys.argv) < 2:
        return 0

    msg_file = sys.argv[1]
    stamp_commit_message_file(msg_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
