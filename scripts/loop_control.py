#!/usr/bin/env python3
"""Unified loop control, status inspection, and adapter contract.

Provides project-agnostic stop/resume operations, status inspection, and
contradiction detection across desktop stop markers, native-wake state, and
external orchestrator adapters.

Never writes to or mutates external orchestrators; adapter interaction is
strictly read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_INVALID, EXIT_DEGRADED = 0, 1, 2
PAUSE_REASONS = (
    "operator-requested", "factory-complete", "human-intervention",
    "quota-exhausted", "maintenance", "error-threshold",
)
DEFAULT_PAUSE_REASON = "operator-requested"
CONTRADICTION_ORCHESTRATOR_PAUSED_WITHOUT_STOP = "orchestrator_paused_without_stop_marker"
CONTRADICTION_STOP_MARKER_WITHOUT_ORCHESTRATOR_PAUSE = "stop_marker_without_orchestrator_pause"


def stop_applies(stop_doc: dict | None, project: str | None) -> bool:
    """Return True if the stop marker document applies to the given project."""
    if not isinstance(stop_doc, dict):
        return False
    projects = stop_doc.get("projects") or []
    if "*" in projects or (project and project in projects):
        return True
    return bool(projects) and project is None


def resolve_desktop_stop_marker(target_home: Path, project: str | None = None) -> dict:
    """Read and resolve the desktop stop marker under target_home/.aru."""
    stop_path = target_home / ".aru" / "factory-loop.stop"
    if not stop_path.is_file():
        return {"present": False, "applies": False, "scope": "none", "projects": [], "reason": None, "path": str(stop_path)}
    try:
        data = json.loads(stop_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"malformed stop marker at {stop_path}: expected JSON object")
    raw_projects = data.get("projects")
    if raw_projects is None:
        projects = []
    elif isinstance(raw_projects, list):
        projects = [str(p) for p in raw_projects]
    else:
        raise ValueError(f"malformed stop marker at {stop_path}: 'projects' must be a list")
    scope = "global" if "*" in projects else ("project" if projects else "none")
    return {
        "present": True, "applies": stop_applies(data, project), "scope": scope,
        "projects": projects, "reason": data.get("reason") or DEFAULT_PAUSE_REASON, "path": str(stop_path),
    }


def resolve_native_wake(target_home: Path, project: str | None = None) -> dict:
    """Read native-wake configuration under target_home/.aru."""
    wake_path = target_home / ".aru" / "native-wake.json"
    if not wake_path.is_file():
        return {"enabled": False, "automation_id": None, "evidence": "none"}
    try:
        data = json.loads(wake_path.read_text(encoding="utf-8"))
        entry = (data.get("projects") or {}).get(project or "", {}) if isinstance(data, dict) else {}
    except Exception:
        entry = {}
    enabled = bool(entry.get("enabled", False))
    auto_id = entry.get("automation_id")
    return {
        "enabled": enabled,
        "automation_id": auto_id if enabled else None,
        "evidence": entry.get("evidence") or ("requested" if enabled else "none"),
    }


def query_orchestrator(adapter_cmd: str | None, project: str | None) -> dict:
    """Query external orchestrator state via read-only adapter command."""
    if not adapter_cmd:
        return {"configured": False, "adapter": None, "state": "unknown", "reason": None, "detail": "no orchestrator adapter configured", "error": None}
    cmd = shlex.split(adapter_cmd) + (["--project", project] if project else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception as exc:
        return {"configured": True, "adapter": adapter_cmd, "state": "unknown", "reason": None, "detail": "adapter execution failed", "error": str(exc)}
    if proc.returncode != 0:
        return {"configured": True, "adapter": adapter_cmd, "state": "unknown", "reason": None, "detail": "adapter returned non-zero exit code", "error": proc.stderr.strip() or f"exit code {proc.returncode}"}
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
    except Exception as exc:
        return {"configured": True, "adapter": adapter_cmd, "state": "unknown", "reason": None, "detail": "adapter output is not valid JSON", "error": str(exc)}
    state = data.get("state", "unknown")
    if state not in {"enabled", "paused", "unknown"}:
        state = "unknown"
    return {"configured": True, "adapter": data.get("adapter") or adapter_cmd, "state": state, "reason": data.get("reason"), "detail": data.get("detail"), "error": None}


def detect_contradictions(marker: dict, orchestrator: dict) -> list[dict]:
    """Identify contradictions between desktop stop marker and orchestrator state."""
    contradictions = []
    if orchestrator.get("configured"):
        orch_state, applies = orchestrator.get("state"), marker.get("applies", False)
        if orch_state == "paused" and not applies:
            contradictions.append({"id": CONTRADICTION_ORCHESTRATOR_PAUSED_WITHOUT_STOP, "detail": "Orchestrator is paused for project but no desktop stop marker applies."})
        elif orch_state == "enabled" and applies:
            contradictions.append({"id": CONTRADICTION_STOP_MARKER_WITHOUT_ORCHESTRATOR_PAUSE, "detail": "Desktop stop marker applies to project but orchestrator is enabled."})
    return contradictions


def compute_status(marker: dict, orchestrator: dict, contradictions: list[dict], error: str | None = None) -> str:
    """Compute overall status string."""
    if error:
        return "invalid"
    if contradictions:
        return "contradictory"
    if orchestrator.get("error"):
        return "degraded"
    if marker.get("applies") or orchestrator.get("state") == "paused":
        return "stopped"
    return "ok"


def get_status(target_home: Path, project: str | None = None, adapter_cmd: str | None = None) -> dict:
    """Assemble full status report dictionary."""
    err = None
    try:
        marker = resolve_desktop_stop_marker(target_home, project)
    except ValueError as exc:
        marker = {"present": False, "applies": False, "scope": "none", "projects": [], "reason": None, "path": str(target_home / ".aru" / "factory-loop.stop")}
        err = str(exc)
    wake = resolve_native_wake(target_home, project)
    orch = query_orchestrator(adapter_cmd, project)
    contradictions = detect_contradictions(marker, orch)
    payload = {
        "status": compute_status(marker, orch, contradictions, error=err),
        "project": project, "desktop_stop_marker": marker, "native_wake": wake,
        "orchestrator": orch, "contradictions": contradictions, "pause_reasons": list(PAUSE_REASONS),
    }
    if err:
        payload["error"] = err
    return payload


def execute_stop(target_home: Path, project: str | None = None, reason: str = DEFAULT_PAUSE_REASON, source: str = "loop_control.py") -> dict:
    """Persist explicit stop marker."""
    if reason not in PAUSE_REASONS:
        raise ValueError(f"invalid pause reason '{reason}'. Valid reasons: {', '.join(PAUSE_REASONS)}")
    if project and not project.startswith("/"):
        raise ValueError("--project must be an absolute path")
    aru_dir = target_home / ".aru"
    aru_dir.mkdir(parents=True, exist_ok=True)
    stop_path = aru_dir / "factory-loop.stop"
    projects = []
    if stop_path.is_file():
        try:
            data = json.loads(stop_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected JSON object")
            projects = list(data.get("projects") or [])
        except Exception as exc:
            raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
    token = project or "*"
    if token not in projects:
        projects.append(token)
    doc = {"projects": projects, "stopped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": source, "reason": reason}
    tmp = stop_path.with_suffix(".stop.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(stop_path)
    return {"action": "stop", "changed": True, "token": token, "stop_path": str(stop_path), "reason": reason}


def execute_resume(target_home: Path, project: str | None = None, reason: str | None = None) -> tuple[int, str, dict]:
    """Resume loop by clearing stop marker."""
    if reason and reason not in PAUSE_REASONS:
        raise ValueError(f"invalid pause reason '{reason}'. Valid reasons: {', '.join(PAUSE_REASONS)}")
    if project and not project.startswith("/"):
        raise ValueError("--project must be an absolute path")
    stop_path = target_home / ".aru" / "factory-loop.stop"
    if not stop_path.is_file():
        return EXIT_OK, "No stop requested; continuing", {"action": "resume", "changed": False, "message": "No stop requested; continuing"}
    try:
        data = json.loads(stop_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
    except Exception as exc:
        raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
    projects = list(data.get("projects") or [])
    if not project:
        stop_path.unlink(missing_ok=True)
        return EXIT_OK, f"removed stop marker {stop_path}", {"action": "resume", "changed": True, "message": f"removed stop marker {stop_path}"}
    if "*" in projects:
        return EXIT_INVALID, "error: global stop (*) is in effect; resume without --project to clear it", {"action": "resume", "error": "global stop in effect"}
    if project not in projects:
        return EXIT_OK, "No stop requested; continuing", {"action": "resume", "changed": False, "message": "No stop requested; continuing"}
    remaining = [p for p in projects if p != project]
    if remaining:
        data["projects"] = remaining
        tmp = stop_path.with_suffix(".stop.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(stop_path)
        return EXIT_OK, f"cleared stop for {project} in {stop_path}", {"action": "resume", "changed": True, "message": f"cleared stop for {project} in {stop_path}"}
    stop_path.unlink(missing_ok=True)
    return EXIT_OK, f"removed stop marker {stop_path}", {"action": "resume", "changed": True, "message": f"removed stop marker {stop_path}"}


def render_human_status(payload: dict) -> str:
    """Render human-readable status output."""
    marker, wake, orch = payload.get("desktop_stop_marker") or {}, payload.get("native_wake") or {}, payload.get("orchestrator") or {}
    lines = [
        f"Aru loop-control status: {payload['status']}",
        f"project: {payload['project'] or '(none)'}",
        f"desktop_stop_marker: present={marker.get('present')} applies={marker.get('applies')} scope={marker.get('scope')} reason={marker.get('reason')}",
        f"native_wake: enabled={wake.get('enabled')} automation_id={wake.get('automation_id')} evidence={wake.get('evidence')}",
        f"orchestrator: configured={orch.get('configured')} adapter={orch.get('adapter')} state={orch.get('state')} reason={orch.get('reason')}",
    ]
    if orch.get("detail"):
        lines.append(f"orchestrator_detail: {orch['detail']}")
    for c in payload.get("contradictions") or []:
        lines.append(f"contradiction: [{c['id']}] {c['detail']}")
    if payload.get("error"):
        lines.append(f"error: {payload['error']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    common_p = argparse.ArgumentParser(add_help=False)
    common_p.add_argument("--project", default=argparse.SUPPRESS, help="Absolute project path")
    common_p.add_argument("--target-home", default=argparse.SUPPRESS, help="Target home directory (overrides HOME)")
    common_p.add_argument("--orchestrator-adapter", default=argparse.SUPPRESS, help="Read-only orchestrator adapter command")
    common_p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")

    parser = argparse.ArgumentParser(parents=[common_p], description="Unified loop control, status inspection, and adapter contract.")
    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("status", parents=[common_p], help="Check loop control status")
    stop_p = subparsers.add_parser("stop", parents=[common_p], help="Persist explicit stop marker")
    stop_p.add_argument("--reason", default=DEFAULT_PAUSE_REASON, help="Bounded pause reason")
    resume_p = subparsers.add_parser("resume", parents=[common_p], help="Clear explicit stop marker")
    resume_p.add_argument("--reason", default=None, help="Bounded pause reason")

    args = parser.parse_args(argv)
    action = args.action or "status"
    target_home = Path(getattr(args, "target_home", None) or Path.home())
    project = getattr(args, "project", None)
    if project and not project.startswith("/"):
        print("error: --project must be an absolute path", file=sys.stderr)
        return EXIT_INVALID

    if action == "stop":
        try:
            res = execute_stop(target_home, project=project, reason=getattr(args, "reason", DEFAULT_PAUSE_REASON))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if getattr(args, "json", False):
            json.dump(res, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(f"wrote stop marker {res['stop_path']} for {res['token']}")
        return EXIT_OK

    if action == "resume":
        try:
            code, msg, res = execute_resume(target_home, project=project, reason=getattr(args, "reason", None))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if code != EXIT_OK:
            print(msg, file=sys.stderr)
            return code
        if getattr(args, "json", False):
            json.dump(res, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print(msg)
        return EXIT_OK

    payload = get_status(target_home, project=project, adapter_cmd=getattr(args, "orchestrator_adapter", None))
    if getattr(args, "json", False):
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human_status(payload))

    st = payload["status"]
    if st == "invalid":
        return EXIT_INVALID
    if st in {"degraded", "contradictory"}:
        return EXIT_DEGRADED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
