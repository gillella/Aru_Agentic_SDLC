#!/usr/bin/env python3
# line-ceiling: 1045
"""Post stamped Slack events for the Aru factory control room.

GitHub remains the work queue. Slack downtime must not halt factory work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENV_PATH = Path.home() / ".aru" / "slack.env"
DEDUPE_PATH = Path.home() / ".aru" / "slack-notify-dedupe.json"
AUDIT_PATH = Path.home() / ".aru" / "slack-notify-audit.json"
SECRET_RE = re.compile(
    r"(xox[baprs]-[A-Za-z0-9-]{8,}|xapp-[A-Za-z0-9-]{8,}|Bearer\s+\S+"
    r"|Authorization\s*:\s*(?:Basic|Bearer)\s+\S+"
    r"|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"
    r"|[?&](?:X-Amz-Signature|sig|signature)=[^&\s]+"
    r"|-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----"
    r"|\b(?:password|passwd|api[_-]?key|client[_-]?secret|private[_-]?key"
    r"|access[_-]?key(?:[_-]?id)?|(?:aws[_-]?)?secret[_-]?access[_-]?key"
    r"|authorization|secret|token)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE | re.DOTALL,
)
SLACK_USER_RE = re.compile(r"^U[A-Z0-9]{8,}$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SLACK_MENTION_RE = re.compile(
    r"<(?:@[UW][A-Z0-9]+|![^>]+|#[A-Z0-9]+(?:\|[^>]*)?)>", re.IGNORECASE
)
FORBIDDEN_CONTENT_RE = re.compile(
    r"(?:\bheartbeat\b|\braw\s+diff\b|\bgit\s+diff\b|\bprompt\s*:"
    r"|\btest[-_\s]+logs?\s*:|\btest\s+output\s*:"
    r"|\bsystem\s+prompt\b|\buser\s+prompt\b|\bagent\s+prompt\b"
    r"|\byou\s+are\s+(?:an?\s+)?(?:ai|assistant|coding\s+agent)\b"
    r"|\btoken(?:s)?\s*(?:used|remaining|count|:|=)"
    r"|^(?:diff --git\s|---\s+[ab]/|\+\+\+\s+[ab]/|@@\s)"
    r"|^(?:FAILED\s+\S+|FAIL:\s+\S+|ERROR:\s+\S+|Traceback \(most recent call last\):)"
    r"|^\S+\.(?:py|js|jsx|ts|tsx|go|rs|rb|java|kt|swift|cs|c|cc|cpp|php)\S*"
    r"(?:\s+|::)(?:\S+(?:\s+|::))*?(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[[^\]]+\])?\s*$"
    r"|^\s*(?:=+\s*)?(?:(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed"
    r"|deselected|warnings?))(?:,\s*|\s+and\s+)?)+(?:\s+in\s+\d+(?:\.\d+)?s)?"
    r"(?:\s*=+)?\s*$"
    r"|^\s*Ran\s+\d+\s+tests?\s+in\s+\d+(?:\.\d+)?s\s*\r?\n\s*\r?\n?"
    r"(?:OK|FAILED(?:\s+\([^)]*\))?)\s*$"
    r"|^(?:Test Suites|Tests|Snapshots):\s+.+$|^Ran all test suites\.?\s*$"
    r"|^=+\s*test session starts\s*=+$|^collected\s+\d+\s+items?\b"
    r"|^no tests ran in\s+\d+(?:\.\d+)?s\s*$"
    r"|^\S+\s+\.{1,}\s+\[\s*\d+%\]\s*$"
    r"|^(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\d+\]\s+.+$"
    r"|^(?:PASS|FAIL)\s+\S+\.(?:test|spec)\.[A-Za-z0-9]+\s*$"
    r"|^TAP version\s+\d+\s*$|^1\.\.\d+\s*$|^(?:not\s+)?ok(?:\s+\d+)?(?:\s+-.*)?$"
    r"|^(?:ok|\?)\s+\S+(?:\s+\d+(?:\.\d+)?s)?\s*$|^---\s+(?:PASS|FAIL|SKIP):\s+.+$"
    r"|^test result:\s+(?:ok|FAILED)\b|^test\s+\S+\s+\.\.\.\s+(?:ok|FAILED|ignored)\s*$"
    r"|^\d+\s+runs?,\s+\d+\s+assertions?,\s+\d+\s+failures?,\s+\d+\s+errors?\b"
    r"|^Test Suite ['\"].+['\"] (?:started|passed|failed)\b"
    r"|^Executed\s+\d+\s+tests?,\s+with\s+\d+\s+failures?\b"
    r"|^\s*\d+\s+(?:passed|failed|skipped|passing|failing)\s+\(\d+(?:\.\d+)?[sm]\)\s*$"
    r"|^\[INFO\]\s+Tests run:|^Tests run:\s+\d+,\s+Failures:"
    r"|^Test Run (?:Successful|Failed)\b|^Total tests:\s+\d+\b"
    r"|^(?:PASS|FAIL|OK)\s*$|^OK\s+\(\d+\s+tests?,\s+\d+\s+assertions?\)\s*$"
    r"|^FAILURES!\s*(?:Tests:.*)?$|^\d+\s+examples?,\s+\d+\s+failures?\b"
    r"|^(?:Test Files|Tests)\s+\d+\s+(?:passed|failed|skipped)\s+\(\d+\)\s*$"
    r"|^\d{2}:\d{2}\s+\+\d+(?:\s+-\d+)?:\s+All tests passed!\s*$"
    r"|^Passed!\s+-\s+Failed:\s+\d+,\s+Passed:\s+\d+\b"
    r"|^\S+\s+>\s+\S+\s+(?:PASSED|FAILED|SKIPPED)\s*$"
    r"|^running\s+\d+\s+tests?\s*$|^All tests passed!\s*$"
    r"|^\s*(?:[✓✔✗✘]|\[PASS\]|\[FAIL\])\s+\S+"
    r"|^(?:ERROR\s+collecting\s+\S+|E\s{2,}.+)"
    r"|\b(?:act as|your task is|follow (?:these|the) instructions)\b"
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b"
    r"|^\s*\[(?:system(?:\s+message)?|developer|user|assistant|human|inst)\]\s*"
    r"|[\"']role[\"']\s*:\s*[\"'](?:system|developer|user|assistant|human|model|tool|function)[\"']"
    r"|(?:^|[{,]\s*)role\s*:\s*(?:system|developer|user|assistant|human|model|tool|function)\s*(?:[,}]|$)"
    r"|^\s*-\s*role\s*:\s*(?:system|developer|user|assistant|human|model|tool|function)\s*$"
    r"|^\s*role\s*=\s*(?:system|developer|user|assistant|human|model|tool|function)\s*$"
    r"|[\"']system[\"']\s*:\s*[\"']|^\s*system\s*=\s*\S+"
    r"|<\|(?:system|developer|user|assistant|human|model)\|>|<\|im_(?:start|end)\|>"
    r"|^\s*<\|im_start\|>\s*(?:system|developer|user|assistant|human|model)\b"
    r"|^\s*(?:<<\/?SYS>>|\[/?INST\])"
    r"|^\s*#{1,6}\s*(?:system|developer|user|assistant|human|model|instructions)\s*:?[ \t]*$"
    r"|^\s*(?:system|developer|user|assistant|human|model|instructions)\s*:"
    r"|^\s*```\s*(?:system|developer|user|assistant|human|model|prompt)\b"
    r"|<\/?(?:system|developer|user|assistant|human|model|instructions)>"
    r"|={3,}\s*(?:FAILURES|ERRORS)\s*={3,})",
    re.IGNORECASE | re.MULTILINE,
)
ENV_KEYS = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_TEAM_ID",
    "SLACK_CHANNEL_ID",
    "SLACK_OPERATOR_USER_ID",
    "SLACK_ESCALATION_USER_ID",
    "SLACK_CHANNEL_NAME",
)
ALERT_TYPES = frozenset({"blocked", "waiting-on", "hitl"})
# Alert kinds that must wake the Hermes war-room bot via @-mention so
# material decisions escalate to Telegram. `waiting-on` is routine
# peer-claim chatter and deliberately stays mention-free.
ESCALATION_ALERT_TYPES = frozenset({"blocked", "hitl"})
AVAILABILITY_EVENT = "availability"
AVAILABILITY_STATES = frozenset({"cooling-down", "returned"})
COOLDOWN_REASONS = frozenset({
    "credit-exhausted",
    "rate-limited",
    "provider-outage",
    "child-crash",
})
MAX_ALERT_TEXT_CHARS = 1000
MAX_ALERT_TEXT_LINES = 12
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
    escalation_user_id: str = ""
    app_token: str = ""
    signing_secret: str = ""
    channel_name: str = "project-aru-code"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        escalation_user_id=values.get("SLACK_ESCALATION_USER_ID", ""),
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


def sanitize_event(
    event: Dict[str, Any], secrets: Optional[list[str]] = None
) -> Dict[str, Any]:
    """Redact every string-valued field before it reaches either transport."""
    def clean(value: Any) -> Any:
        if isinstance(value, str):
            return SLACK_MENTION_RE.sub(
                "[mention removed]", redact(value, extra=secrets)
            )
        if isinstance(value, dict):
            return {redact(str(key), extra=secrets): clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [clean(item) for item in value]
        return value

    return {redact(str(key), extra=secrets): clean(value) for key, value in event.items()}


def _forbidden_content_field(event: Dict[str, Any]) -> str:
    text = event.get("text")
    return "text" if isinstance(text, str) and FORBIDDEN_CONTENT_RE.search(text) else ""


def read_alert_payload_file(path: Path) -> str:
    """Read one local, operator-owned private file without following symlinks."""
    # O_NONBLOCK prevents a FIFO/device path from hanging before fstat can
    # prove that the opened descriptor is a regular file.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("alert payload path is not a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError("alert payload file is not owned by the current operator")
        if metadata.st_mode & 0o077:
            raise ValueError("alert payload file permissions must be 0600 or stricter")
        return handle.read().rstrip("\r\n")


def config_for_project(config: SlackConfig, project: Any) -> SlackConfig:
    """Bind workspace credentials to one registry-controlled destination."""
    if project.slack_team_id != config.team_id:
        raise ValueError("project belongs to a different Slack workspace")
    return replace(config, channel_id=project.slack_channel_id)


def validate_alert_event(event: Dict[str, Any]) -> None:  # noqa: C901, PLR0912
    """Raise ValueError when an alert event is incomplete or forbidden."""
    kind = str(event.get("type") or "").strip().lower()
    if kind in FORBIDDEN_TYPES:
        raise ValueError(f"forbidden Slack event type: {kind}")
    if kind not in ALERT_TYPES:
        raise ValueError(f"alert type must be one of {sorted(ALERT_TYPES)}; got {kind!r}")
    for field in (
        "agent",
        "family",
        "repo",
        "state",
        "text",
        "project_id",
        "waiting_on_agent",
        "operator_user_id",
    ):
        if field in event and event[field] is not None and not isinstance(event[field], str):
            raise ValueError(f"alert field {field} must be a string")
    for field in ("issue", "pr", "waiting_on_issue", "waiting_on_pr"):
        value = event.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f"alert field {field} must be a positive integer")
    if not str(event.get("agent") or "").strip():
        raise ValueError("alert requires agent")
    repo = str(event.get("repo") or "").strip()
    if not REPO_SLUG_RE.fullmatch(repo):
        raise ValueError("alert requires authoritative repo as owner/name")
    if kind == "waiting-on":
        if not event.get("waiting_on_agent"):
            raise ValueError("waiting-on requires waiting_on_agent")
        if not (event.get("waiting_on_issue") or event.get("waiting_on_pr")):
            raise ValueError("waiting-on requires waiting_on_issue and/or waiting_on_pr")
    if kind == "hitl" and not str(event.get("text") or "").strip():
        raise ValueError("hitl requires decision text")
    text = str(event.get("text") or "")
    char_count = len(text)
    line_count = len(text.splitlines())
    if char_count > MAX_ALERT_TEXT_CHARS or line_count > MAX_ALERT_TEXT_LINES:
        raise ValueError(
            f"alert summary exceeds size limit ({char_count}/{MAX_ALERT_TEXT_CHARS} characters, "
            f"{line_count}/{MAX_ALERT_TEXT_LINES} lines); nothing delivered to Slack or GitHub"
        )
    forbidden_field = _forbidden_content_field(event)
    if forbidden_field:
        raise ValueError(f"forbidden alert content in {forbidden_field}")
    event["type"] = kind


def validate_availability_event(event: Dict[str, Any]) -> None:  # noqa: C901, PLR0912
    """Validate one concise project-channel availability transition."""
    kind = str(event.get("type") or "").strip().lower()
    if kind != AVAILABILITY_EVENT:
        raise ValueError(f"availability event type must be {AVAILABILITY_EVENT!r}")
    for field in (
        "agent", "family", "repo", "state", "text", "project_id",
        "cooldown_reason", "retry_at", "dedupe_key",
    ):
        if field in event and event[field] is not None and not isinstance(event[field], str):
            raise ValueError(f"availability field {field} must be a string")
    for field in ("issue", "pr"):
        value = event.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"availability field {field} must be a positive integer")
    if not str(event.get("agent") or "").strip():
        raise ValueError("availability transition requires agent")
    repo = str(event.get("repo") or "").strip()
    if not REPO_SLUG_RE.fullmatch(repo):
        raise ValueError("availability transition requires authoritative repo as owner/name")
    state = str(event.get("state") or "").strip().lower()
    if state not in AVAILABILITY_STATES:
        raise ValueError(f"availability state must be one of {sorted(AVAILABILITY_STATES)}")
    reason = str(event.get("cooldown_reason") or "").strip().lower()
    if state == "cooling-down" and reason not in COOLDOWN_REASONS:
        raise ValueError("cooling-down transition requires a valid cooldown_reason")
    if state == "returned" and reason:
        raise ValueError("returned transition must not carry cooldown_reason")
    retry_at = str(event.get("retry_at") or "").strip()
    if retry_at:
        try:
            parsed_retry = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("retry_at must be an ISO-8601 timestamp") from exc
        if parsed_retry.tzinfo is None:
            raise ValueError("retry_at must include a timezone")
    text = str(event.get("text") or "")
    char_count = len(text)
    line_count = len(text.splitlines())
    if char_count > MAX_ALERT_TEXT_CHARS or line_count > MAX_ALERT_TEXT_LINES:
        raise ValueError(
            f"availability summary exceeds size limit ({char_count}/{MAX_ALERT_TEXT_CHARS} characters, "
            f"{line_count}/{MAX_ALERT_TEXT_LINES} lines); nothing delivered to Slack or GitHub"
        )
    forbidden_field = _forbidden_content_field(event)
    if forbidden_field:
        raise ValueError(f"forbidden availability content in {forbidden_field}")
    event["type"] = kind
    event["state"] = state
    if reason:
        event["cooldown_reason"] = reason


def _peer_ref(event: Dict[str, Any]) -> str:
    parts = []
    if event.get("waiting_on_issue"):
        parts.append(f"issue #{event['waiting_on_issue']}")
    if event.get("waiting_on_pr"):
        parts.append(f"PR #{event['waiting_on_pr']}")
    return " ".join(parts)


def format_event(event: Dict[str, Any], secrets: Optional[list[str]] = None) -> str:
    event = sanitize_event(event, secrets)
    kind = event.get("type", "state")
    agent = event.get("agent", "unknown")
    family = event.get("family", "unknown")
    repo = event.get("repo", "")
    issue = event.get("issue")
    pr = event.get("pr")
    state = event.get("state", "")
    stamp = event.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = []
    repo_url = f"https://github.com/{repo}" if REPO_SLUG_RE.fullmatch(str(repo)) else ""
    if issue:
        suffix = f" ({repo_url}/issues/{issue})" if repo_url else ""
        ref.append(f"issue #{issue}{suffix}")
    if pr:
        suffix = f" ({repo_url}/pull/{pr})" if repo_url else ""
        ref.append(f"PR #{pr}{suffix}")
    ref_s = " ".join(ref) if ref else "no GitHub ref"
    body = redact(str(event.get("text") or ""), extra=secrets).strip()
    lines: list[str] = []
    operator = str(event.get("operator_user_id") or "").strip()
    escalation = str(event.get("escalation_user_id") or "").strip()
    if kind in ESCALATION_ALERT_TYPES and escalation.startswith("U"):
        lines.append(f"<@{escalation}> {kind.upper()} — escalate to Hermes")
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
    if kind == AVAILABILITY_EVENT:
        transition = f"availability={state}"
        reason = str(event.get("cooldown_reason") or "").strip()
        retry_at = str(event.get("retry_at") or "").strip()
        if reason:
            transition += f" reason={reason}"
        if retry_at:
            transition += f" retry_at={retry_at}"
        lines.append(transition)
    if body:
        lines.append(body)
    return "\n".join(lines)


def format_github_alert_comment(event: Dict[str, Any], secrets: Optional[list[str]] = None) -> str:
    """Durable GitHub twin of a Slack alert (no Slack mention markup)."""
    event = sanitize_event(event, secrets)
    kind = str(event.get("type") or "state")
    body = redact(str(event.get("text") or ""), extra=secrets).strip()
    lines = [
        f"## Factory alert (`{kind}`)",
        "",
        f"- agent: `{event.get('agent', 'unknown')}`",
        f"- family: `{event.get('family', 'unknown')}`",
        f"- project: `{event.get('project_id', '') or 'unrouted'}`",
    ]
    repo = str(event.get("repo") or "")
    repo_url = f"https://github.com/{repo}" if REPO_SLUG_RE.fullmatch(repo) else ""
    if repo_url:
        lines.append(f"- repository: [{repo}]({repo_url})")
    if event.get("issue"):
        issue = event["issue"]
        lines.append(f"- issue: [#{issue}]({repo_url}/issues/{issue})" if repo_url else f"- issue: #{issue}")
    if event.get("pr"):
        pr = event["pr"]
        lines.append(f"- PR: [#{pr}]({repo_url}/pull/{pr})" if repo_url else f"- PR: #{pr}")
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
    """In-memory dedupe. Peek does not record; remember persists after success."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.ttl = ttl_seconds
        self._seen: Dict[str, float] = {}

    def _purge(self, clock: float) -> None:
        expired = [item for item, exp in self._seen.items() if exp <= clock]
        for item in expired:
            del self._seen[item]

    def contains(self, key: str, now: Optional[float] = None) -> bool:
        clock = time.time() if now is None else now
        self._purge(clock)
        return key in self._seen

    def remember(self, key: str, now: Optional[float] = None) -> None:
        clock = time.time() if now is None else now
        self._purge(clock)
        self._seen[key] = clock + self.ttl

    def seen(self, key: str, now: Optional[float] = None) -> bool:
        """Backward-compatible: True if already present; otherwise record and return False."""
        if self.contains(key, now=now):
            return True
        self.remember(key, now=now)
        return False


class FileDedupeCache(DedupeCache):
    """Lock-protected, restart-durable dedupe for factory alerts."""

    def __init__(self, path: Path = DEDUPE_PATH, ttl_seconds: int = 3600) -> None:
        super().__init__(ttl_seconds=ttl_seconds)
        self.path = path
        self._load()

    def _load(self) -> None:
        try:
            from slack_projects import RegistryError, read_secure_json
        except ImportError:
            if not self.path.is_file():
                return
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            self._ingest(payload)
            return
        try:
            payload = read_secure_json(self.path, {"entries": {}})
        except RegistryError:
            return
        self._ingest(payload)

    def _ingest(self, payload: Any) -> None:
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

    def _persist(self) -> None:
        snapshot = dict(self._seen)

        def update(payload: Any) -> Dict[str, Any]:
            entries = payload.get("entries") if isinstance(payload, dict) else {}
            if not isinstance(entries, dict):
                entries = {}
            clock = time.time()
            merged = {
                str(key): float(exp)
                for key, exp in entries.items()
                if _safe_float(exp) > clock
            }
            merged.update(snapshot)
            return {"entries": merged}

        try:
            from slack_projects import RegistryError, mutate_secure_json
        except ImportError:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {"entries": {}}
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(update(current), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.chmod(0o600)
            os.replace(tmp, self.path)
            self.path.chmod(0o600)
            return
        try:
            mutate_secure_json(self.path, {"entries": {}}, update)
        except RegistryError:
            pass

    def contains(self, key: str, now: Optional[float] = None) -> bool:
        self._load()
        return super().contains(key, now=now)

    def remember(self, key: str, now: Optional[float] = None) -> None:
        super().remember(key, now=now)
        try:
            self._persist()
        except OSError:
            pass

    def seen(self, key: str, now: Optional[float] = None) -> bool:
        if self.contains(key, now=now):
            return True
        self.remember(key, now=now)
        return False


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _raw_dedupe_material(event: Dict[str, Any]) -> str:
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


def dedupe_key(event: Dict[str, Any]) -> str:
    """Stable digest so raw alert text (and secrets) never hit disk."""
    material = _raw_dedupe_material(event)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    project = str(event.get("project_id", "") or "unrouted")
    return f"{project}|sha256:{digest}"


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


def _stamp_authoritative_mentions(config: SlackConfig, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Stamp operator + escalation user ids from config; reject invalid values.

    The configured ids are authoritative; caller-supplied mention identities
    are never trusted. Empty escalation config means no escalation mention
    (legacy behavior). Returns an error dict on the first invalid identity,
    else None.
    """
    kind = event.get("type")
    if kind == "hitl":
        operator = str(config.operator_user_id or "").strip()
        if not SLACK_USER_RE.fullmatch(operator):
            return {"ok": False, "error": "invalid_operator_user_id"}
        event["operator_user_id"] = operator
    if kind in ESCALATION_ALERT_TYPES:
        escalation = str(config.escalation_user_id or "").strip()
        if escalation and not SLACK_USER_RE.fullmatch(escalation):
            return {"ok": False, "error": "invalid_escalation_user_id"}
        event["escalation_user_id"] = escalation
    return None


def post_event(
    config: SlackConfig,
    event: Dict[str, Any],
    transport: Transport = slack_api_transport,
    cache: Optional[DedupeCache] = None,
    thread_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Post one event. Never raises for Slack/network failures.

    Dedupe keys are recorded only after a successful Slack delivery so a
    transient outage can retry once Slack recovers.
    """
    kind = str(event.get("type") or "").strip().lower()
    if kind in FORBIDDEN_TYPES:
        return {"ok": False, "error": "forbidden_event_type", "type": kind}
    stamped = dict(event)
    stamped["type"] = kind
    if kind in ALERT_TYPES:
        try:
            validate_alert_event(stamped)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_alert", "detail": str(exc)}
    elif kind == AVAILABILITY_EVENT:
        try:
            validate_availability_event(stamped)
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_availability",
                "detail": str(exc),
            }
    mention_error = _stamp_authoritative_mentions(config, stamped)
    if mention_error:
        return mention_error
    stamped = sanitize_event(stamped, secrets_from_config(config))
    cache = cache if cache is not None else DedupeCache()
    key = dedupe_key(stamped)
    if cache.contains(key):
        return {"ok": True, "deduped": True}
    text = format_event(stamped, secrets=secrets_from_config(config))
    try:
        result = transport(config, text, thread_ts)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "slack_unavailable", "detail": type(exc).__name__}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "slack_rejected")}
    cache.remember(key)
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


def alert_github_targets(event: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Prefer PR when present; comment on both when both issue and PR are set."""
    targets: List[Tuple[str, int]] = []
    if event.get("pr"):
        targets.append(("pr", int(event["pr"])))
    if event.get("issue"):
        targets.append(("issue", int(event["issue"])))
    return targets


def record_delivery_audit(
    path: Path,
    event: Dict[str, Any],
    key: str,
    slack: Dict[str, Any],
    retry_state: str,
) -> bool:
    """Append a secret-safe, lock-protected Slack delivery audit record."""
    safe = sanitize_event(event)
    record = {
        "timestamp": _now(),
        "project_id": safe.get("project_id") or "unrouted",
        "event_type": safe.get("type") or "unknown",
        "agent": safe.get("agent") or "unknown",
        "issue": safe.get("issue"),
        "pr": safe.get("pr"),
        "dedupe_id": key,
        "outcome": "delivered" if slack.get("ok") else "slack_failed",
        "error_class": redact(str(slack.get("detail") or slack.get("error") or "")),
        "retry_state": retry_state,
    }

    def update(payload: Any) -> Dict[str, Any]:
        events = payload.get("events") if isinstance(payload, dict) else []
        if not isinstance(events, list):
            events = []
        return {"events": [*events[-999:], record]}

    try:
        from slack_projects import mutate_secure_json

        mutate_secure_json(path, {"events": []}, update)
        return True
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def delivery_retry_pending(path: Path, key: str) -> bool:
    """Return whether the last recorded delivery for this key needs retry."""
    try:
        from slack_projects import read_secure_json

        payload = read_secure_json(path, {"events": []})
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
    events = payload.get("events") if isinstance(payload, dict) else []
    if not isinstance(events, list):
        return False
    matches = [item for item in events if isinstance(item, dict) and item.get("dedupe_id") == key]
    return bool(matches and matches[-1].get("retry_state") == "pending_retry")


def notify_alert(  # noqa: C901, PLR0912
    config: SlackConfig,
    event: Dict[str, Any],
    *,
    transport: Transport = slack_api_transport,
    cache: Optional[DedupeCache] = None,
    comment: CommentFn = default_github_comment,
    repo_dir: str = ".",
    skip_github: bool = False,
    audit_path: Path = AUDIT_PATH,
) -> Dict[str, Any]:
    """GitHub-first factory alert, then Slack. Never raises for Slack failures."""
    if skip_github:
        return {
            "ok": False,
            "error": "invalid_alert",
            "detail": "alert delivery requires a durable GitHub comment",
        }
    try:
        validate_alert_event(event)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_alert", "detail": str(exc)}

    secrets = secrets_from_config(config)
    stamped = dict(event)
    mention_error = _stamp_authoritative_mentions(config, stamped)
    if mention_error:
        return {
            "ok": False,
            "error": "invalid_alert",
            "detail": mention_error.get("error", "invalid mention identity"),
        }
    stamped = sanitize_event(stamped, secrets)

    alert_cache = cache if cache is not None else FileDedupeCache()
    key = dedupe_key(stamped)
    github_body = format_github_alert_comment(stamped, secrets=secrets)
    targets = alert_github_targets(stamped)
    if not targets:
        return {
            "ok": False,
            "error": "github_comment_failed",
            "slack": {"ok": False, "error": "github_comment_failed"},
            "github_ok": False,
            "github_body": github_body,
            "deduped": False,
        }

    github_ok = True
    github_was_new = False
    for target_kind, target_number in targets:
        target_key = f"github:{target_kind}:{target_number}:{key}"
        if alert_cache.contains(target_key):
            continue
        try:
            delivered = bool(comment(target_kind, target_number, github_body, repo_dir))
        except (OSError, TypeError, ValueError):
            delivered = False
        if delivered:
            alert_cache.remember(target_key)
            github_was_new = True
        else:
            github_ok = False

    # Slack is discussion only. If any required durable target failed, leave
    # Slack unsent and every failed GitHub target retryable.
    if not github_ok:
        return {
            "ok": False,
            "error": "github_comment_failed",
            "slack": {"ok": False, "error": "github_comment_failed"},
            "github_ok": False,
            "github_body": github_body,
            "deduped": False,
        }

    retry_was_pending = delivery_retry_pending(audit_path, key)
    slack = post_event(
        config,
        stamped,
        transport=transport,
        cache=alert_cache,
    )
    audit_ok: Optional[bool] = None
    if not slack.get("ok"):
        audit_ok = record_delivery_audit(
            audit_path,
            stamped,
            key,
            slack,
            "pending_retry",
        )
    elif retry_was_pending and not slack.get("deduped"):
        audit_ok = record_delivery_audit(
            audit_path,
            stamped,
            key,
            slack,
            "complete",
        )
    return {
        "ok": bool(slack.get("ok")),
        "slack": slack,
        "github_ok": True,
        "github_body": github_body,
        "deduped": bool(slack.get("deduped")) and not github_was_new,
        "audit_ok": audit_ok,
    }


def main(argv: Optional[list[str]] = None) -> int:  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Post one Aru factory Slack event.")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--event", required=True, dest="type")
    parser.add_argument("--repo", default="")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--state", default="")
    payload = parser.add_mutually_exclusive_group()
    payload.add_argument("--text", default="")
    payload.add_argument("--text-file", default="")
    parser.add_argument("--waiting-on-agent", default="")
    parser.add_argument("--waiting-on-issue", type=int)
    parser.add_argument("--waiting-on-pr", type=int)
    parser.add_argument("--cooldown-reason", default="")
    parser.add_argument("--retry-at", default="")
    parser.add_argument("--dedupe-key", default="")
    payload.add_argument("--decision", default="", help="HITL decision text (alias for --text)")
    payload.add_argument("--decision-file", default="", help="read HITL decision text from a file")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--no-github-comment", action="store_true")
    parser.add_argument("--project-id", default="")
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
        registry = ProjectRegistry(
            Path(args.registry_file) if args.registry_file else DEFAULT_REGISTRY_PATH
        )
        if args.project_id:
            # An explicit --project-id always wins.
            project = registry.get(args.project_id)
        else:
            # Otherwise resolve the binding from the checkout the agent is
            # working in, so a bound repo needs no per-agent Slack setup.
            project = registry.find_by_checkout(Path(args.repo_dir))
        config = config_for_project(base, project)
    except (ValueError, RegistryError) as exc:
        print(f"[WARN] Slack notify skipped: {exc}", file=sys.stderr)
        return 0
    text = args.decision or args.text
    payload_file = args.decision_file or args.text_file
    if payload_file:
        try:
            text = read_alert_payload_file(Path(payload_file))
        except (OSError, UnicodeError, ValueError) as exc:
            print(
                f"[WARN] Slack notify skipped: cannot read alert payload file: {exc}",
                file=sys.stderr,
            )
            return 2
    event: Dict[str, Any] = {
        "type": args.type,
        "agent": args.agent,
        "family": args.family,
        # Project registry identity is authoritative. Never let a caller
        # redirect alert links by overriding the repository slug.
        "repo": project.repo_slug,
        "issue": args.issue,
        "pr": args.pr,
        "state": args.state,
        "text": text,
        "project_id": project.project_id,
    }
    if args.waiting_on_agent:
        event["waiting_on_agent"] = args.waiting_on_agent
    if args.waiting_on_issue:
        event["waiting_on_issue"] = args.waiting_on_issue
    if args.waiting_on_pr:
        event["waiting_on_pr"] = args.waiting_on_pr
    if args.cooldown_reason:
        event["cooldown_reason"] = args.cooldown_reason
    if args.retry_at:
        event["retry_at"] = args.retry_at
    if args.dedupe_key:
        event["dedupe_key"] = args.dedupe_key

    if str(args.type).lower() in ALERT_TYPES:
        result = notify_alert(
            config,
            event,
            repo_dir=project.local_path,
            skip_github=args.no_github_comment,
        )
        if result.get("error") == "invalid_alert":
            print(f"[WARN] Slack notify skipped: {result.get('detail')}", file=sys.stderr)
            return 2
        slack = result.get("slack") or {}
        if result.get("github_ok") is False:
            print("[WARN] Slack alert: durable GitHub comment failed", file=sys.stderr)
            return 1
        if not result.get("ok"):
            print(f"[WARN] Slack notify failed: {slack.get('error')}", file=sys.stderr)
            if result.get("audit_ok") is False:
                print("[WARN] Slack failure audit could not be persisted", file=sys.stderr)
                return 1
            return 0
        if result.get("deduped"):
            print("deduped")
        else:
            print("posted")
        return 0

    result = post_event(config, event, cache=FileDedupeCache())
    if not result.get("ok"):
        error_msg = result.get("detail") or result.get("error")
        print(f"[WARN] Slack notify failed: {error_msg}", file=sys.stderr)
        if result.get("error") in (
            "invalid_availability",
            "invalid_alert",
            "forbidden_event_type",
            "invalid_operator_user_id",
            "invalid_escalation_user_id",
        ):
            return 2
        return 0
    if result.get("deduped"):
        print("deduped")
    else:
        print("posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
