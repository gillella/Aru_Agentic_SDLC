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
from typing import List, Optional, Tuple


def _parse_terminal_trailers(message: str) -> Tuple[str, List[str]]:
    """Splits a commit message into the main content (subject/body) and terminal trailer lines.

    According to Git trailer conventions:
    - Trailers appear in a contiguous block at the end of the message.
    - Each trailer line matches `<Token>: <value>`.
    - The first line (subject) is never a trailer.
    - If the terminal paragraph contains any non-trailer lines, the entire paragraph is body prose.
    """
    raw_lines = message.rstrip().splitlines()
    if not raw_lines:
        return "", []

    idx = len(raw_lines) - 1
    while idx >= 0 and not raw_lines[idx].strip():
        idx -= 1

    if idx <= 0:
        return "\n".join(raw_lines).rstrip(), []

    paragraph_end = idx
    while idx >= 0 and raw_lines[idx].strip():
        idx -= 1
    paragraph_start = idx + 1

    if paragraph_start == 0:
        return "\n".join(raw_lines).rstrip(), []

    candidate_lines = raw_lines[paragraph_start:paragraph_end + 1]
    trailer_regex = re.compile(r"^[A-Za-z0-9_-]+:\s*.+$")

    if not all(trailer_regex.match(line.strip()) for line in candidate_lines):
        return "\n".join(raw_lines).rstrip(), []

    body = "\n".join(raw_lines[:paragraph_start]).rstrip()
    trailers = [line.strip() for line in candidate_lines]
    return body, trailers


def _format_commit_message(content: str, agent_id: str) -> str:
    """Formats a git commit message with standard Agent trailer."""
    msg = content.strip()
    if not msg:
        return msg

    trailer = f"Agent: {agent_id}"
    body, trailers = _parse_terminal_trailers(msg)

    if any(re.match(r"^agent\s*:", t, re.IGNORECASE) for t in trailers):
        return msg

    if trailers:
        trailers.append(trailer)
        return body + "\n\n" + "\n".join(trailers)

    if body:
        return body + "\n\n" + trailer

    return msg + "\n\n" + trailer


def stamp_commit_message_file(msg_file_path: str, agent: Optional[str] = None) -> bool:
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
