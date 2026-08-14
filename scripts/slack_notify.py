#!/usr/bin/env python3
"""Post stamped Slack events for the Aru factory control room.

GitHub remains the work queue. Slack downtime must not halt factory work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENV_PATH = Path.home() / ".aru" / "slack.env"
DEDUPE_PATH = Path.home() / ".aru" / "slack-notify-dedupe.json"
SECRET_RE = re.compile(
    r"(xox[baprs]-[A-Za-z0-9-]{8,}|xapp-[A-Za-z0-9-]{8,}|Bearer\s+\S+)",
    re.IGNORECASE,
)
ENV_KEYS = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_TEAM_ID",
    "SLACK_CHANNEL_ID",
    "SLACK_OPERATOR_USER_ID",
    "SLACK_CHANNEL_NAME",
)
ALERT_TYPES = frozenset({"blocked", "waiting-on", "hitl"})
FORBIDDEN_TYPES = frozenset(
    {
        "heartbeat",
        "diff",
        "diffs",
        "prompt",
        "prompts",
        "token",
        "tokens",
        "test-log",
        "test_log",
        "test-logs",
    }
)


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    team_id: str
    channel_id: str
    operator_user_id: str = ""
    app_token: str = ""
    signing_secret: str = ""
    channel_name: str = "project-aru-code"


def load_slack_env(path: Path = ENV_PATH) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def config_from_env(values: Dict[str, str], require_channel: bool = True) -> SlackConfig:
    token = values.get("SLACK_BOT_TOKEN", "")
    team = values.get("SLACK_TEAM_ID", "")
    channel = values.get("SLACK_CHANNEL_ID", "")
    if not token.startswith("xoxb-") or len(token) < 40:
        raise ValueError("SLACK_BOT_TOKEN is missing or not a bot token")
    if not team.startswith("T") or len(team) < 8:
        raise ValueError("SLACK_TEAM_ID is missing")
    if require_channel and (
        not (channel.startswith("C") or channel.startswith("G")) or len(channel) < 8
    ):
        raise ValueError("SLACK_CHANNEL_ID is missing")
    return SlackConfig(
        bot_token=token,
        team_id=team,
        channel_id=channel,
        operator_user_id=values.get("SLACK_OPERATOR_USER_ID", ""),
        app_token=values.get("SLACK_APP_TOKEN", ""),
        signing_secret=values.get("SLACK_SIGNING_SECRET", ""),
        channel_name=values.get("SLACK_CHANNEL_NAME", "project-aru-code").lstrip("#"),
    )


def redact(text: str, extra: Optional[list[str]] = None) -> str:
    out = SECRET_RE.sub("[redacted]", text or "")
    for secret in extra or []:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def secrets_from_config(config: SlackConfig) -> list[str]:
    return [value for value in (config.bot_token, config.app_token, config.signing_secret) if value]


def config_for_project(config: SlackConfig, project: Any) -> SlackConfig:
    """Bind workspace credentials to one registry-controlled destination."""
    if project.slack_team_id != config.team_id:
        raise ValueError("project belongs to a different Slack workspace")
    return replace(config, channel_id=project.slack_channel_id)


def validate_alert_event(event: Dict[str, Any]) -> None:
    """Raise ValueError when an alert event is incomplete or forbidden."""
    kind = str(event.get("type") or "").strip().lower()
    if kind in FORBIDDEN_TYPES:
        raise ValueError(f"forbidden Slack event type: {kind}")
    if kind not in ALERT_TYPES:
        raise ValueError(f"alert type must be one of {sorted(ALERT_TYPES)}; got {kind!r}")
    if not event.get("agent"):
        raise ValueError("alert requires agent")
    if kind == "waiting-on":
        if not event.get("waiting_on_agent"):
            raise ValueError("waiting-on requires waiting_on_agent")
        if not (event.get("waiting_on_issue") or event.get("waiting_on_pr")):
            raise ValueError("waiting-on requires waiting_on_issue and/or waiting_on_pr")
    if kind == "hitl" and not str(event.get("text") or "").strip():
        raise ValueError("hitl requires decision text")


def _peer_ref(event: Dict[str, Any]) -> str:
    parts = []
    if event.get("waiting_on_issue"):
        parts.append(f"issue #{event['waiting_on_issue']}")
    if event.get("waiting_on_pr"):
        parts.append(f"PR #{event['waiting_on_pr']}")
    return " ".join(parts)


def format_event(event: Dict[str, Any], secrets: Optional[list[str]] = None) -> str:
    kind = event.get("type", "state")
    agent = event.get("agent", "unknown")
    family = event.get("family", "unknown")
    repo = event.get("repo", "")
    issue = event.get("issue")
    pr = event.get("pr")
    state = event.get("state", "")
    stamp = event.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = []
    if issue:
        ref.append(f"issue #{issue}")
    if pr:
        ref.append(f"PR #{pr}")
    ref_s = " ".join(ref) if ref else "no GitHub ref"
    body = redact(str(event.get("text") or ""), extra=secrets).strip()
    lines: list[str] = []
    operator = str(event.get("operator_user_id") or "").strip()
    if kind == "hitl" and operator.startswith("U"):
        lines.append(f"<@{operator}> HITL — decision needed")
    lines.append(f"[{kind}] agent=`{agent}` family=`{family}` {ref_s}")
    lines.append(
        f"project={event.get('project_id', '') or 'unrouted'} repo={repo} "
        f"state={state} ts={stamp}"
    )
    if kind == "waiting-on":
        peer = event.get("waiting_on_agent") or "unknown"
        lines.append(
            f"waiting on agent=`{peer}` holding {_peer_ref(event) or 'unknown ref'} "
            "(claim not stolen)"
        )
    if body:
        lines.append(body)
    return "\n".join(lines)


def format_github_alert_comment(event: Dict[str, Any], secrets: Optional[list[str]] = None) -> str:
    """Durable GitHub twin of a Slack alert (no Slack mention markup)."""
    kind = str(event.get("type") or "state")
    body = redact(str(event.get("text") or ""), extra=secrets).strip()
    lines = [
        f"## Factory alert (`{kind}`)",
        "",
        f"- agent: `{event.get('agent', 'unknown')}`",
        f"- family: `{event.get('family', 'unknown')}`",
        f"- project: `{event.get('project_id', '') or 'unrouted'}`",
    ]
    if event.get("issue"):
        lines.append(f"- issue: #{event['issue']}")
    if event.get("pr"):
        lines.append(f"- PR: #{event['pr']}")
    if kind == "waiting-on":
        lines.append(f"- waiting on agent: `{event.get('waiting_on_agent')}`")
        lines.append(f"- peer holds: {_peer_ref(event)}")
        lines.append("- action: wait; do not steal the claim")
    if kind == "hitl":
        lines.append("- HITL: agents stop for intervention; Slack does not replace the stop")
    if body:
        lines.extend(["", body])
    lines.append("")
    return "\n".join(lines)


class DedupeCache:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.ttl = ttl_seconds
        self._seen: Dict[str, float] = {}

    def seen(self, key: str, now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        expired = [item for item, exp in self._seen.items() if exp <= clock]
        for item in expired:
            del self._seen[item]
        if key in self._seen:
            return True
        self._seen[key] = clock + self.ttl
        return False


class FileDedupeCache(DedupeCache):
    """Process-restart durable dedupe for alert heartbeats."""

    def __init__(self, path: Path = DEDUPE_PATH, ttl_seconds: int = 3600) -> None:
        super().__init__(ttl_seconds=ttl_seconds)
        self.path = path
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return
        clock = time.time()
        for key, exp in entries.items():
            try:
                expiry = float(exp)
            except (TypeError, ValueError):
                continue
            if expiry > clock:
                self._seen[str(key)] = expiry

    def _save(self) -> None:
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"entries": self._seen}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self.path.chmod(0o600)

    def seen(self, key: str, now: Optional[float] = None) -> bool:
        result = super().seen(key, now=now)
        try:
            self._save()
        except OSError:
            pass
        return result


def dedupe_key(event: Dict[str, Any]) -> str:
    if event.get("dedupe_key"):
        return "|".join((str(event.get("project_id", "")), str(event["dedupe_key"])))
    parts = [
        str(event.get(name, ""))
        for name in ("project_id", "type", "agent", "issue", "pr", "text")
    ]
    if event.get("type") == "waiting-on":
        parts.extend(
            [
                str(event.get("waiting_on_agent", "")),
                str(event.get("waiting_on_issue", "")),
                str(event.get("waiting_on_pr", "")),
            ]
        )
    return "|".join(parts)


Transport = Callable[[SlackConfig, str, Optional[str]], Dict[str, Any]]


class RejectRedirectHandler(HTTPRedirectHandler):
    """Do not follow redirects; the bot token must not leave Slack."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError("slack_redirect_rejected")


def slack_api_transport(config: SlackConfig, text: str, thread_ts: Optional[str]) -> Dict[str, Any]:
    payload = {"channel": config.channel_id, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = urlencode(payload).encode("utf-8")
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {config.bot_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    opener = build_opener(RejectRedirectHandler)
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_event(
    config: SlackConfig,
    event: Dict[str, Any],
    transport: Transport = slack_api_transport,
    cache: Optional[DedupeCache] = None,
    thread_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Post one event. Never raises for Slack/network failures."""
    kind = str(event.get("type") or "").strip().lower()
    if kind in FORBIDDEN_TYPES:
        return {"ok": False, "error": "forbidden_event_type", "type": kind}
    cache = cache if cache is not None else DedupeCache()
    key = dedupe_key(event)
    if cache.seen(key):
        return {"ok": True, "deduped": True}
    stamped = dict(event)
    if kind == "hitl" and not stamped.get("operator_user_id") and config.operator_user_id:
        stamped["operator_user_id"] = config.operator_user_id
    text = format_event(stamped, secrets=secrets_from_config(config))
    try:
        result = transport(config, text, thread_ts)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "slack_unavailable", "detail": type(exc).__name__}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "slack_rejected")}
    return {"ok": True, "ts": result.get("ts")}


def default_github_comment(
    kind: str, number: int, body: str, repo_dir: str = "."
) -> bool:
    from common import run_cmd

    resource = "issue" if kind == "issue" else "pr"
    code, _, _ = run_cmd(
        ["gh", resource, "comment", str(number), "--body", body],
        check=False,
        cwd=repo_dir,
    )
    return code == 0


CommentFn = Callable[[str, int, str, str], bool]


def notify_alert(
    config: SlackConfig,
    event: Dict[str, Any],
    *,
    transport: Transport = slack_api_transport,
    cache: Optional[DedupeCache] = None,
    comment: CommentFn = default_github_comment,
    repo_dir: str = ".",
    skip_github: bool = False,
) -> Dict[str, Any]:
    """GitHub-first factory alert, then Slack. Never raises for Slack failures."""
    try:
        validate_alert_event(event)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_alert", "detail": str(exc)}

    secrets = secrets_from_config(config)
    stamped = dict(event)
    if stamped.get("type") == "hitl" and not stamped.get("operator_user_id"):
        stamped["operator_user_id"] = config.operator_user_id

    github_ok: Optional[bool] = None
    github_body = format_github_alert_comment(stamped, secrets=secrets)
    if not skip_github:
        target_kind = ""
        target_number = 0
        if stamped.get("issue"):
            target_kind, target_number = "issue", int(stamped["issue"])
        elif stamped.get("pr"):
            target_kind, target_number = "pr", int(stamped["pr"])
        if target_kind and target_number:
            try:
                github_ok = bool(comment(target_kind, target_number, github_body, repo_dir))
            except (OSError, TypeError, ValueError):
                github_ok = False

    slack = post_event(
        config,
        stamped,
        transport=transport,
        cache=cache if cache is not None else FileDedupeCache(),
    )
    return {
        "ok": bool(slack.get("ok")),
        "slack": slack,
        "github_ok": github_ok,
        "github_body": github_body,
        "deduped": bool(slack.get("deduped")),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Post one Aru factory Slack event.")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--event", required=True, dest="type")
    parser.add_argument("--repo", default="")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--state", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--waiting-on-agent", default="")
    parser.add_argument("--waiting-on-issue", type=int)
    parser.add_argument("--waiting-on-pr", type=int)
    parser.add_argument("--decision", default="", help="HITL decision text (alias for --text)")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--no-github-comment", action="store_true")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--registry-file", default="")
    parser.add_argument("--env-file", default=str(ENV_PATH))
    args = parser.parse_args(argv)
    try:
        from slack_projects import DEFAULT_REGISTRY_PATH, ProjectRegistry, RegistryError
    except ImportError as exc:
        print(f"[WARN] Slack notify skipped: {exc}", file=sys.stderr)
        return 0
    try:
        base = config_from_env(load_slack_env(Path(args.env_file)), require_channel=False)
        project = ProjectRegistry(
            Path(args.registry_file) if args.registry_file else DEFAULT_REGISTRY_PATH
        ).get(args.project_id)
        config = config_for_project(base, project)
    except (ValueError, RegistryError) as exc:
        print(f"[WARN] Slack notify skipped: {exc}", file=sys.stderr)
        return 0
    text = args.decision or args.text
    event: Dict[str, Any] = {
        "type": args.type,
        "agent": args.agent,
        "family": args.family,
        "repo": args.repo,
        "issue": args.issue,
        "pr": args.pr,
        "state": args.state,
        "text": text,
        "project_id": args.project_id,
    }
    if args.waiting_on_agent:
        event["waiting_on_agent"] = args.waiting_on_agent
    if args.waiting_on_issue:
        event["waiting_on_issue"] = args.waiting_on_issue
    if args.waiting_on_pr:
        event["waiting_on_pr"] = args.waiting_on_pr

    if str(args.type).lower() in ALERT_TYPES:
        result = notify_alert(
            config,
            event,
            repo_dir=args.repo_dir,
            skip_github=args.no_github_comment,
        )
        if result.get("error") == "invalid_alert":
            print(f"[WARN] Slack notify skipped: {result.get('detail')}", file=sys.stderr)
            return 0
        slack = result.get("slack") or {}
        if not result.get("ok"):
            print(f"[WARN] Slack notify failed: {slack.get('error')}", file=sys.stderr)
            return 0
        if result.get("deduped"):
            print("deduped")
        else:
            print("posted")
        return 0

    result = post_event(config, event)
    if not result.get("ok"):
        print(f"[WARN] Slack notify failed: {result.get('error')}", file=sys.stderr)
        return 0
    if result.get("deduped"):
        print("deduped")
    else:
        print("posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
