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
    lines = [
        f"[{kind}] agent=`{agent}` family=`{family}` {ref_s}",
        f"project={event.get('project_id', '') or 'unrouted'} repo={repo} state={state} ts={stamp}",
    ]
    if body:
        lines.append(body)
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


def dedupe_key(event: Dict[str, Any]) -> str:
    if event.get("dedupe_key"):
        return "|".join((str(event.get("project_id", "")), str(event["dedupe_key"])))
    return "|".join(
        str(event.get(name, ""))
        for name in ("project_id", "type", "agent", "issue", "pr", "text")
    )


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
    cache = cache if cache is not None else DedupeCache()
    key = dedupe_key(event)
    if cache.seen(key):
        return {"ok": True, "deduped": True}
    text = format_event(event, secrets=secrets_from_config(config))
    try:
        result = transport(config, text, thread_ts)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "slack_unavailable", "detail": type(exc).__name__}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "slack_rejected")}
    return {"ok": True, "ts": result.get("ts")}


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
    event = {
        "type": args.type,
        "agent": args.agent,
        "family": args.family,
        "repo": args.repo,
        "issue": args.issue,
        "pr": args.pr,
        "state": args.state,
        "text": args.text,
        "project_id": args.project_id,
    }
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
