"""Read-only local process observer. Authentication/quota is probed only before work."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


NAMES = {
    "openai-codex": {"codex"}, "claude-code": {"claude"},
    "xai-cursor": {"cursor-agent"}, "google-antigravity": {"agy", "antigravity"},
}


def observe(family: str, profile: str | None = None) -> dict:
    """Report local agent processes as diagnostics; only the lane's own Claude profile vetoes.

    A desktop session, a process without an observable CLAUDE_CONFIG_DIR, or a
    process on another profile says nothing about remaining quota, so it is
    counted and reported but never treated as exhaustion. Managed workers are
    tracked by the Driver's reservation lock, not by process names. The one
    remaining veto is a live process on exactly this lane's profile: two
    sessions on one config directory contend for the same state.
    """
    result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                            timeout=10, check=False)
    if result.returncode:
        return {"available": False, "reason": "process inventory unavailable"}
    unrelated = 0
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or Path(parts[1]).name not in NAMES[family]:
            continue
        if family == "claude-code" and profile:
            # Inspect only this process's profile identifier; never print its arguments/environment.
            environment = subprocess.run(["ps", "eww", "-p", parts[0], "-o", "command="],
                                         capture_output=True, text=True, timeout=10, check=False)
            if environment.returncode:
                # The process is listed but its profile cannot be read: do not guess
                # that it is unrelated. Fail closed for this observation only.
                return {"available": False,
                        "reason": f"observer unavailable: profile inspection failed for pid {parts[0]}"}
            match = re.search(r"(?:^|\s)CLAUDE_CONFIG_DIR=([^\s]+)", environment.stdout)
            if match and Path(match[1]).name == f"config-{profile}":
                return {"available": False,
                        "reason": f"managed session for profile {profile} is live (pid {parts[0]})"}
        unrelated += 1
    if unrelated:
        return {"available": True,
                "reason": f"{unrelated} unrelated local agent process(es) observed; quota not yet probed"}
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
