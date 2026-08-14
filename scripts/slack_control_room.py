#!/usr/bin/env python3
"""Aru Slack control-room bridge: routed operator commands, not a work queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fleet_status import evaluate_fleet_status
from slack_notify import (
    ENV_PATH,
    SlackConfig,
    config_for_project,
    config_from_env,
    load_slack_env,
    post_event,
    redact,
    secrets_from_config,
)
from slack_projects import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_REGISTRY_PATH,
    ProjectRecord,
    ProjectRegistry,
    RegistryError,
    mutate_secure_json,
    read_secure_json,
)

STOP_PATH = Path.home() / ".aru" / "factory-loop.stop"
PID_PATH = Path.home() / ".aru" / "slack-control-room.pid"
SEEN_PATH = Path.home() / ".aru" / "slack-control-room-seen.json"
BRIDGE_IDENTITY = "aru-slack-control-room"
_STATUS_LOCK = threading.Lock()
COMMAND_RE = re.compile(
    r"(?P<verb>status|stop|resume|intervention)\b(?:\s+(?P<rest>.+))?",
    re.IGNORECASE,
)
ISSUE_RE = re.compile(
    r"(?:(?P<kind>issue|pr)\s*[#:]?\s*(?P<numbered>\d+)|#(?P<hash>\d+))",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorize(config: SlackConfig, project: ProjectRecord, user_id: str) -> bool:
    operator = config.operator_user_id
    return bool(
        operator.startswith("U")
        and len(operator) >= 8
        and user_id == operator
        and config.team_id == project.slack_team_id
    )


def parse_command(text: str) -> Optional[Dict[str, str]]:
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", redact(text or "")).strip()
    match = COMMAND_RE.match(cleaned)
    if not match:
        return None
    verb = match.group("verb").lower()
    rest = (match.group("rest") or "").strip()
    parsed = {"verb": verb, "target": "project", "ref": "", "decision": rest}
    if verb in {"stop", "resume"}:
        parsed["target"] = rest.split()[0].lower() if rest else "project"
        parsed["decision"] = ""
    elif verb == "intervention":
        found = ISSUE_RE.search(rest)
        parsed["kind"] = "issue"
        if found:
            parsed["kind"] = (found.group("kind") or "issue").lower()
            parsed["ref"] = found.group("numbered") or found.group("hash") or ""
            parsed["decision"] = rest[found.end():].strip()
    return parsed


def _stop_document(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError("invalid factory-loop.stop document")
    projects, agents = value.get("projects", []), value.get("agents", [])
    if not isinstance(projects, list) or not all(isinstance(item, str) for item in projects):
        raise RegistryError("invalid factory-loop.stop projects")
    if not isinstance(agents, list) or not all(isinstance(item, str) for item in agents):
        raise RegistryError("invalid factory-loop.stop agents")
    return value


def _adopt_legacy_stop_file(path: Path) -> None:
    """Secure a regular legacy stop file without rejecting its old 0644 mode."""
    if not path.exists() and not path.is_symlink():
        return
    try:
        info = path.lstat()
    except OSError as exc:
        raise RegistryError(f"cannot inspect factory-loop.stop: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RegistryError(f"unsafe factory-loop.stop file: {path}")
    if info.st_uid != os.getuid():
        raise RegistryError(f"factory-loop.stop is not owned by the current user: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise RegistryError(f"factory-loop.stop is writable by another user: {path}")
    if mode != 0o600:
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise RegistryError(f"factory-loop.stop changed while securing it: {path}")
            if stat.S_IMODE(opened.st_mode) & 0o022:
                raise RegistryError(f"factory-loop.stop is writable by another user: {path}")
            os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise RegistryError(f"cannot secure factory-loop.stop: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def load_stop_file(path: Optional[Path] = None) -> Dict[str, Any]:
    destination = path or STOP_PATH
    _adopt_legacy_stop_file(destination)
    return _stop_document(read_secure_json(destination, {}))


def write_stop_file(
    projects: List[str], source: str, path: Optional[Path] = None,
    agents: Optional[List[str]] = None,
) -> None:
    destination = path or STOP_PATH
    _adopt_legacy_stop_file(destination)
    payload = {
        "projects": projects, "agents": list(agents or []),
        "stopped_at": _now(), "source": source,
    }
    mutate_secure_json(destination, {}, lambda _current: payload)


def agent_stop_token(project_path: str, agent: str) -> str:
    return f"{project_path}::{agent}"


def agent_stop_applies(
    project_path: str, agent: str, data: Optional[Dict[str, Any]] = None
) -> bool:
    document = _stop_document(data if data is not None else load_stop_file())
    return bool(
        "*" in document.get("projects", [])
        or project_path in document.get("projects", [])
        or agent_stop_token(project_path, agent) in document.get("agents", [])
    )


def apply_stop(project_path: str, target: str, path: Optional[Path] = None) -> str:
    if target == "all":
        return "global Slack stop is not supported; use the local operator control"

    def update(value: Any) -> Dict[str, Any]:
        document = _stop_document(value)
        projects, agents = list(document.get("projects", [])), list(document.get("agents", []))
        if target == "project":
            if project_path not in projects:
                projects.append(project_path)
        else:
            token = agent_stop_token(project_path, target)
            if token not in agents:
                agents.append(token)
        return {
            **document, "projects": projects, "agents": agents,
            "stopped_at": _now(), "source": "slack_control_room",
        }

    destination = path or STOP_PATH
    _adopt_legacy_stop_file(destination)
    mutate_secure_json(destination, {}, update)
    subject = "project" if target == "project" else target
    return f"stop recorded for {subject} (drain-first; loops check between units)"


def apply_resume(project_path: str, target: str, path: Optional[Path] = None) -> str:
    destination = path or STOP_PATH
    _adopt_legacy_stop_file(destination)
    if not destination.is_file():
        return "no operator stop is in effect"
    result = {"message": ""}

    def update(value: Any) -> Dict[str, Any]:
        document = _stop_document(value)
        projects, agents = list(document.get("projects", [])), list(document.get("agents", []))
        if "*" in projects:
            result["message"] = "global stop (*) is local-operator-only and cannot be cleared from Slack"
            return document
        if target == "all":
            result["message"] = "global Slack resume is not supported; use the local operator control"
            return document
        if target == "project":
            projects = [item for item in projects if item != project_path]
            agents = [item for item in agents if not item.startswith(f"{project_path}::")]
            result["message"] = "cleared operator stop for project"
        elif project_path in projects:
            result["message"] = "project stop is in effect; resume the project to clear it"
            return document
        else:
            agents = [item for item in agents if item != agent_stop_token(project_path, target)]
            result["message"] = f"cleared operator stop for {target}"
        return {
            **document, "projects": projects, "agents": agents,
            "stopped_at": _now(), "source": "slack_control_room",
        }

    mutate_secure_json(destination, {}, update)
    return result["message"]


def load_capacity(repo_dir: str) -> Dict[str, Any]:
    from common import list_open_issues
    from triage_backlog import capacity, partition

    original = os.getcwd()
    try:
        os.chdir(os.path.abspath(repo_dir))
        _backlog, ready, held = partition(list_open_issues() or [])
        return capacity(ready, held)
    except (OSError, TypeError, ValueError) as exc:
        return {"error": str(exc), "concurrent": [], "deferred": [], "ready_total": 0}
    finally:
        os.chdir(original)


def load_loop_heartbeats(project_path: str) -> List[str]:
    from doctor_local_agent_integrations import report

    aru_home = Path(os.environ.get("ARU_SDLC_HOME") or Path(__file__).resolve().parents[1])
    payload = report(aru_home, Path.home(), project_path)
    return [
        f"{name}: last_heartbeat={agent.get('last_heartbeat') or 'unknown'} "
        f"wake_evidence={agent.get('native_wake_evidence') or 'none'}"
        for name, agent in (payload.get("agents") or {}).items()
    ]


def verified_runtime_health(registry: ProjectRegistry, project: ProjectRecord) -> str:
    checked = registry.verify(project.project_id)
    runtime = str(checked.get("runtime_health") or "degraded_unreachable")
    if runtime != "healthy":
        return runtime
    if checked.get("identity_verified"):
        return "healthy"
    return (
        "degraded_identity_unverified"
        if checked.get("verification_error")
        else "degraded_identity_mismatch"
    )


def status_text(project: ProjectRecord, runtime_health: Optional[str] = None) -> str:
    health = runtime_health or ("healthy" if project.healthy else "degraded_unreachable")
    if health != "healthy":
        return (
            f"project {project.project_id}: {health}; checkout unavailable or identity "
            f"unverified at {project.local_path}. Use registry verify, recover, or close locally."
        )
    with _STATUS_LOCK:
        status = evaluate_fleet_status(project.local_path)
        capacity = load_capacity(project.local_path)
    lines = [
        f"project {project.project_id}: healthy",
        f"factory state: {status.get('state')} ({status.get('summary', '')})",
        f"open issues: {status.get('open_issues_count', '?')} open PRs: {status.get('open_prs_count', '?')}",
    ]
    if capacity.get("error"):
        lines.append(f"capacity: unavailable ({capacity['error']})")
    else:
        concurrent = capacity.get("concurrent") or []
        lines.append(
            f"capacity: claimable={len(concurrent)} ready={capacity.get('ready_total', '?')} "
            f"concurrent={concurrent}"
        )
    reviews = [item for item in (status.get("reasons") or []) if "review" in str(item).lower()]
    lines.append("open review work: " + ("; ".join(reviews[:8]) if reviews else "none"))
    stop = load_stop_file()
    scoped_projects = [
        item for item in stop.get("projects", [])
        if item in {"*", project.local_path}
    ]
    scoped_agents = [
        item for item in stop.get("agents", [])
        if item.startswith(f"{project.local_path}::")
    ]
    if scoped_projects or scoped_agents:
        lines.append(f"operator stop: projects={scoped_projects} agents={scoped_agents}")
    lines.append("loop heartbeats:")
    lines.extend(f"  {item}" for item in load_loop_heartbeats(project.local_path))
    return "\n".join(lines)


def parse_ref(ref: str, kind: str = "issue") -> Tuple[str, int]:
    return ("pr" if kind.lower() == "pr" else "issue", int(ref))


def github_comment(kind: str, number: int, decision: str, repo_dir: str) -> bool:
    from common import run_cmd

    body = f"Operator intervention via Slack control room ({_now()}):\n\n{decision}\n"
    code, _, _ = run_cmd(
        ["gh", "issue" if kind == "issue" else "pr", "comment", str(number), "--body", body],
        check=False, cwd=repo_dir,
    )
    return code == 0


def handle_command(
    config: SlackConfig, parsed: Dict[str, str], project: ProjectRecord,
    comment: Callable[[str, int, str, str], bool] = github_comment,
    runtime_health: Optional[str] = None,
) -> str:
    verb = parsed["verb"]
    health = runtime_health or ("healthy" if project.healthy else "degraded_unreachable")
    if verb == "status":
        return status_text(project, health)
    if health != "healthy":
        return "project checkout is degraded; use registry verify, recover, or close locally"
    if verb == "stop":
        return apply_stop(project.local_path, parsed.get("target") or "project")
    if verb == "resume":
        return apply_resume(project.local_path, parsed.get("target") or "project")
    if verb == "intervention":
        ref, decision = parsed.get("ref") or "", parsed.get("decision") or ""
        if not ref or not decision:
            return "intervention needs `#<issue-or-pr> <decision>`"
        kind, number = parse_ref(ref, parsed.get("kind") or "issue")
        safe_decision = redact(decision, extra=secrets_from_config(config))
        if not comment(kind, number, safe_decision, project.local_path):
            return f"could not copy intervention onto GitHub {kind} #{number}"
        return f"copied intervention to GitHub {kind} #{number}"
    return "unknown command"


def load_seen_ids(path: Optional[Path] = None) -> Dict[str, str]:
    payload = read_secure_json(path or SEEN_PATH, {"ids": {}})
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, dict) or not all(isinstance(key, str) for key in ids):
        raise RegistryError("invalid Slack deduplication store")
    return {key: str(value) for key, value in ids.items()}


def record_seen_id(
    event_key: str, path: Optional[Path] = None, legacy_key: str = "",
) -> bool:
    duplicate = {"value": False}

    def update(payload: Any) -> Dict[str, Any]:
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if not isinstance(ids, dict):
            raise RegistryError("invalid Slack deduplication store")
        if event_key in ids or (legacy_key and legacy_key in ids):
            duplicate["value"] = True
            return payload
        ids[event_key] = _now()
        return {"ids": dict(list(ids.items())[-1500:])}

    mutate_secure_json(path or SEEN_PATH, {"ids": {}}, update)
    return duplicate["value"]


def handle_slack_message(
    config: SlackConfig, registry: ProjectRegistry, payload: Dict[str, Any], seen_ids: set[str],
    comment: Callable[[str, int, str, str], bool] = github_comment,
    notify: Callable[..., Dict[str, Any]] = post_event,
    seen_path: Optional[Path] = None,
) -> Optional[str]:
    team_id = str(payload.get("team") or payload.get("team_id") or "")
    channel_id = str(payload.get("channel") or "")
    try:
        project = registry.resolve(team_id, channel_id)
    except RegistryError:
        route_hash = hashlib.sha256(f"{team_id}\0{channel_id}".encode()).hexdigest()[:16]
        registry.audit(
            "invalid_inbound_route", "slack_bridge",
            detail=(
                f"team={redact(team_id) or 'missing'} "
                f"channel={redact(channel_id) or 'missing'}"
            ),
            throttle_key=f"invalid-route:{route_hash}",
        )
        return None
    if not authorize(config, project, str(payload.get("user") or "")):
        return None
    try:
        runtime_health = verified_runtime_health(registry, project)
    except RegistryError:
        return None
    event_id = str(payload.get("client_msg_id") or payload.get("event_id") or payload.get("ts") or "")
    event_key = "|".join((project.project_id, team_id, channel_id, event_id))
    try:
        if event_id:
            if (
                event_key in seen_ids
                or event_id in seen_ids
                or record_seen_id(event_key, seen_path, legacy_key=event_id)
            ):
                return None
            seen_ids.add(event_key)
        parsed = parse_command(str(payload.get("text") or ""))
        if not parsed:
            return None
        reply = handle_command(config, parsed, project, comment, runtime_health)
    except RegistryError as exc:
        print(
            f"[ERROR] control-room state is unusable: {redact(str(exc))}",
            file=sys.stderr,
        )
        reply = "control-room state is unusable; inspect and repair the bridge host locally"
        parsed = {"verb": "state-error"}
    notify(
        config_for_project(config, project),
        {
            "project_id": project.project_id, "type": "command-ack",
            "agent": "slack-bridge", "family": "human", "text": reply,
            "dedupe_key": f"{project.project_id}:{team_id}:{channel_id}:ack:{event_id}:{parsed['verb']}",
        },
    )
    return reply


def doctor(
    env_path: Path = ENV_PATH, registry_path: Path = DEFAULT_REGISTRY_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "env_file": str(env_path), "env_file_present": env_path.is_file(),
        "registry_file": str(registry_path), "bolt_installed": _bolt_available(),
        "bridge_running": _bridge_running(PID_PATH), "ok": False,
    }
    try:
        config = config_from_env(load_slack_env(env_path), require_channel=False)
        projects = ProjectRegistry(registry_path, audit_path).list(include_closed=False)
        mismatched = [item.project_id for item in projects if item.slack_team_id != config.team_id]
        if mismatched:
            raise RegistryError(f"projects belong to another Slack workspace: {mismatched}")
    except (ValueError, RegistryError) as exc:
        report["error"] = str(exc)
        return report
    report.update({
        "ok": True, "team_id": config.team_id, "active_projects": len(projects),
        "degraded_projects": [item.project_id for item in projects if not item.healthy],
        "operator_configured": bool(config.operator_user_id),
        "socket_token_present": config.app_token.startswith("xapp-"),
    })
    return report


def _bolt_available() -> bool:
    try:
        import slack_bolt  # noqa: F401
        return True
    except ImportError:
        return False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_command(pid: int) -> str:
    from common import run_cmd
    code, stdout, _ = run_cmd(["ps", "-p", str(pid), "-o", "command="], check=False)
    return stdout if code == 0 else ""


def _pid_record(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _is_our_bridge(pid: int) -> bool:
    return "slack_control_room" in _process_command(pid)


def _bridge_running(path: Path) -> bool:
    try:
        pid = int(_pid_record(path).get("pid"))
    except (TypeError, ValueError):
        return False
    return _pid_exists(pid) and _is_our_bridge(pid)


def write_pid(path: Path = PID_PATH) -> None:
    payload = {
        "pid": os.getpid(), "identity": BRIDGE_IDENTITY, "started_at": _now(),
        "argv": Path(sys.argv[0]).name if sys.argv else "slack_control_room.py",
    }
    mutate_secure_json(path, {}, lambda _current: payload)


def clear_pid(path: Path = PID_PATH) -> None:
    if path.is_file():
        path.unlink()


def stop_bridge() -> str:
    if not PID_PATH.is_file():
        return "bridge is not running"
    try:
        pid = int(_pid_record(PID_PATH).get("pid"))
    except (TypeError, ValueError):
        clear_pid(PID_PATH)
        return "bridge is not running"
    if _pid_exists(pid) and not _is_our_bridge(pid):
        return f"refusing to signal pid {pid}: not the control-room bridge"
    if not _pid_exists(pid):
        clear_pid(PID_PATH)
        return "bridge is not running"
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        clear_pid(PID_PATH)
        return "bridge is not running"
    except OSError as exc:
        return f"could not signal pid {pid}: {exc}"
    for _ in range(20):
        if not _pid_exists(pid):
            clear_pid(PID_PATH)
            return "bridge stopped"
        time.sleep(0.1)
    return "bridge sent SIGTERM; pid file still present"


def start_bridge(config: SlackConfig, registry: ProjectRegistry) -> int:
    if not (config.operator_user_id.startswith("U") and len(config.operator_user_id) >= 8):
        print("[ERROR] SLACK_OPERATOR_USER_ID is required; commands fail closed.", file=sys.stderr)
        return 1
    try:
        mismatched = [
            item.project_id
            for item in registry.list(include_closed=False)
            if item.slack_team_id != config.team_id
        ]
    except RegistryError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if mismatched:
        print(f"[ERROR] projects belong to another Slack workspace: {mismatched}", file=sys.stderr)
        return 1
    if not _bolt_available():
        print("[ERROR] slack-bolt is not installed. pip install -r requirements-slack.txt", file=sys.stderr)
        return 1
    if not config.app_token.startswith("xapp-"):
        print("[ERROR] SLACK_APP_TOKEN (xapp-) is required for Socket Mode", file=sys.stderr)
        return 1
    if _bridge_running(PID_PATH):
        print("[ERROR] bridge already running", file=sys.stderr)
        return 1
    if PID_PATH.is_file():
        clear_pid(PID_PATH)
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=config.bot_token)
    seen = set(load_seen_ids())

    @app.event("app_mention")
    def _mention(body, event):  # pragma: no cover - live Slack path
        payload = dict(event)
        payload["team"] = body.get("team_id") or event.get("team")
        handle_slack_message(config, registry, payload, seen)

    write_pid(PID_PATH)
    try:
        SocketModeHandler(app, config.app_token).start()
    finally:
        clear_pid(PID_PATH)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status", "doctor", "stop"])
    parser.add_argument("--project-id")
    parser.add_argument("--registry-file", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--env-file", type=Path, default=ENV_PATH)
    args = parser.parse_args(argv)
    registry = ProjectRegistry(args.registry_file, args.audit_file)
    if args.command == "doctor":
        report = doctor(args.env_file, args.registry_file, args.audit_file)
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1
    if args.command == "stop":
        print(stop_bridge())
        return 0
    if args.command == "status":
        if not args.project_id:
            parser.error("status requires --project-id")
        try:
            project = registry.get(args.project_id)
            print(status_text(project, verified_runtime_health(registry, project)))
            return 0
        except RegistryError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
    try:
        config = config_from_env(load_slack_env(args.env_file), require_channel=False)
        registry.list(include_closed=False)
    except (ValueError, RegistryError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return start_bridge(config, registry)


if __name__ == "__main__":
    sys.exit(main())
