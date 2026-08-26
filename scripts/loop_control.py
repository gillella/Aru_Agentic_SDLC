#!/usr/bin/env python3
# line-ceiling: 460
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
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None

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
    if not isinstance(projects, list):
        return False
    if "*" in projects:
        return True
    if project and project in projects:
        return True
    return False


@contextmanager
def marker_lock(aru_dir: Path):
    """Serialise read-modify-write of the shared stop marker across processes.

    Two concurrent project-scoped stops otherwise read the same project list and
    write it back independently, so the second silently discards the first
    operator's durable stop intent.
    """
    aru_dir.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - non-POSIX host
        yield
        return
    with open(aru_dir / "factory-loop.stop.lock", "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_stop_marker(stop_path: Path, doc: dict) -> None:
    """Replace the marker atomically through a temp file unique to this call.

    A fixed `.tmp` sibling is shared by every concurrent writer, so one process
    can replace or unlink the half-written file another is still using.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(stop_path.parent), prefix=".factory-loop.stop.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_name, stop_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def parse_stop_reasons(data: dict) -> dict[str, str]:
    """Return the marker's per-project reason map, ignoring a malformed value."""
    raw = data.get("reasons")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str)}


def reason_for_project(data: dict, project: str | None) -> str:
    """Resolve the pause reason recorded for `project`, not for the last stop.

    The marker holds one entry per project token, because a single top-level
    `reason` made a later stop of project B rewrite project A's reason while
    both tokens stayed active. The top-level value is still honoured last so
    markers written before the map existed keep reporting their reason.
    """
    reasons = parse_stop_reasons(data)
    for token in ([project] if project else []) + ["*"]:
        if token in reasons:
            return reasons[token]
    legacy = data.get("reason")
    return legacy if isinstance(legacy, str) and legacy else DEFAULT_PAUSE_REASON


def parse_stop_projects(data: dict, stop_path: Path) -> list[str]:
    """Return the marker's project tokens, rejecting a non-list `projects`.

    A string, mapping, or number here would otherwise be silently coerced by
    `list()` into bogus per-character/per-key tokens, or raise a bare
    `TypeError` for a non-iterable, so every caller fails closed on it the same
    way `status` does.
    """
    raw = data.get("projects")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"malformed stop marker at {stop_path}: 'projects' must be a list")
    return [str(p) for p in raw]


def resolve_desktop_stop_marker(target_home: Path, project: str | None = None) -> dict:
    """Read and resolve the desktop stop marker under target_home/.aru."""
    stop_path = target_home / ".aru" / "factory-loop.stop"
    if not stop_path.is_file():
        return {"present": False, "applies": False, "scope": "none", "projects": [], "reason": None,
                "path": str(stop_path), "valid": True, "error": None}
    try:
        data = json.loads(stop_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"malformed stop marker at {stop_path}: expected JSON object")
    projects = parse_stop_projects(data, stop_path)
    scope = "global" if "*" in projects else ("project" if projects else "none")
    return {
        "present": True, "applies": stop_applies(data, project), "scope": scope,
        "projects": projects, "reason": reason_for_project(data, project), "path": str(stop_path),
        "valid": True, "error": None,
    }


def invalid_stop_marker(stop_path: Path, error: str) -> dict:
    """Describe a stop marker that exists on disk but cannot be parsed.

    Presence, path, and the parse error are preserved so a caller can tell a
    corrupt marker apart from an absent one; `applies` stays False so a marker
    nobody can read never silently authorises the loop to keep running.
    """
    return {
        "present": stop_path.is_file(), "applies": False, "scope": "none", "projects": [],
        "reason": None, "path": str(stop_path), "valid": False, "error": error,
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
    if not isinstance(entry, dict):
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
    if not isinstance(state, str) or state not in {"enabled", "paused", "unknown"}:
        state = "unknown"
    adapter_name = data.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        adapter_name = adapter_cmd
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    detail = data.get("detail")
    if detail is not None and not isinstance(detail, str):
        detail = str(detail)
    return {"configured": True, "adapter": adapter_name, "state": state, "reason": reason, "detail": detail, "error": None}


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
        stop_path = target_home / ".aru" / "factory-loop.stop"
        err = str(exc)
        marker = invalid_stop_marker(stop_path, err)
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
    stop_path = aru_dir / "factory-loop.stop"
    token = project or "*"
    with marker_lock(aru_dir):
        projects, reasons = [], {}
        if stop_path.is_file():
            try:
                data = json.loads(stop_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("expected JSON object")
            except Exception as exc:
                raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
            projects = parse_stop_projects(data, stop_path)
            reasons = parse_stop_reasons(data)
        if token not in projects:
            projects.append(token)
        reasons[token] = reason
        doc = {"projects": projects, "reasons": reasons, "stopped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": source, "reason": reason}
        write_stop_marker(stop_path, doc)
    return {"action": "stop", "changed": True, "token": token, "stop_path": str(stop_path), "reason": reason}


def execute_resume(target_home: Path, project: str | None = None, reason: str | None = None) -> tuple[int, str, dict]:
    """Resume loop by clearing stop marker."""
    if reason and reason not in PAUSE_REASONS:
        raise ValueError(f"invalid pause reason '{reason}'. Valid reasons: {', '.join(PAUSE_REASONS)}")
    if project and not project.startswith("/"):
        raise ValueError("--project must be an absolute path")
    aru_dir = target_home / ".aru"
    stop_path = aru_dir / "factory-loop.stop"
    no_stop = (EXIT_OK, "No stop requested; continuing", {"action": "resume", "changed": False, "message": "No stop requested; continuing"})
    if not stop_path.is_file():
        return no_stop
    # Locked for the same reason stop is: clearing one project rewrites the
    # list every other project's stop intent also lives in.
    with marker_lock(aru_dir):
        if not stop_path.is_file():
            return no_stop
        try:
            data = json.loads(stop_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected JSON object")
        except Exception as exc:
            raise ValueError(f"malformed stop marker at {stop_path}: {exc}") from exc
        projects = parse_stop_projects(data, stop_path)
        if not project:
            stop_path.unlink(missing_ok=True)
            return EXIT_OK, f"removed stop marker {stop_path}", {"action": "resume", "changed": True, "message": f"removed stop marker {stop_path}"}
        if "*" in projects:
            return EXIT_INVALID, "error: global stop (*) is in effect; resume without --project to clear it", {"action": "resume", "error": "global stop in effect"}
        if project not in projects:
            return no_stop
        remaining = [p for p in projects if p != project]
        if not remaining:
            stop_path.unlink(missing_ok=True)
            return EXIT_OK, f"removed stop marker {stop_path}", {"action": "resume", "changed": True, "message": f"removed stop marker {stop_path}"}
        reasons = parse_stop_reasons(data)
        reasons.pop(project, None)
        data["projects"], data["reasons"] = remaining, reasons
        write_stop_marker(stop_path, data)
        return EXIT_OK, f"cleared stop for {project} in {stop_path}", {"action": "resume", "changed": True, "message": f"cleared stop for {project} in {stop_path}"}


def render_human_status(payload: dict) -> str:
    """Render human-readable status output."""
    marker, wake, orch = payload.get("desktop_stop_marker") or {}, payload.get("native_wake") or {}, payload.get("orchestrator") or {}
    lines = [
        f"Aru loop-control status: {payload['status']}",
        f"project: {payload['project'] or '(none)'}",
        f"desktop_stop_marker: present={marker.get('present')} applies={marker.get('applies')} "
        f"valid={marker.get('valid')} scope={marker.get('scope')} reason={marker.get('reason')}",
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


def _handle_stop(target_home: Path, project: str | None, reason: str, is_json: bool) -> int:
    try:
        res = execute_stop(target_home, project=project, reason=reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if is_json:
        json.dump(res, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(f"wrote stop marker {res['stop_path']} for {res['token']}")
    return EXIT_OK


def _handle_resume(target_home: Path, project: str | None, reason: str | None, is_json: bool) -> int:
    try:
        code, msg, res = execute_resume(target_home, project=project, reason=reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if code != EXIT_OK:
        print(msg, file=sys.stderr)
        return code
    if is_json:
        json.dump(res, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(msg)
    return EXIT_OK


def _handle_status(target_home: Path, project: str | None, adapter_cmd: str | None, is_json: bool) -> int:
    payload = get_status(target_home, project=project, adapter_cmd=adapter_cmd)
    if is_json:
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

    is_json = bool(getattr(args, "json", False))
    if action == "stop":
        return _handle_stop(target_home, project, getattr(args, "reason", DEFAULT_PAUSE_REASON), is_json)
    if action == "resume":
        return _handle_resume(target_home, project, getattr(args, "reason", None), is_json)
    return _handle_status(target_home, project, getattr(args, "orchestrator_adapter", None), is_json)


if __name__ == "__main__":
    sys.exit(main())
