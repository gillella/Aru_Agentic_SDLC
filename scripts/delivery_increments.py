#!/usr/bin/env python3
# line-ceiling: 730
"""Operator-authorized Delivery Increment records and transition rules."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 1
DEFAULT_INCREMENT_PATH = Path.home() / ".aru" / "delivery-increments.json"
INCREMENT_ID_RE = re.compile(r"^inc_[0-9a-f]{20}$")
PROJECT_ID_RE = re.compile(r"^proj_[A-Za-z0-9_-]{3,64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SLACK_USER_RE = re.compile(r"^U[A-Z0-9]{7,}$")
SLACK_TEAM_RE = re.compile(r"^T[A-Z0-9]{7,}$")
SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{7,}$")
ACTIVE_NORMAL_STATES = {"authorized", "active"}
LIFECYCLE_STATES = {"authorized", "active", "accepted", "closed"}
RELEASE_STATES = {"unreleased", "deployment-authorized", "deployed"}
ACTIONS = {
    "authorize", "revise", "start", "accept",
    "authorize-deployment", "deployed", "cancel",
}


class IncrementError(RuntimeError):
    """The requested increment decision is invalid or unsafe."""


def _private_directory(path: Path) -> None:  # noqa: C901, PLR0912
    if path.is_symlink():
        raise IncrementError(f"unsafe directory: {path}")
    if not path.exists():
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            # Concurrent creation race: another process created the directory.
            pass
        except OSError as exc:
            raise IncrementError(f"cannot create directory {path}: {exc}") from exc

    if path.is_symlink() or not path.is_dir():
        raise IncrementError(f"unsafe directory: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise IncrementError(f"cannot stat directory {path}: {exc}") from exc
    if info.st_uid != os.getuid():
        raise IncrementError(f"directory is not owned by the current user: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise IncrementError(f"directory is writable by another user: {path}")
    if mode != 0o700:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise IncrementError(f"directory changed while securing it: {path}")
            if stat.S_IMODE(opened.st_mode) & 0o022:
                raise IncrementError(f"directory is writable by another user: {path}")
            os.fchmod(descriptor, 0o700)
        except OSError as exc:
            raise IncrementError(f"cannot secure directory {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise IncrementError(f"refusing symlink: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise IncrementError(f"cannot stat file {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise IncrementError(f"not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise IncrementError(f"file is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise IncrementError(f"file must be private (0600): {path}")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    _private_directory(path.parent)
    lock_path = path.with_name(f"{path.name}.lock")
    if lock_path.exists():
        _private_file(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise IncrementError(f"cannot open lock file {lock_path}: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_unlocked(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise IncrementError(f"refusing symlink: {path}")
    if path.exists():
        _private_file(path)
    temp_name: Optional[str] = None
    try:
        encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        os.chmod(path, 0o600)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise IncrementError(f"cannot write {path}: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _unique_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IncrementError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(constant: str) -> Any:
    raise IncrementError(f"invalid JSON constant: {constant}")


def _strict_json_loads(value: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise IncrementError(f"invalid Delivery Increment JSON: {exc}") from exc


def _read_increment_unlocked(path: Path, default: Any) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        if default is not None:
            return deepcopy(default)
        raise IncrementError(f"file does not exist: {path}") from exc
    except OSError as exc:
        raise IncrementError(f"cannot open {path}: {exc}") from exc

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise IncrementError(f"not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise IncrementError(f"file is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise IncrementError(f"file must be private (0600): {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return _strict_json_loads(handle.read())
    except IncrementError:
        raise
    except (OSError, UnicodeError) as exc:
        raise IncrementError(f"cannot read {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_increment_json(path: Path, default: Any) -> Any:
    with _file_lock(path):
        return _read_increment_unlocked(path, default)


def _mutate_increment_json(path: Path, default: Any, updater: Any) -> Any:
    with _file_lock(path):
        current = _read_increment_unlocked(path, default)
        updated = updater(deepcopy(current))
        _write_unlocked(path, updated)
        return updated


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IncrementError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IncrementError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise IncrementError(f"{name} must include a timezone")
    return value


def _timestamp_value(value: Any, name: str) -> datetime:
    _timestamp(value, name)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _string(value: Any, name: str, pattern: Optional[re.Pattern[str]] = None) -> str:
    if not isinstance(value, str) or not value:
        raise IncrementError(f"invalid {name}")
    if pattern is not None and not pattern.fullmatch(value):
        raise IncrementError(f"invalid {name}")
    return value


def _issues(value: Any) -> List[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value)
        or len(set(value)) != len(value)
    ):
        raise IncrementError("issue_scope must contain unique positive issue numbers")
    return sorted(value)


def increment_id_for_event(project_id: str, event_id: str) -> str:
    if (
        not isinstance(project_id, str)
        or not PROJECT_ID_RE.fullmatch(project_id)
        or not isinstance(event_id, str)
        or not event_id
        or "|" in event_id
    ):
        raise IncrementError("project_id and Slack event id are required")
    digest = hashlib.sha256(f"{project_id}\0{event_id}".encode()).hexdigest()[:20]
    return f"inc_{digest}"


def operator_evidence(
    *, user_id: str, team_id: str, channel_id: str, event_id: str,
    github_repository: str, github_record_url: str, recorded_at: str,
) -> Dict[str, Any]:
    evidence = {
        "source": "slack_control_room",
        "authenticated": True,
        "slack_user_id": user_id,
        "slack_team_id": team_id,
        "slack_channel_id": channel_id,
        "slack_event_id": event_id,
        "github_repository": github_repository,
        "github_record_url": github_record_url,
        "recorded_at": recorded_at,
    }
    _validate_evidence(evidence)
    return evidence


def _validate_evidence(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise IncrementError("operator evidence is required")
    required = {
        "source", "authenticated", "slack_user_id", "slack_team_id",
        "slack_channel_id", "slack_event_id", "github_repository",
        "github_record_url", "recorded_at",
    }
    if set(value) != required:
        raise IncrementError("operator evidence has an invalid schema")
    if value["source"] != "slack_control_room" or value["authenticated"] is not True:
        raise IncrementError("only an authenticated Slack operator decision is authoritative")
    for key in ("slack_user_id", "slack_team_id", "slack_channel_id", "slack_event_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise IncrementError(f"invalid {key}")
    if "|" in value["slack_event_id"]:
        raise IncrementError("invalid slack_event_id")
    if (
        not SLACK_USER_RE.fullmatch(value["slack_user_id"])
        or not SLACK_TEAM_RE.fullmatch(value["slack_team_id"])
    ):
        raise IncrementError("invalid Slack operator identity")
    if not SLACK_CHANNEL_RE.fullmatch(value["slack_channel_id"]):
        raise IncrementError("invalid Slack channel identity")
    repository = _string(value["github_repository"], "github_repository", REPOSITORY_RE)
    url = value["github_record_url"]
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repository}/"):
        raise IncrementError("a durable GitHub decision URL is required")
    _timestamp(value["recorded_at"], "recorded_at")
    return deepcopy(value)


def _validate_decision(value: Any) -> Dict[str, Any]:  # noqa: C901, PLR0912
    if not isinstance(value, dict):
        raise IncrementError("decision must be an object")
    required = {
        "decision_id", "action", "increment_id", "project_id",
        "operator_user_id", "github_repository",
    }
    if not required.issubset(value):
        raise IncrementError("decision identity is incomplete")
    _string(value["decision_id"], "decision_id")
    if not isinstance(value["action"], str) or value["action"] not in ACTIONS:
        raise IncrementError(f"unsupported decision action: {value['action']}")
    _string(value["increment_id"], "increment_id", INCREMENT_ID_RE)
    _string(value["project_id"], "project_id", PROJECT_ID_RE)
    operator = _string(value["operator_user_id"], "operator_user_id")
    if not SLACK_USER_RE.fullmatch(operator):
        raise IncrementError("invalid operator_user_id")
    _string(value["github_repository"], "github_repository", REPOSITORY_RE)
    allowed = required | {
        "kind", "control_issue", "issue_scope", "baseline_commit", "risk_accepted",
    }
    if set(value) - allowed:
        raise IncrementError("decision contains unknown fields")
    normalized = deepcopy(value)
    if value["action"] == "authorize":
        if not isinstance(value.get("kind", "normal"), str) or value.get("kind", "normal") not in {"normal", "emergency"}:
            raise IncrementError("kind must be normal or emergency")
        if isinstance(value.get("control_issue"), bool) or not isinstance(value.get("control_issue"), int) or value["control_issue"] <= 0:
            raise IncrementError("control_issue must be a positive issue number")
        normalized["issue_scope"] = _issues(value.get("issue_scope"))
        baseline = _string(value.get("baseline_commit"), "baseline_commit")
        if not COMMIT_RE.fullmatch(baseline):
            raise IncrementError("baseline_commit must be a full commit SHA")
        normalized["kind"] = value.get("kind", "normal")
        normalized["baseline_commit"] = baseline.lower()
    elif value["action"] == "revise":
        normalized["issue_scope"] = _issues(value.get("issue_scope"))
    elif any(key in value for key in ("kind", "control_issue", "issue_scope", "baseline_commit")):
        raise IncrementError(f"{value['action']} does not accept scope fields")
    if "risk_accepted" in value and not isinstance(value["risk_accepted"], bool):
        raise IncrementError("risk_accepted must be boolean")
    if "risk_accepted" in value and value["action"] != "accept":
        raise IncrementError("risk_accepted is valid only for sprint acceptance")
    return normalized


def _validate_record(value: Any) -> Dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    if not isinstance(value, dict):
        raise IncrementError("invalid increment record")
    required = {
        "increment_id", "project_id", "kind", "control_issue", "issue_scope",
        "baseline_commit", "lifecycle_state", "release_state", "created_at",
        "updated_at", "accepted_at", "deployed_at", "decisions",
    }
    if set(value) != required:
        raise IncrementError("invalid increment record schema")
    increment_id = _string(value["increment_id"], "increment_id", INCREMENT_ID_RE)
    project_id = _string(value["project_id"], "project_id", PROJECT_ID_RE)
    if not isinstance(value["kind"], str) or value["kind"] not in {"normal", "emergency"}:
        raise IncrementError("invalid increment kind")
    if isinstance(value["control_issue"], bool) or not isinstance(value["control_issue"], int) or value["control_issue"] <= 0:
        raise IncrementError("invalid control_issue")
    current_scope = _issues(value["issue_scope"])
    baseline = _string(value["baseline_commit"], "baseline_commit", COMMIT_RE).lower()
    if not isinstance(value["lifecycle_state"], str) or value["lifecycle_state"] not in LIFECYCLE_STATES:
        raise IncrementError("invalid lifecycle_state")
    if not isinstance(value["release_state"], str) or value["release_state"] not in RELEASE_STATES:
        raise IncrementError("invalid release_state")
    if not isinstance(value["decisions"], list) or not value["decisions"]:
        raise IncrementError("increment must retain operator decision evidence")

    state = ""
    release = "unreleased"
    scope: List[int] = []
    accepted_at = None
    deployed_at = None
    created_at = None
    previous_time: Optional[datetime] = None
    decision_ids = set()
    authority: Optional[tuple[str, str, str]] = None

    for index, item in enumerate(value["decisions"]):
        if not isinstance(item, dict) or set(item) != {"decision", "evidence", "scope_after"}:
            raise IncrementError("invalid decision history")
        decision = _validate_decision(item["decision"])
        evidence = _validate_evidence(item["evidence"])
        decided_time = _timestamp_value(evidence["recorded_at"], "recorded_at")
        if previous_time is not None and decided_time <= previous_time:
            raise IncrementError("decision history is not strictly chronological")
        previous_time = decided_time
        if decision["increment_id"] != increment_id or decision["project_id"] != project_id:
            raise IncrementError("decision history belongs to another increment")
        expected_decision_id = "|".join((
            project_id, evidence["slack_team_id"], evidence["slack_channel_id"],
            evidence["slack_event_id"],
        ))
        if decision["decision_id"] != expected_decision_id:
            raise IncrementError("decision identity does not match Slack evidence")
        if (
            decision["operator_user_id"] != evidence["slack_user_id"]
            or decision["github_repository"] != evidence["github_repository"]
        ):
            raise IncrementError("decision authority does not match operator evidence")
        current_authority = (
            evidence["slack_team_id"], evidence["slack_channel_id"],
            evidence["github_repository"],
        )
        if authority is None:
            authority = current_authority
        elif current_authority != authority:
            raise IncrementError("operator authority changed inside one increment")
        if f"/issues/{value['control_issue']}#" not in evidence["github_record_url"]:
            raise IncrementError("decision evidence is anchored to the wrong control issue")
        if decision["decision_id"] in decision_ids:
            raise IncrementError("duplicate decision_id")
        decision_ids.add(decision["decision_id"])
        scope_after = _issues(item["scope_after"])
        action = decision["action"]

        if index == 0:
            if (
                action != "authorize"
                or decision["kind"] != value["kind"]
                or decision["control_issue"] != value["control_issue"]
                or decision["baseline_commit"] != baseline
                or decision["issue_scope"] != scope_after
                or increment_id_for_event(project_id, evidence["slack_event_id"]) != increment_id
            ):
                raise IncrementError("increment identity does not match its authorization")
            state = "authorized"
            scope = scope_after
            created_at = evidence["recorded_at"]
            continue

        if action == "authorize":
            raise IncrementError("authorization may appear only once")
        if action == "revise":
            if state not in ACTIVE_NORMAL_STATES or decision["issue_scope"] != scope_after:
                raise IncrementError("invalid scope revision history")
            scope = scope_after
        else:
            if scope_after != scope:
                raise IncrementError("scope changed without a revise decision")
            if action == "start" and state == "authorized":
                state = "active"
            elif action == "accept" and state == "active":
                state = "accepted"
                accepted_at = evidence["recorded_at"]
            elif action == "authorize-deployment" and state == "accepted" and release == "unreleased":
                release = "deployment-authorized"
            elif action == "deployed" and state == "accepted" and release == "deployment-authorized":
                state = "closed"
                release = "deployed"
                deployed_at = evidence["recorded_at"]
            elif action == "cancel" and state in ACTIVE_NORMAL_STATES:
                state = "closed"
            else:
                raise IncrementError(f"invalid {action} transition in decision history")

    derived = {
        "issue_scope": scope,
        "lifecycle_state": state,
        "release_state": release,
        "created_at": created_at,
        "updated_at": value["decisions"][-1]["evidence"]["recorded_at"],
        "accepted_at": accepted_at,
        "deployed_at": deployed_at,
    }
    for key, expected in derived.items():
        if value[key] != expected:
            raise IncrementError(f"{key} does not match replayed decision history")
    if current_scope != scope:
        raise IncrementError("current scope does not match replayed decision history")
    normalized = deepcopy(value)
    normalized["baseline_commit"] = baseline
    normalized["issue_scope"] = scope
    return normalized


def _global_invariants(records: List[Dict[str, Any]]) -> None:
    events = []
    for record in records:
        for history in record["decisions"]:
            events.append((
                _timestamp_value(history["evidence"]["recorded_at"], "recorded_at"),
                history["decision"]["decision_id"], record, history["decision"],
            ))
    states: Dict[str, str] = {}
    releases: Dict[str, str] = {}
    kinds: Dict[str, str] = {}
    projects: Dict[str, str] = {}
    for _when, _decision_id, record, decision in sorted(events, key=lambda item: (item[0], item[1])):
        increment_id = record["increment_id"]
        project_id = record["project_id"]
        action = decision["action"]
        if action == "authorize":
            if record["kind"] == "normal" and any(
                projects.get(other) == project_id
                and kinds.get(other) == "normal"
                and states.get(other) in ACTIVE_NORMAL_STATES
                for other in states
            ):
                raise IncrementError(f"multiple active normal increments for {project_id}")
            states[increment_id] = "authorized"
            releases[increment_id] = "unreleased"
            kinds[increment_id] = record["kind"]
            projects[increment_id] = project_id
        elif action == "start":
            states[increment_id] = "active"
        elif action == "accept":
            held = [
                other for other in states
                if other != increment_id
                and projects.get(other) == project_id
                and states.get(other) == "accepted"
                and releases.get(other) != "deployed"
            ]
            if held and not decision.get("risk_accepted", False):
                raise IncrementError("accepted increment queue lacks explicit risk acceptance")
            states[increment_id] = "accepted"
        elif action == "authorize-deployment":
            releases[increment_id] = "deployment-authorized"
        elif action == "deployed":
            states[increment_id] = "closed"
            releases[increment_id] = "deployed"
        elif action == "cancel":
            states[increment_id] = "closed"


def _document(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "increments"}:
        raise IncrementError("invalid Delivery Increment registry")
    if (
        isinstance(value["schema"], bool)
        or not isinstance(value["schema"], int)
        or value["schema"] != SCHEMA_VERSION
    ):
        raise IncrementError("unsupported Delivery Increment registry schema")
    if not isinstance(value["increments"], list):
        raise IncrementError("invalid Delivery Increment registry")
    records = [_validate_record(item) for item in value["increments"]]
    ids = [item["increment_id"] for item in records]
    if len(ids) != len(set(ids)):
        raise IncrementError("duplicate increment_id")
    decision_ids = [
        history["decision"]["decision_id"]
        for record in records for history in record["decisions"]
    ]
    if len(decision_ids) != len(set(decision_ids)):
        raise IncrementError("decision_id reused across increments")
    _global_invariants(records)
    return {"schema": SCHEMA_VERSION, "increments": records}


class DeliveryIncrementStore:
    def __init__(self, path: Path = DEFAULT_INCREMENT_PATH):
        self.path = path

    def list(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        document = _document(_read_increment_json(
            self.path, {"schema": SCHEMA_VERSION, "increments": []}
        ))
        records = document["increments"]
        if project_id is not None:
            if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
                raise IncrementError("invalid project_id")
            records = [item for item in records if item["project_id"] == project_id]
        return deepcopy(records)

    def get(self, increment_id: str) -> Dict[str, Any]:
        matches = [item for item in self.list() if item["increment_id"] == increment_id]
        if len(matches) != 1:
            raise IncrementError(f"unknown or ambiguous increment: {increment_id}")
        return matches[0]

    def active(self, project_id: str) -> Optional[Dict[str, Any]]:
        matches = [
            item for item in self.list(project_id)
            if item["kind"] == "normal" and item["lifecycle_state"] in ACTIVE_NORMAL_STATES
        ]
        if len(matches) > 1:
            raise IncrementError(f"multiple active normal increments for {project_id}")
        return matches[0] if matches else None

    def apply_operator_decision(  # noqa: C901, PLR0915
        self, decision: Dict[str, Any], evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_decision = _validate_decision(decision)
        normalized_evidence = _validate_evidence(evidence)
        result: Dict[str, Any] = {}

        def update(raw: Any) -> Dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
            document = _document(raw)
            for record in document["increments"]:
                for history in record["decisions"]:
                    if history["decision"]["decision_id"] == normalized_decision["decision_id"]:
                        if (
                            history["decision"] != normalized_decision
                            or history["evidence"] != normalized_evidence
                        ):
                            raise IncrementError("decision_id was reused with different content")
                        result.update(deepcopy(record))
                        return document

            records = document["increments"]
            action = normalized_decision["action"]
            increment_id = normalized_decision["increment_id"]
            project_id = normalized_decision["project_id"]
            now = normalized_evidence["recorded_at"]
            if action == "authorize":
                if any(item["increment_id"] == increment_id for item in records):
                    raise IncrementError(f"increment already exists: {increment_id}")
                if normalized_decision["kind"] == "normal" and any(
                    item["project_id"] == project_id
                    and item["kind"] == "normal"
                    and item["lifecycle_state"] in ACTIVE_NORMAL_STATES
                    for item in records
                ):
                    raise IncrementError(f"project {project_id} already has an active increment")
                record = {
                    "increment_id": increment_id,
                    "project_id": project_id,
                    "kind": normalized_decision["kind"],
                    "control_issue": normalized_decision["control_issue"],
                    "issue_scope": normalized_decision["issue_scope"],
                    "baseline_commit": normalized_decision["baseline_commit"],
                    "lifecycle_state": "authorized",
                    "release_state": "unreleased",
                    "created_at": now,
                    "updated_at": now,
                    "accepted_at": None,
                    "deployed_at": None,
                    "decisions": [],
                }
                records.append(record)
            else:
                matches = [item for item in records if item["increment_id"] == increment_id]
                if len(matches) != 1 or matches[0]["project_id"] != project_id:
                    raise IncrementError(f"unknown increment for project: {increment_id}")
                record = matches[0]
                state = record["lifecycle_state"]
                if action == "revise":
                    if state not in ACTIVE_NORMAL_STATES:
                        raise IncrementError("only an authorized or active increment can be revised")
                    record["issue_scope"] = normalized_decision["issue_scope"]
                elif action == "start":
                    if state != "authorized":
                        raise IncrementError("only an authorized increment can start")
                    record["lifecycle_state"] = "active"
                elif action == "accept":
                    if state != "active":
                        raise IncrementError("only an active increment can be accepted")
                    held = [
                        item for item in records
                        if item is not record
                        and item["project_id"] == project_id
                        and item["lifecycle_state"] == "accepted"
                        and item["release_state"] != "deployed"
                    ]
                    if held and not normalized_decision.get("risk_accepted", False):
                        raise IncrementError(
                            "another accepted increment is undeployed; explicit risk acceptance required"
                        )
                    record["lifecycle_state"] = "accepted"
                    record["accepted_at"] = now
                elif action == "authorize-deployment":
                    if state != "accepted" or record["release_state"] != "unreleased":
                        raise IncrementError("deployment authorization requires an accepted increment")
                    record["release_state"] = "deployment-authorized"
                elif action == "deployed":
                    if state != "accepted" or record["release_state"] != "deployment-authorized":
                        raise IncrementError("deployment completion requires explicit authorization")
                    record["release_state"] = "deployed"
                    record["lifecycle_state"] = "closed"
                    record["deployed_at"] = now
                elif action == "cancel":
                    if state not in ACTIVE_NORMAL_STATES:
                        raise IncrementError("only an authorized or active increment can be cancelled")
                    record["lifecycle_state"] = "closed"
                else:  # pragma: no cover - validated above
                    raise IncrementError(f"unsupported action: {action}")
                record["updated_at"] = now

            history = {
                "decision": normalized_decision,
                "evidence": normalized_evidence,
                "scope_after": list(record["issue_scope"]),
            }
            record["decisions"].append(history)
            record["updated_at"] = now
            _validate_record(record)
            result.update(deepcopy(record))
            return document

        _mutate_increment_json(
            self.path, {"schema": SCHEMA_VERSION, "increments": []}, update
        )
        return result
