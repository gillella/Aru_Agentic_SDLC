#!/usr/bin/env python3
"""Read-only continuity diagnosis for local coding-agent integrations.

Full install-link diagnosis is issue #34. This command reports desktop
continuity adapters, capability levels, explicit-stop state, and version
probes. It never prints credential values and never mutates configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SECRET_ENV = re.compile(r"(TOKEN|SECRET|KEY|PASSWORD|PAT|CREDENTIAL|AUTH)", re.I)
VERSION_SAFE = re.compile(r"^[A-Za-z0-9._+ -]{1,80}$")

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_DEGRADED = 2


def load_catalog(aru_home: Path) -> dict:
    path = aru_home / "templates" / "integrations" / "continuity.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def probe_version(binary: str) -> str | None:
    found = shutil.which(binary)
    if not found:
        return None
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
    }
    try:
        proc = subprocess.run(
            [found, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unreadable"
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text:
        return "unknown"
    first = text[0].strip()
    if SECRET_ENV.search(first):
        return "redacted"
    if not VERSION_SAFE.search(first):
        return "unparsed"
    return first


def agent_detected(target_home: Path, spec: dict) -> bool:
    for token in spec.get("detect", []):
        if token.startswith("."):
            if (target_home / token).exists():
                return True
        elif shutil.which(token):
            return True
    return False


def stop_applies(stop_doc: dict | None, project: str | None) -> bool:
    if not stop_doc:
        return False
    projects = stop_doc.get("projects") or []
    if "*" in projects:
        return True
    if project and project in projects:
        return True
    return bool(projects) and project is None


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def report(aru_home: Path, target_home: Path, project: str | None) -> dict:
    catalog = load_catalog(aru_home)
    stop_doc = load_json(target_home / ".aru" / "factory-loop.stop")
    wake_doc = load_json(target_home / ".aru" / "native-wake.json") or {"projects": {}}
    wake_entry = (wake_doc.get("projects") or {}).get(project or "", {})
    agents = {}
    for name, spec in catalog["agents"].items():
        detected = agent_detected(target_home, spec)
        version = probe_version(name) if detected else None
        same = spec["same_task_native_wake"]
        enabled = bool(wake_entry.get("enabled")) if same.startswith("opt_in") else False
        gap = same in {"session_loop_only", "unsupported"}
        agents[name] = {
            "detected": detected,
            "version": version,
            "active_task_loop": spec["active_task_loop"],
            "same_task_native_wake": same,
            "same_task_native_wake_notes": spec["same_task_native_wake_notes"],
            "app_restart_recovery": spec["app_restart_recovery"],
            "machine_restart_recovery": spec["machine_restart_recovery"],
            "native_wake_enabled": enabled,
            "capability_gap": gap,
        }
    stopped = stop_applies(stop_doc, project)
    return {
        "install_diagnosis": "not_yet",
        "issue_34": True,
        "project": project,
        "stop_file": str(target_home / ".aru" / "factory-loop.stop"),
        "loop_stopped": stopped,
        "stop": stop_doc,
        "native_wake": wake_entry or None,
        "non_guarantees": catalog["non_guarantees"],
        "forbidden": catalog["forbidden"],
        "agents": agents,
    }


def render_human(payload: dict) -> str:
    lines = [
        "Aru continuity doctor (install-link diagnosis is #34 / not yet diagnosable)",
        f"loop_stopped: {payload['loop_stopped']}",
        f"project: {payload['project'] or '(none)'}",
    ]
    for name, agent in payload["agents"].items():
        mark = "detected" if agent["detected"] else "absent"
        gap = " gap" if agent["capability_gap"] else ""
        lines.append(
            f"  {name}: {mark} version={agent['version']} "
            f"same_task_wake={agent['same_task_native_wake']}{gap}"
        )
    lines.append("Never claims app-quit, sleep, power-off, or credit recovery.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only continuity doctor.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--aru-home")
    parser.add_argument("--target-home")
    parser.add_argument("--project")
    args = parser.parse_args()
    aru_home = Path(args.aru_home or os.environ.get("ARU_SDLC_HOME") or Path(__file__).resolve().parents[1])
    target_home = Path(args.target_home or Path.home())
    if args.project and not args.project.startswith("/"):
        print("error: --project must be an absolute path", file=sys.stderr)
        return EXIT_INVALID
    if not (aru_home / "templates" / "integrations" / "continuity.json").is_file():
        print("error: continuity catalog missing under --aru-home", file=sys.stderr)
        return EXIT_INVALID
    payload = report(aru_home, target_home, args.project)
    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(payload))
    if payload["loop_stopped"] or any(
        a["detected"] and a["capability_gap"] for a in payload["agents"].values()
    ):
        return EXIT_DEGRADED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
