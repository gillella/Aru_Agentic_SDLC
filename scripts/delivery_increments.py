#!/usr/bin/env python3
"""Operator-authorized Delivery Increment records and transition rules."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from slack_projects import RegistryError, mutate_secure_json, read_secure_json


SCHEMA_VERSION = 1
DEFAULT_INCREMENT_PATH = Path.home() / ".aru" / "delivery-increments.json"
INCREMENT_ID_RE = re.compile(r"^inc_[0-9a-f]{20}$")
PROJECT_ID_RE = re.compile(r"^proj_[A-Za-z0-9_-]{3,64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
ACTIVE_NORMAL_STATES = {"authorized", "active"}
LIFECYCLE_STATES = {"authorized", "active", "accepted", "closed"}
RELEASE_STATES = {"unreleased", "deployment-authorized", "deployed"}
ACTIONS = {
    "authorize", "revise", "start", "accept",
    "authorize-deployment", "deployed", "cancel",
}


class IncrementError(RegistryError):
    """The requested increment decision is invalid or unsafe."""


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
    if not PROJECT_ID_RE.fullmatch(project_id) or not isinstance(event_id, str) or not event_id:
        raise IncrementError("project_id and Slack event id are required")
    digest = hashlib.sha256(f"{project_id}\0{event_id}".encode()).hexdigest()[:20]
    return f"inc_{digest}"


def operator_evidence(
    *, user_id: str, team_id: str, channel_id: str, event_id: str,
    github_record_url: str, recorded_at: str,
) -> Dict[str, Any]:
    evidence = {
        "source": "slack_control_room",
        "authenticated": True,
        "slack_user_id": user_id,
        "slack_team_id": team_id,
        "slack_channel_id": channel_id,
        "slack_event_id": event_id,
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
        "slack_channel_id", "slack_event_id", "github_record_url", "recorded_at",
    }
    if set(value) != required:
        raise IncrementError("operator evidence has an invalid schema")
    if value["source"] != "slack_control_room" or value["authenticated"] is not True:
        raise IncrementError("only an authenticated Slack operator decision is authoritative")
    for key in ("slack_user_id", "slack_team_id", "slack_channel_id", "slack_event_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise IncrementError(f"invalid {key}")
    if not value["slack_user_id"].startswith("U") or not value["slack_team_id"].startswith("T"):
        raise IncrementError("invalid Slack operator identity")
    if not value["slack_channel_id"].startswith(("C", "G")):
        raise IncrementError("invalid Slack channel identity")
    url = value["github_record_url"]
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise IncrementError("a durable GitHub decision URL is required")
    _timestamp(value["recorded_at"], "recorded_at")
    return deepcopy(value)


def _validate_decision(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise IncrementError("decision must be an object")
    required = {"decision_id", "action", "increment_id", "project_id"}
    if not required.issubset(value):
        raise IncrementError("decision identity is incomplete")
    if not isinstance(value["decision_id"], str) or not value["decision_id"]:
        raise IncrementError("decision_id is required")
    if value["action"] not in ACTIONS:
        raise IncrementError(f"unsupported decision action: {value['action']}")
    if not INCREMENT_ID_RE.fullmatch(str(value["increment_id"])):
        raise IncrementError("invalid increment_id")
    if not PROJECT_ID_RE.fullmatch(str(value["project_id"])):
        raise IncrementError("invalid project_id")
    allowed = required | {
        "kind", "control_issue", "issue_scope", "baseline_commit", "risk_accepted",
    }
    if set(value) - allowed:
        raise IncrementError("decision contains unknown fields")
    normalized = deepcopy(value)
    if value["action"] == "authorize":
        if value.get("kind", "normal") not in {"normal", "emergency"}:
            raise IncrementError("kind must be normal or emergency")
        if isinstance(value.get("control_issue"), bool) or not isinstance(value.get("control_issue"), int) or value["control_issue"] <= 0:
            raise IncrementError("control_issue must be a positive issue number")
        normalized["issue_scope"] = _issues(value.get("issue_scope"))
        if not COMMIT_RE.fullmatch(str(value.get("baseline_commit") or "")):
            raise IncrementError("baseline_commit must be a full commit SHA")
        normalized["kind"] = value.get("kind", "normal")
        normalized["baseline_commit"] = str(value["baseline_commit"]).lower()
    elif value["action"] == "revise":
        normalized["issue_scope"] = _issues(value.get("issue_scope"))
    elif any(key in value for key in ("kind", "control_issue", "issue_scope", "baseline_commit")):
        raise IncrementError(f"{value['action']} does not accept scope fields")
    if "risk_accepted" in value and not isinstance(value["risk_accepted"], bool):
        raise IncrementError("risk_accepted must be boolean")
    if value.get("risk_accepted") and value["action"] != "accept":
        raise IncrementError("risk_accepted is valid only for sprint acceptance")
    return normalized


def _validate_record(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise IncrementError("invalid increment record")
    required = {
        "increment_id", "project_id", "kind", "control_issue", "issue_scope",
        "baseline_commit", "lifecycle_state", "release_state", "created_at",
        "updated_at", "accepted_at", "deployed_at", "decisions",
    }
    if set(value) != required:
        raise IncrementError("invalid increment record schema")
    if not INCREMENT_ID_RE.fullmatch(str(value["increment_id"])):
        raise IncrementError("invalid increment_id")
    if not PROJECT_ID_RE.fullmatch(str(value["project_id"])):
        raise IncrementError("invalid project_id")
    if value["kind"] not in {"normal", "emergency"}:
        raise IncrementError("invalid increment kind")
    if isinstance(value["control_issue"], bool) or not isinstance(value["control_issue"], int) or value["control_issue"] <= 0:
        raise IncrementError("invalid control_issue")
    scope = _issues(value["issue_scope"])
    if not COMMIT_RE.fullmatch(str(value["baseline_commit"])):
        raise IncrementError("invalid baseline_commit")
    if value["lifecycle_state"] not in LIFECYCLE_STATES:
        raise IncrementError("invalid lifecycle_state")
    if value["release_state"] not in RELEASE_STATES:
        raise IncrementError("invalid release_state")
    _timestamp(value["created_at"], "created_at")
    _timestamp(value["updated_at"], "updated_at")
    for key in ("accepted_at", "deployed_at"):
        if value[key] is not None:
            _timestamp(value[key], key)
    if not isinstance(value["decisions"], list) or not value["decisions"]:
        raise IncrementError("increment must retain operator decision evidence")
    decision_ids = set()
    normalized_decisions = []
    for item in value["decisions"]:
        if not isinstance(item, dict) or set(item) != {"decision", "evidence", "scope_after"}:
            raise IncrementError("invalid decision history")
        decision = _validate_decision(item["decision"])
        evidence = _validate_evidence(item["evidence"])
        if (
            decision["increment_id"] != value["increment_id"]
            or decision["project_id"] != value["project_id"]
        ):
            raise IncrementError("decision history belongs to another increment")
        if f"/issues/{value['control_issue']}#" not in evidence["github_record_url"]:
            raise IncrementError("decision evidence is anchored to the wrong control issue")
        if decision["decision_id"] in decision_ids:
            raise IncrementError("duplicate decision_id")
        decision_ids.add(decision["decision_id"])
        normalized_scope = _issues(item["scope_after"])
        normalized_decisions.append((decision, evidence, normalized_scope))
    first_decision, _first_evidence, first_scope = normalized_decisions[0]
    if (
        first_decision["action"] != "authorize"
        or first_decision["kind"] != value["kind"]
        or first_decision["control_issue"] != value["control_issue"]
        or first_decision["baseline_commit"] != value["baseline_commit"]
        or first_decision["issue_scope"] != first_scope
    ):
        raise IncrementError("increment identity does not match its authorization")
    if normalized_decisions[-1][2] != scope:
        raise IncrementError("current scope does not match decision history")
    if normalized_decisions[-1][1]["recorded_at"] != value["updated_at"]:
        raise IncrementError("updated_at does not match the latest decision")
    if value["lifecycle_state"] == "accepted" and value["accepted_at"] is None:
        raise IncrementError("accepted increment is missing accepted_at")
    if value["release_state"] == "deployment-authorized" and value["lifecycle_state"] != "accepted":
        raise IncrementError("deployment authorization requires accepted lifecycle")
    if value["release_state"] == "deployed":
        if value["lifecycle_state"] != "closed" or value["deployed_at"] is None:
            raise IncrementError("deployed increment must be closed with deployed_at")
    elif value["deployed_at"] is not None:
        raise IncrementError("undeployed increment cannot have deployed_at")
    normalized = deepcopy(value)
    normalized["issue_scope"] = scope
    return normalized


def _document(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "increments"}:
        raise IncrementError("invalid Delivery Increment registry")
    if value["schema"] != SCHEMA_VERSION or not isinstance(value["increments"], list):
        raise IncrementError("unsupported Delivery Increment registry schema")
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
    return {"schema": SCHEMA_VERSION, "increments": records}


class DeliveryIncrementStore:
    def __init__(self, path: Path = DEFAULT_INCREMENT_PATH):
        self.path = path

    def list(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        document = _document(read_secure_json(
            self.path, {"schema": SCHEMA_VERSION, "increments": []}
        ))
        records = document["increments"]
        if project_id is not None:
            if not PROJECT_ID_RE.fullmatch(project_id):
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

    def apply_operator_decision(
        self, decision: Dict[str, Any], evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_decision = _validate_decision(decision)
        normalized_evidence = _validate_evidence(evidence)
        result: Dict[str, Any] = {}

        def update(raw: Any) -> Dict[str, Any]:
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

        mutate_secure_json(
            self.path, {"schema": SCHEMA_VERSION, "increments": []}, update
        )
        return result
