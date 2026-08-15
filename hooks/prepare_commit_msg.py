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

# Add scripts directory to path to reuse common.py
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import common
except ImportError:
    sdlc_home = os.environ.get("ARU_SDLC_HOME")
    if sdlc_home and (Path(sdlc_home) / "scripts").is_dir():
        sys.path.insert(0, str(Path(sdlc_home) / "scripts"))
        import common
    else:
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

    if common:
        formatted = common.format_commit_message(content, agent=agent_id)
    else:
        # Fallback if common is not importable
        formatted = content.rstrip() + f"\n\nAgent: {agent_id}"

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
