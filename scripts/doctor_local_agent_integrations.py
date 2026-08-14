#!/usr/bin/env python3
"""Read-only continuity diagnosis for local coding-agent integrations.

Full install-link diagnosis is issue #34. This command reports desktop
continuity adapters, capability levels, explicit-stop state, and version
probes. It never prints credential values and never mutates configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

SECRET_ENV = re.compile(r"(TOKEN|SECRET|KEY|PASSWORD|PAT|CREDENTIAL|AUTH)", re.I)
VERSION_SAFE = re.compile(r"^[A-Za-z0-9._+ -]{1,80}$")
MANAGED_CODEX_ID = re.compile(r'^id = "aru-code-loop(-[0-9a-f]+)?"\s*$', re.M)
CODEX_STATUS = re.compile(r'^status = "([^"]+)"\s*$', re.M)

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


def config_detected(target_home: Path, spec: dict) -> bool:
    for token in spec.get("detect", []):
        if token.startswith(".") and (target_home / token).exists():
            return True
    return False


def cli_name(spec: dict) -> str | None:
    for token in spec.get("detect", []):
        if not token.startswith("."):
            return token
    return None


def application_roots(target_home: Path) -> list[Path]:
    roots = [target_home / "Applications"]
    if target_home.resolve() == Path.home().resolve():
        roots.extend([Path("/Applications"), Path.home() / "Applications"])
    unique = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def sanitize_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if SECRET_ENV.search(text):
        return "redacted"
    if not VERSION_SAFE.search(text):
        return "unparsed"
    return text


def read_bundle_meta(app_path: Path) -> dict | None:
    plist = app_path / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ident = data.get("CFBundleIdentifier")
    version = sanitize_version(
        data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
    )
    return {
        "path": str(app_path),
        "bundle_id": ident if isinstance(ident, str) else None,
        "version": version,
    }


def find_macos_app(spec: dict, roots: list[Path]) -> dict | None:
    names = spec.get("macos_app_names") or []
    bundle_ids = set(spec.get("macos_bundle_ids") or [])
    for name in names:
        for root in roots:
            meta = read_bundle_meta(root / name)
            if meta:
                return meta
    if not bundle_ids:
        return None
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.suffix != ".app":
                continue
            meta = read_bundle_meta(child)
            if meta and meta.get("bundle_id") in bundle_ids:
                return meta
    return None


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


def project_automation_id(project: str) -> str:
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return f"aru-code-loop-{digest}"


def read_managed_codex_status(toml: Path) -> str | None:
    if not toml.is_file():
        return None
    try:
        text = toml.read_text(encoding="utf-8")
    except OSError:
        return None
    if not MANAGED_CODEX_ID.search(text):
        return None
    match = CODEX_STATUS.search(text)
    return match.group(1) if match else "unknown"


def native_wake_state(name: str, spec: dict, target_home: Path,
                      project: str | None, wake_entry: dict) -> dict:
    """Split operator opt-in from verified per-app configured/active wake."""
    same = spec["same_task_native_wake"]
    requested = bool(wake_entry.get("enabled")) if same.startswith("opt_in") else False
    if name == "codex" and project:
        auto_id = wake_entry.get("automation_id") or project_automation_id(project)
        auto_dir = target_home / ".codex" / "automations" / auto_id
        prompt = (auto_dir / "PROMPT.md").is_file()
        status = read_managed_codex_status(auto_dir / "automation.toml")
        prepared = requested or prompt
        configured = status is not None
        enabled = status == "ACTIVE"
        if enabled:
            evidence = "automation_active"
        elif status == "PAUSED":
            evidence = "automation_paused"
        elif prepared:
            evidence = "prompt_only"
        else:
            evidence = "none"
    elif same.startswith("opt_in"):
        prepared, configured, enabled = requested, False, False
        evidence = "requested" if requested else "none"
    else:
        prepared, configured, enabled = False, False, False
        evidence = "unsupported"
    return {
        "native_wake_prepared": prepared,
        "native_wake_configured": configured,
        "native_wake_enabled": enabled,
        "native_wake_evidence": evidence,
    }


def report(aru_home: Path, target_home: Path, project: str | None) -> dict:
    catalog = load_catalog(aru_home)
    stop_doc = load_json(target_home / ".aru" / "factory-loop.stop")
    wake_doc = load_json(target_home / ".aru" / "native-wake.json") or {"projects": {}}
    wake_entry = (wake_doc.get("projects") or {}).get(project or "", {})
    roots = application_roots(target_home)
    agents = {}
    for name, spec in catalog["agents"].items():
        app = find_macos_app(spec, roots)
        config = config_detected(target_home, spec)
        binary = cli_name(spec)
        cli_version = probe_version(binary) if binary else None
        cli_present = bool(binary and shutil.which(binary))
        detected = bool(app or config or cli_present)
        version = (app or {}).get("version") or cli_version
        same = spec["same_task_native_wake"]
        gap = same in {"session_loop_only", "unsupported"}
        wake = native_wake_state(name, spec, target_home, project, wake_entry)
        agents[name] = {
            "detected": detected,
            "version": version,
            "app_path": (app or {}).get("path"),
            "app_version": (app or {}).get("version"),
            "cli_version": cli_version,
            "config_detected": config,
            "active_task_loop": spec["active_task_loop"],
            "same_task_native_wake": same,
            "same_task_native_wake_notes": spec["same_task_native_wake_notes"],
            "app_restart_recovery": spec["app_restart_recovery"],
            "machine_restart_recovery": spec["machine_restart_recovery"],
            "capability_gap": gap,
            **wake,
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
        app = f" app={agent['app_path']}" if agent.get("app_path") else ""
        wake = (
            f" prepared={agent['native_wake_prepared']}"
            f" enabled={agent['native_wake_enabled']}"
            f" evidence={agent['native_wake_evidence']}"
        )
        lines.append(
            f"  {name}: {mark} version={agent['version']}{app} "
            f"same_task_wake={agent['same_task_native_wake']}{gap}{wake}"
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
