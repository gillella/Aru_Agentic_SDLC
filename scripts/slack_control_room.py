#!/usr/bin/env python3
"""Aru Slack control-room bridge: operator commands, not a second work queue."""

from __future__ import annotations

import argparse
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
)

STOP_PATH = Path.home() / ".aru" / "factory-loop.stop"
PID_PATH = Path.home() / ".aru" / "slack-control-room.pid"
COMMAND_RE = re.compile(
    r"(?P<verb>status|stop|resume|intervention)\b(?:\s+(?P<rest>.+))?",
    re.IGNORECASE,
)
ISSUE_RE = re.compile(r"#(\d+)|(?:issue|pr)\s*[#:]?\s*(\d+)", re.IGNORECASE)


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
        parsed["ref"] = next((g for g in found.groups() if g), "") if found else ""
        if found:
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


def write_stop_file(projects: List[str], source: str, path: Optional[Path] = None) -> None:
    dest = path or STOP_PATH
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {"projects": projects, "stopped_at": _now(), "source": source}
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    dest.chmod(0o600)


def apply_stop(project: str, target: str) -> str:
    data = load_stop_file()
    projects = list(data.get("projects") or [])
    token = "*" if target == "all" else (project or "*")
    if token not in projects:
        projects.append(token)
    write_stop_file(projects, "slack_control_room")
    return f"stop recorded for {token} (drain-first; loops check between units)"


def apply_resume(project: str, target: str) -> str:
    if not STOP_PATH.is_file():
        return "no operator stop is in effect"
    data = load_stop_file()
    projects = list(data.get("projects") or [])
    scoped = target not in {"", "all"}
    if scoped and "*" in projects:
        return "global stop (*) is in effect; resume all to clear it"
    if not scoped:
        STOP_PATH.unlink()
        return "cleared operator stop for all projects"
    projects = [item for item in projects if item != project]
    if not projects:
        STOP_PATH.unlink()
        return f"cleared operator stop for {project}"
    write_stop_file(projects, "slack_control_room")
    return f"cleared operator stop for {project}"


def status_text(repo_dir: str = ".") -> str:
    status = evaluate_fleet_status(repo_dir)
    health = status.get("codebase_health") or {}
    lines = [
        f"factory state: {status.get('state')} ({status.get('summary', '')})",
        f"open issues: {status.get('open_issues_count', '?')} open PRs: {status.get('open_prs_count', '?')}",
    ]
    claims = status.get("active_claims") or []
    if claims:
        lines.append("claims: " + ", ".join(str(item) for item in claims[:8]))
    if health:
        lines.append(
            f"codebase loc={health.get('loc')} files={health.get('file_count')}"
        )
    stop = load_stop_file()
    if stop.get("projects"):
        lines.append(f"operator stop: {stop.get('projects')} at {stop.get('stopped_at')}")
    return "\n".join(lines)


def parse_ref(ref: str) -> Tuple[str, int]:
    number = int(ref)
    return ("issue", number)


def handle_command(
    config: SlackConfig,
    parsed: Dict[str, str],
    project: str,
    repo_dir: str,
    comment: Callable[[str, int, str, str], bool],
) -> str:
    verb = parsed["verb"]
    if verb == "status":
        return status_text(repo_dir)
    if verb == "stop":
        return apply_stop(project, parsed.get("target") or "all")
    if verb == "resume":
        return apply_resume(project, parsed.get("target") or "all")
    if verb == "intervention":
        ref = parsed.get("ref") or ""
        decision = redact(parsed.get("decision") or "")
        if not ref or not decision:
            return "intervention needs `#<issue-or-pr> <decision>`"
        kind, number = parse_ref(ref)
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
    if event_id and event_id in seen_ids:
        return None
    if event_id:
        seen_ids.add(event_id)
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
        "bridge_running": _pid_alive(PID_PATH),
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


def _pid_alive(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def write_pid(path: Path = PID_PATH) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    path.chmod(0o600)


def clear_pid(path: Path = PID_PATH) -> None:
    if path.is_file():
        path.unlink()


def stop_bridge() -> str:
    if not PID_PATH.is_file():
        return "bridge is not running"
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, 15)
    except (OSError, ValueError) as exc:
        clear_pid()
        return f"bridge stop failed: {exc}"
    for _ in range(20):
        if not _pid_alive(PID_PATH):
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
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    if not config.app_token.startswith("xapp-"):
        print("[ERROR] SLACK_APP_TOKEN (xapp-) is required for Socket Mode", file=sys.stderr)
        return 1
    app = App(token=config.bot_token)
    seen: set[str] = set()

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
        print(status_text(args.repo_dir))
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
