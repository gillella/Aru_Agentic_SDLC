#!/usr/bin/env python3
"""prepare_commit_msg.py - stamps the Agent: <id> trailer on commits in governed repositories.

Runs as a git prepare-commit-msg hook.
Git passes:
  $1: path to file containing commit message buffer
  $2: message source (message, template, merge, squash, commit, or empty)
  $3: commit SHA (when amending or using -c/-C)
"""

import os
import re
import sys
from pathlib import Path


def _format_commit_message(content: str, agent_id: str) -> str:
    """Formats a git commit message with standard Agent trailer."""
    msg = content.strip()
    if not msg:
        return msg
    trailer = f"Agent: {agent_id}"
    lines = msg.splitlines()
    if any(re.match(r"^agent\s*:", line, re.IGNORECASE) for line in lines):
        return msg
    return msg + "\n\n" + trailer


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

    agent_id = agent
    if not agent_id:
        for var in ("ARU_AGENT_ID", "AGENT_ID", "ARU_AGENT", "AGENT"):
            val = os.environ.get(var, "").strip()
            if val:
                agent_id = val
                break

    if not agent_id:
        return False

    formatted = _format_commit_message(content, agent_id=agent_id)

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
