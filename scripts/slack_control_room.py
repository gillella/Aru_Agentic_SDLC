#!/usr/bin/env python3
"""Aru Slack control-room bridge: operator commands, not a second work queue."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fleet_status import evaluate_fleet_status
from slack_notify import (
    ENV_PATH,
    SlackConfig,
    config_from_env,
    load_slack_env,
    post_event,
    redact,
    secrets_from_config,
)

STOP_PATH = Path.home() / ".aru" / "factory-loop.stop"
PID_PATH = Path.home() / ".aru" / "slack-control-room.pid"
SEEN_PATH = Path.home() / ".aru" / "slack-control-room-seen.json"
BRIDGE_IDENTITY = "aru-slack-control-room"
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


def authorize(config: SlackConfig, team_id: str, channel_id: str, user_id: str) -> bool:
    operator = config.operator_user_id
    if not (operator.startswith("U") and len(operator) >= 8):
        return False
    if team_id != config.team_id or channel_id != config.channel_id:
        return False
    return user_id == operator


def parse_command(text: str) -> Optional[Dict[str, str]]:
    cleaned = redact(text or "")
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", cleaned).strip()
    match = COMMAND_RE.match(cleaned)
    if not match:
        return None
    verb = match.group("verb").lower()
    rest = (match.group("rest") or "").strip()
    parsed = {"verb": verb, "target": "all", "ref": "", "decision": rest}
    if verb in {"stop", "resume"}:
        parsed["target"] = rest.split()[0].lower() if rest else "all"
        parsed["decision"] = ""
    elif verb == "intervention":
        found = ISSUE_RE.search(rest)
        parsed["kind"] = "issue"
        parsed["ref"] = ""
        if found:
            parsed["kind"] = (found.group("kind") or "issue").lower()
            parsed["ref"] = found.group("numbered") or found.group("hash") or ""
            parsed["decision"] = rest[found.end():].strip()
    return parsed


def load_stop_file(path: Optional[Path] = None) -> Dict[str, Any]:
    dest = path or STOP_PATH
    if not dest.is_file():
        return {}
    try:
        return json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_stop_file(
    projects: List[str],
    source: str,
    path: Optional[Path] = None,
    agents: Optional[List[str]] = None,
) -> None:
    dest = path or STOP_PATH
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "projects": projects,
        "agents": list(agents or []),
        "stopped_at": _now(),
        "source": source,
    }
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    dest.chmod(0o600)


def agent_stop_token(project: str, agent: str) -> str:
    return f"{project}::{agent}"


def agent_stop_applies(project: str, agent: str, data: Optional[Dict[str, Any]] = None) -> bool:
    doc = data if data is not None else load_stop_file()
    projects = list(doc.get("projects") or [])
    agents = list(doc.get("agents") or [])
    if "*" in projects or project in projects:
        return True
    return agent_stop_token(project, agent) in agents


def apply_stop(project: str, target: str) -> str:
    data = load_stop_file()
    projects = list(data.get("projects") or [])
    agents = list(data.get("agents") or [])
    if target == "all":
        if "*" not in projects:
            projects.append("*")
        write_stop_file(projects, "slack_control_room", agents=agents)
        return "stop recorded for * (drain-first; loops check between units)"
    token = agent_stop_token(project, target)
    if token not in agents:
        agents.append(token)
    write_stop_file(projects, "slack_control_room", agents=agents)
    return f"stop recorded for {target} (drain-first; loops check between units)"


def apply_resume(project: str, target: str) -> str:
    if not STOP_PATH.is_file():
        return "no operator stop is in effect"
    data = load_stop_file()
    projects = list(data.get("projects") or [])
    agents = list(data.get("agents") or [])
    scoped = target not in {"", "all"}
    if not scoped:
        STOP_PATH.unlink()
        return "cleared operator stop for all projects"
    if "*" in projects:
        return "global stop (*) is in effect; resume all to clear it"
    if project in projects:
        return f"project stop for {project} is in effect; resume all to clear it"
    token = agent_stop_token(project, target)
    agents = [item for item in agents if item != token]
    if not projects and not agents:
        STOP_PATH.unlink()
        return f"cleared operator stop for {target}"
    write_stop_file(projects, "slack_control_room", agents=agents)
    return f"cleared operator stop for {target}"


def load_capacity(repo_dir: str) -> Dict[str, Any]:
    from common import list_open_issues
    from triage_backlog import capacity, partition

    original = os.getcwd()
    try:
        os.chdir(os.path.abspath(repo_dir))
        issues = list_open_issues() or []
        _backlog, ready, held = partition(issues)
        return capacity(ready, held)
    except (OSError, TypeError, ValueError) as exc:
        return {"error": str(exc), "concurrent": [], "deferred": [], "ready_total": 0}
    finally:
        os.chdir(original)


def load_loop_heartbeats(project: str) -> List[str]:
    from doctor_local_agent_integrations import report

    aru_home = Path(os.environ.get("ARU_SDLC_HOME") or Path(__file__).resolve().parents[1])
    abs_project = project if str(project).startswith("/") else str(Path(project or ".").resolve())
    payload = report(aru_home, Path.home(), abs_project)
    lines = []
    for name, agent in (payload.get("agents") or {}).items():
        last = agent.get("last_heartbeat") or "unknown"
        evidence = agent.get("native_wake_evidence") or "none"
        lines.append(f"{name}: last_heartbeat={last} wake_evidence={evidence}")
    return lines


def _review_work_lines(status: Dict[str, Any]) -> List[str]:
    markers = ("In Review", "pending review", "requested changes")
    return [item for item in (status.get("reasons") or []) if any(mark in str(item) for mark in markers)]


def status_text(repo_dir: str = ".", project: str = "") -> str:
    status = evaluate_fleet_status(repo_dir)
    health = status.get("codebase_health") or {}
    cap = load_capacity(repo_dir)
    target = project or str(Path(repo_dir).resolve())
    beats = load_loop_heartbeats(target)
    lines = [
        f"factory state: {status.get('state')} ({status.get('summary', '')})",
        f"open issues: {status.get('open_issues_count', '?')} open PRs: {status.get('open_prs_count', '?')}",
    ]
    if cap.get("error"):
        lines.append(f"capacity: unavailable ({cap['error']})")
    else:
        concurrent = cap.get("concurrent") or []
        lines.append(
            f"capacity: claimable={len(concurrent)} ready={cap.get('ready_total', '?')} "
            f"concurrent={concurrent}"
        )
    reviews = _review_work_lines(status)
    lines.append("open review work: " + ("; ".join(reviews[:8]) if reviews else "none"))
    claims = status.get("active_claims") or []
    if claims:
        lines.append("claims: " + ", ".join(str(item) for item in claims[:8]))
    if health:
        lines.append(
            f"codebase loc={health.get('loc')} files={health.get('file_count')}"
        )
    stop = load_stop_file()
    if stop.get("projects") or stop.get("agents"):
        lines.append(
            f"operator stop: projects={stop.get('projects')} agents={stop.get('agents')} "
            f"at {stop.get('stopped_at')}"
        )
    lines.append("loop heartbeats:")
    lines.extend(f"  {item}" for item in beats)
    return "\n".join(lines)


def parse_ref(ref: str, kind: str = "issue") -> Tuple[str, int]:
    token = "pr" if kind.lower() == "pr" else "issue"
    return (token, int(ref))


def handle_command(
    config: SlackConfig,
    parsed: Dict[str, str],
    project: str,
    repo_dir: str,
    comment: Callable[[str, int, str, str], bool],
) -> str:
    verb = parsed["verb"]
    if verb == "status":
        return status_text(repo_dir, project)
    if verb == "stop":
        return apply_stop(project, parsed.get("target") or "all")
    if verb == "resume":
        return apply_resume(project, parsed.get("target") or "all")
    if verb == "intervention":
        ref = parsed.get("ref") or ""
        raw = parsed.get("decision") or ""
        if not ref or not raw:
            return "intervention needs `#<issue-or-pr> <decision>`"
        kind, number = parse_ref(ref, parsed.get("kind") or "issue")
        decision = redact(raw, extra=secrets_from_config(config))
        ok = comment(kind, number, decision, repo_dir)
        if not ok:
            return f"could not copy intervention onto GitHub {kind} #{number}"
        return f"copied intervention to GitHub {kind} #{number}"
    return "unknown command"


def github_comment(kind: str, number: int, decision: str, repo_dir: str = ".") -> bool:
    from common import run_cmd

    body = (
        "Operator intervention via Slack control room "
        f"({_now()}):\n\n{decision}\n"
    )
    resource = "issue" if kind == "issue" else "pr"
    code, _, _ = run_cmd(
        ["gh", resource, "comment", str(number), "--body", body],
        check=False,
        cwd=repo_dir,
    )
    return code == 0


def load_seen_ids(path: Optional[Path] = None) -> Dict[str, str]:
    dest = path or SEEN_PATH
    if not dest.is_file():
        return {}
    try:
        payload = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ids = payload.get("ids") if isinstance(payload, dict) else payload
    if isinstance(ids, dict):
        return {str(key): str(value) for key, value in ids.items()}
    if isinstance(ids, list):
        return {str(item): "" for item in ids}
    return {}


def record_seen_id(event_id: str, path: Optional[Path] = None) -> bool:
    """Persist event_id. Return True if it was already recorded."""
    dest = path or SEEN_PATH
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    if not dest.exists():
        dest.write_text("{}\n", encoding="utf-8")
        dest.chmod(0o600)
    with dest.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            payload = json.loads(handle.read() or "{}")
        except json.JSONDecodeError:
            payload = {}
        ids = payload.get("ids") if isinstance(payload, dict) else {}
        if not isinstance(ids, dict):
            ids = {}
        if event_id in ids:
            return True
        ids[event_id] = _now()
        if len(ids) > 2000:
            ids = dict(list(ids.items())[-1500:])
        handle.seek(0)
        handle.truncate()
        json.dump({"ids": ids}, handle, indent=2, sort_keys=True)
        handle.write("\n")
    dest.chmod(0o600)
    return False


def handle_slack_message(
    config: SlackConfig,
    payload: Dict[str, Any],
    seen_ids: set[str],
    project: str,
    repo_dir: str,
    comment: Callable[[str, int, str, str], bool] = github_comment,
    notify: Callable[..., Dict[str, Any]] = post_event,
) -> Optional[str]:
    event_id = str(payload.get("client_msg_id") or payload.get("ts") or "")
    if event_id:
        already = event_id in seen_ids or record_seen_id(event_id)
        seen_ids.add(event_id)
        if already:
            return None
    if not authorize(
        config,
        str(payload.get("team") or payload.get("team_id") or ""),
        str(payload.get("channel") or ""),
        str(payload.get("user") or ""),
    ):
        return None
    parsed = parse_command(str(payload.get("text") or ""))
    if not parsed:
        return None
    reply = handle_command(config, parsed, project, repo_dir, comment)
    notify(
        config,
        {
            "type": "command-ack",
            "agent": "slack-bridge",
            "family": "human",
            "text": reply,
            "dedupe_key": f"ack:{event_id}:{parsed['verb']}",
        },
    )
    return reply


def doctor(env_path: Path = ENV_PATH) -> Dict[str, Any]:
    values = load_slack_env(env_path)
    report: Dict[str, Any] = {
        "env_file": str(env_path),
        "env_file_present": env_path.is_file(),
        "bolt_installed": _bolt_available(),
        "pid_file": str(PID_PATH),
        "bridge_running": _bridge_running(PID_PATH),
        "ok": False,
    }
    try:
        config = config_from_env(values)
    except ValueError as exc:
        report["error"] = str(exc)
        return report
    report.update(
        {
            "ok": True,
            "team_id": config.team_id,
            "channel_id": config.channel_id,
            "operator_configured": bool(config.operator_user_id),
            "socket_token_present": config.app_token.startswith("xapp-"),
        }
    )
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
    text = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("pid") is not None:
            return data
    except json.JSONDecodeError:
        pass
    try:
        return {"pid": int(text), "identity": ""}
    except ValueError:
        return {}


def _is_our_bridge(pid: int) -> bool:
    command = _process_command(pid)
    return "slack_control_room" in command


def _bridge_running(path: Path) -> bool:
    record = _pid_record(path)
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return False
    return _pid_exists(pid) and _is_our_bridge(pid)


def write_pid(path: Path = PID_PATH) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "identity": BRIDGE_IDENTITY,
        "started_at": _now(),
        "argv": Path(sys.argv[0]).name if sys.argv else "slack_control_room.py",
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    path.chmod(0o600)


def clear_pid(path: Path = PID_PATH) -> None:
    if path.is_file():
        path.unlink()


def stop_bridge() -> str:
    if not PID_PATH.is_file():
        return "bridge is not running"
    record = _pid_record(PID_PATH)
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        clear_pid()
        return "bridge is not running"
    if _pid_exists(pid) and not _is_our_bridge(pid):
        return f"refusing to signal pid {pid}: not the control-room bridge"
    if not _pid_exists(pid):
        clear_pid()
        return "bridge is not running"
    try:
        os.kill(pid, 15)
    except OSError as exc:
        clear_pid()
        return f"bridge stop failed: {exc}"
    for _ in range(20):
        if not _pid_exists(pid):
            clear_pid()
            return "bridge stopped"
        time.sleep(0.1)
    return "bridge sent SIGTERM; pid file still present"


def start_bridge(config: SlackConfig, project: str, repo_dir: str) -> int:
    if not (config.operator_user_id.startswith("U") and len(config.operator_user_id) >= 8):
        print(
            "[ERROR] SLACK_OPERATOR_USER_ID is required; commands fail closed.",
            file=sys.stderr,
        )
        return 1
    if not _bolt_available():
        print(
            "[ERROR] slack-bolt is not installed. "
            "pip install -r requirements-slack.txt",
            file=sys.stderr,
        )
        return 1
    if not config.app_token.startswith("xapp-"):
        print("[ERROR] SLACK_APP_TOKEN (xapp-) is required for Socket Mode", file=sys.stderr)
        return 1
    if _bridge_running(PID_PATH):
        print("[ERROR] bridge already running", file=sys.stderr)
        return 1
    if PID_PATH.is_file():
        clear_pid()
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    app = App(token=config.bot_token)
    seen: set[str] = set(load_seen_ids())

    @app.event("app_mention")
    def _mention(body, event):  # pragma: no cover - live Slack path
        payload = dict(event)
        payload["team"] = body.get("team_id") or event.get("team")
        handle_slack_message(config, payload, seen, project, repo_dir)

    write_pid()
    try:
        SocketModeHandler(app, config.app_token).start()
    finally:
        clear_pid()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aru Slack control-room bridge")
    parser.add_argument("command", choices=["start", "status", "doctor", "stop"])
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--project", default="")
    parser.add_argument("--env-file", default=str(ENV_PATH))
    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = doctor(Path(args.env_file))
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1
    if args.command == "status":
        print(status_text(args.repo_dir, args.project))
        return 0
    if args.command == "stop":
        print(stop_bridge())
        return 0
    try:
        config = config_from_env(load_slack_env(Path(args.env_file)))
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    project = args.project or str(Path(args.repo_dir).resolve())
    return start_bridge(config, project, args.repo_dir)


if __name__ == "__main__":
    sys.exit(main())
