"""Read-only local process observer. Authentication/quota is probed only before work."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


NAMES = {
    "openai-codex": {"codex"}, "claude-code": {"claude"},
    "xai-cursor": {"cursor-agent", "agent"}, "google-antigravity": {"agy", "antigravity"},
}


def observe(family: str, profile: str | None = None) -> dict:
    result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                            timeout=10, check=False)
    if result.returncode:
        return {"available": False, "reason": "process inventory unavailable"}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or Path(parts[1]).name not in NAMES[family]:
            continue
        if family == "claude-code" and profile:
            # Inspect only this process's profile identifier; never print its arguments/environment.
            environment = subprocess.run(["ps", "eww", "-p", parts[0], "-o", "command="],
                                         capture_output=True, text=True, timeout=10, check=False)
            match = re.search(r"(?:^|\s)CLAUDE_CONFIG_DIR=([^\s]+)", environment.stdout)
            if match and Path(match[1]).name != f"config-{profile}":
                continue
        return {"available": False, "reason": "matching live agent or unidentifiable profile"}
    return {"available": True, "reason": "no matching local agent; quota not yet probed"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", choices=sorted(NAMES), required=True)
    parser.add_argument("--claude-profile")
    args = parser.parse_args()
    try:
        observation = observe(args.family, args.claude_profile)
    except (OSError, subprocess.TimeoutExpired):
        observation = {"available": False, "reason": "process evidence unavailable"}
    print(json.dumps(observation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
