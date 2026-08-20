#!/usr/bin/env python3
# line-ceiling: 1204
"""Project-scoped agent presence and availability registry.

GitHub claims remain authoritative ownership. This registry only records which
agent *tasks* are present for a project, their availability, heartbeats, and
supported wake evidence. It never releases, steals, or restores a claim.

Persistence mirrors the #187 secure JSON discipline (0600, flock, atomic).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from slack_projects import (
    PROJECT_ID_RE,
    RegistryError,
    mutate_secure_json,
    read_secure_json,
)

SCHEMA_VERSION = 1
SCHEMA_NAME = "aru.agent-presence/v1"
DEFAULT_PRESENCE_PATH = Path.home() / ".aru" / "agent-presence.json"
DEFAULT_HEARTBEAT_TTL_SECONDS = 300
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FAMILY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Default ring used to auto-assign a free identity when the registry has no
# registered agents yet. Registered identities from `list` take precedence.
DEFAULT_AGENT_RING = ("gemini-1", "claude-1", "codex-1", "cursor-1", "cursor-2")

AVAILABILITY_STATES = frozenset({
    "available",
    "busy",
    "cooling-down",
    "temporarily-offline",
    "unavailable",
    "returned",
})

COOLDOWN_REASONS = frozenset({
    "credit-exhausted",
    "rate-limited",
    "provider-outage",
    "child-crash",
})

# Map runner / product family hints onto doctor product keys.
FAMILY_TO_PRODUCT = {
    "openai": "codex",
    "anthropic": "claude",
    "google": "antigravity",
    "cursor": "cursor",
}

# --- Worker fingerprint identity (#310) ------------------------------------
# Agent ids used to come from a shared pool, assigned per process. A restart was
# a new PID with no link to the worker that had been running, so it auto-assigned
# a different name; and two machines each arbitrated the pool from their own
# local registry, so both picked ring[0]. Deriving the id from *where the agent
# runs* makes it stable across restarts and distinct across machines without any
# coordination, which removes the collision class rather than policing it.

# Environment override for operators who want to name a worker themselves.
AGENT_ID_ENV_VAR = "ARU_AGENT_ID"
FINGERPRINT_LENGTH = 6


def _machine_identifier() -> str:
    """A value stable for the life of this machine.

    Deliberately not the MAC address or a hardware UUID: those need platform
    -specific probes and add failure modes for something that only has to be
    locally unique and locally stable.
    """
    return platform.node() or "unknown-host"


def worker_fingerprint(repo_root: Optional[str] = None, family: str = "",
                       machine: Optional[str] = None) -> str:
    """Short, stable hash of machine + checkout + family.

    The checkout path is hashed, never embedded: the id ends up in public
    GitHub labels, and an absolute path names the operator's home directory.
    """
    root = os.path.realpath(repo_root or os.getcwd())
    parts = "\x00".join([
        machine or _machine_identifier(),
        root,
        (family or "").strip().lower(),
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def product_for_family(family: str) -> str:
    """Readable prefix for an id, e.g. 'claude' for the anthropic family."""
    return FAMILY_TO_PRODUCT.get((family or "").strip().lower()) or "agent"


def fingerprint_agent_id(family: str = "", repo_root: Optional[str] = None,
                         machine: Optional[str] = None, seat: int = 1) -> str:
    """The id this worker resolves to, e.g. 'claude-a3f19c'.

    `seat` disambiguates a second live session sharing one checkout, which is
    the only case the fingerprint alone cannot separate. Seat 1 carries no
    suffix so the ordinary id stays short.
    """
    base = f"{product_for_family(family)}-{worker_fingerprint(repo_root, family, machine)}"
    return base if seat <= 1 else f"{base}-{seat}"


def configured_agent_id(env: Optional[Dict[str, str]] = None) -> str:
    """An operator-pinned id from the environment, or '' when unset."""
    source = env if env is not None else os.environ
    return (source.get(AGENT_ID_ENV_VAR) or "").strip()


PHASE_TO_AVAILABILITY = {
    "starting": "available",
    "waiting": "available",
    "complete_watch": "available",
    "blocked_wait": "available",
    "active": "busy",
    "agent_unavailable_wait": "cooling-down",
    "error_wait": "temporarily-offline",
    "stopped": "unavailable",
    "stopping": "unavailable",
}


class PresenceError(RegistryError):
    """Presence registry is unavailable, invalid, or rejects the mutation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Optional[datetime] = None) -> str:
    value = moment or _now()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_iso(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PresenceError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def path_derived_project_id(checkout: Path) -> str:
    """Last-resort project_id when GitHub identity cannot be resolved."""
    resolved = checkout.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"proj_path_{digest}"


def identity_derived_project_id(github_repo_id: str, project_v2_id: str) -> str:
    """Clone-independent project_id from durable GitHub repo + board ids."""
    repo = (github_repo_id or "").strip()
    board = (project_v2_id or "").strip()
    if not repo or not board:
        raise PresenceError("identity-derived project_id requires repo and board ids")
    digest = hashlib.sha256(f"{repo}|{board}".encode("utf-8")).hexdigest()[:20]
    return f"proj_repo_{digest}"


def resolve_project_id(  # noqa: C901, PLR0912
    checkout: Path,
    *,
    projects_path: Optional[Path] = None,
    identity_provider: Optional[Callable[[Path], Dict[str, Any]]] = None,
) -> str:
    """Resolve a shared project identity for presence.

    Preference order:
    1. Active #187 registry row whose ``local_path`` matches this checkout.
    2. Active registry row matching durable GitHub repo + ProjectV2 ids.
    3. Deterministic ``proj_repo_<hash>`` from those durable ids (clone-independent).
    4. Path hash only when GitHub identity cannot be discovered (offline/hermetic).
    """
    resolved = checkout.expanduser().resolve()
    identity: Optional[Dict[str, Any]] = None
    provider = identity_provider
    try:
        from slack_projects import ProjectRegistry, discover_checkout_identity

        if provider is None:
            provider = discover_checkout_identity
        registry = ProjectRegistry(projects_path) if projects_path else ProjectRegistry()
        records = registry.list(include_closed=False)
        for record in records:
            try:
                if Path(record.local_path).expanduser().resolve() == resolved:
                    return record.project_id
            except OSError:
                continue
        try:
            identity = provider(resolved)
        except (RegistryError, OSError, ValueError, TypeError):
            identity = None
        if identity:
            repo_id = str(identity.get("github_repo_id") or "")
            board_id = str(identity.get("project_v2_id") or "")
            for record in records:
                if (
                    record.github_repo_id == repo_id
                    and record.project_v2_id == board_id
                ):
                    return record.project_id
            if repo_id and board_id:
                return identity_derived_project_id(repo_id, board_id)
    except RegistryError:
        pass
    except OSError:
        pass
    if identity is None and provider is not None:
        try:
            identity = provider(resolved)
        except (RegistryError, OSError, ValueError, TypeError, PresenceError):
            identity = None
    if identity:
        repo_id = str(identity.get("github_repo_id") or "")
        board_id = str(identity.get("project_v2_id") or "")
        if repo_id and board_id:
            return identity_derived_project_id(repo_id, board_id)
    return path_derived_project_id(resolved)


def _empty_document() -> Dict[str, Any]:
    return {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "agents": {},
        "claims": {},
    }


def _validate_agent_id(agent_id: str) -> str:
    candidate = (agent_id or "").strip()
    if not AGENT_ID_RE.fullmatch(candidate):
        raise PresenceError(f"invalid agent_id: {agent_id}")
    return candidate


def _validate_family(family: str) -> str:
    candidate = (family or "").strip()
    if not FAMILY_RE.fullmatch(candidate):
        raise PresenceError(f"invalid family: {family}")
    return candidate


def _validate_project_id(project_id: str) -> str:
    candidate = (project_id or "").strip()
    if not PROJECT_ID_RE.fullmatch(candidate):
        raise PresenceError(f"invalid project_id: {project_id}")
    return candidate


def _validate_checkout(checkout_path: str) -> str:
    path = Path(checkout_path).expanduser()
    if not path.is_absolute():
        raise PresenceError("checkout_path must be absolute")
    return str(path)


def _validate_availability(state: str) -> str:
    candidate = (state or "").strip()
    if candidate not in AVAILABILITY_STATES:
        raise PresenceError(f"invalid availability: {state}")
    return candidate


@dataclass
class PresenceRecord:
    agent_id: str
    family: str
    project_id: str
    checkout_path: str
    capabilities: List[str] = field(default_factory=list)
    role: str = ""
    workload: Dict[str, Any] = field(default_factory=dict)
    availability: str = "available"
    last_heartbeat: str = ""
    cooldown_until: Optional[str] = None
    cooldown_reason: Optional[str] = None
    wake_evidence_supported: List[str] = field(default_factory=list)
    registered_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PresenceRecord":
        if not isinstance(value, dict):
            raise PresenceError("presence record must be an object")
        try:
            record = cls(
                agent_id=str(value["agent_id"]),
                family=str(value["family"]),
                project_id=str(value["project_id"]),
                checkout_path=str(value["checkout_path"]),
                capabilities=list(value.get("capabilities") or []),
                role=str(value.get("role") or ""),
                workload=dict(value.get("workload") or {}),
                availability=str(value.get("availability") or "available"),
                last_heartbeat=str(value.get("last_heartbeat") or ""),
                cooldown_until=value.get("cooldown_until"),
                cooldown_reason=value.get("cooldown_reason"),
                wake_evidence_supported=list(
                    value.get("wake_evidence_supported") or []
                ),
                registered_at=str(value.get("registered_at") or ""),
                updated_at=str(value.get("updated_at") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PresenceError(f"invalid presence record: {exc}") from exc
        record.validate()
        return record

    def validate(self) -> None:
        _validate_agent_id(self.agent_id)
        _validate_family(self.family)
        _validate_project_id(self.project_id)
        _validate_checkout(self.checkout_path)
        _validate_availability(self.availability)
        if not isinstance(self.capabilities, list) or not all(
            isinstance(item, str) for item in self.capabilities
        ):
            raise PresenceError("capabilities must be a list of strings")
        if not isinstance(self.wake_evidence_supported, list) or not all(
            isinstance(item, str) for item in self.wake_evidence_supported
        ):
            raise PresenceError("wake_evidence_supported must be a list of strings")
        if not isinstance(self.workload, dict):
            raise PresenceError("workload must be an object")
        if self.cooldown_until is not None:
            if not isinstance(self.cooldown_until, str):
                raise PresenceError("cooldown_until must be a string or null")
            _parse_iso(self.cooldown_until)
        if self.cooldown_reason is not None:
            if self.cooldown_reason not in COOLDOWN_REASONS:
                raise PresenceError(f"invalid cooldown_reason: {self.cooldown_reason}")
        if self.last_heartbeat:
            _parse_iso(self.last_heartbeat)
        if self.registered_at:
            _parse_iso(self.registered_at)
        if self.updated_at:
            _parse_iso(self.updated_at)

    def is_cooldown_expired(self, now: datetime) -> bool:
        if self.cooldown_until is None:
            return False
        try:
            deadline = _parse_iso(self.cooldown_until)
        except PresenceError:
            return False
        return now >= deadline

    def public_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PresenceStore:
    """Secure presence registry for one machine."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], datetime] = _now,
        heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ):
        self.path = path or DEFAULT_PRESENCE_PATH
        self.clock = clock
        self.heartbeat_ttl_seconds = int(heartbeat_ttl_seconds)

    def _read(self) -> Dict[str, Any]:
        document = read_secure_json(self.path, _empty_document())
        return self._validate_document(document)

    def _validate_document(self, document: Any) -> Dict[str, Any]:
        if not isinstance(document, dict):
            raise PresenceError("presence document must be an object")
        if document.get("schema") not in {None, SCHEMA_NAME}:
            raise PresenceError(f"unsupported presence schema: {document.get('schema')}")
        if document.get("version") not in {None, SCHEMA_VERSION}:
            raise PresenceError(f"unsupported presence version: {document.get('version')}")
        agents = document.get("agents")
        if agents is None:
            agents = {}
        if not isinstance(agents, dict):
            raise PresenceError("agents must be an object")
        validated: Dict[str, PresenceRecord] = {}
        for key, value in agents.items():
            record = PresenceRecord.from_dict(value)
            if record.agent_id != key:
                raise PresenceError(f"agent key mismatch: {key}")
            validated[key] = record
        return {
            "schema": SCHEMA_NAME,
            "version": SCHEMA_VERSION,
            "agents": {agent_id: record.public_dict() for agent_id, record in validated.items()},
            "claims": document.get("claims") or {},
            "_records": validated,
        }

    def get(self, agent_id: str) -> Optional[PresenceRecord]:
        agent_id = _validate_agent_id(agent_id)
        return self._read()["_records"].get(agent_id)

    def identity_holder(self, agent_id: str, session_id: str, now: Optional[datetime] = None) -> Optional[str]:
        """Return the live session id holding `agent_id`, or None if free.

        A claim is live when it is fresher than the heartbeat TTL. A claim held
        by this very session is not a conflict.
        """
        agent_id = _validate_agent_id(agent_id)
        now = now or self.clock()
        claim = (self._read().get("claims") or {}).get(agent_id)
        if not claim or not isinstance(claim, dict):
            return None
        try:
            at = _parse_iso(claim.get("at", ""))
        except PresenceError:
            return None
        if (now - at).total_seconds() >= self.heartbeat_ttl_seconds:
            return None
        holder = claim.get("session")
        if holder == session_id:
            return None
        return holder

    def resolve_free_identity(self, pool, session_id: str, now: Optional[datetime] = None) -> str:
        """Atomically claim and return one free identity from `pool`.

        Free means not claimed by another live session. Resolution runs inside
        the file-locked mutate, so two concurrent resolutions can never return
        the same identity across processes.
        """
        pool = list(dict.fromkeys(_validate_agent_id(a) for a in pool))
        if pool == [] or not session_id:
            raise PresenceError("resolve_free_identity needs a non-empty pool and a session id")
        now = now or self.clock()
        now_iso = _iso(now)

        def apply(document: Dict[str, Any]) -> str:
            claims = document.get("claims") or {}
            live: Dict[str, Dict[str, Any]] = {}
            for agent_id, claim in claims.items():
                if not isinstance(claim, dict):
                    continue
                try:
                    at = _parse_iso(claim.get("at", ""))
                except PresenceError:
                    continue
                if (now - at).total_seconds() < self.heartbeat_ttl_seconds:
                    live[agent_id] = claim
            busy = {a for a, c in live.items() if c.get("session") != session_id}
            free = [a for a in pool if a not in busy]
            if not free:
                raise PresenceError(
                    "no free agent identity; all are held by another live session: "
                    + ", ".join(sorted(busy))
                )
            chosen = free[0]
            updated = dict(live)
            updated[chosen] = {"session": session_id, "at": now_iso}
            document["claims"] = updated
            return chosen

        return self._mutate(apply)

    def _mutate(self, updater: Callable[[Dict[str, Any]], Any]) -> Any:
        """Apply an in-place document updater; persist the document, return result."""
        result: Dict[str, Any] = {}

        def apply(document: Dict[str, Any]) -> Dict[str, Any]:
            result["value"] = updater(document)
            # Drop ephemeral validation cache before write.
            document.pop("_records", None)
            if "schema" not in document:
                document["schema"] = SCHEMA_NAME
            if "version" not in document:
                document["version"] = SCHEMA_VERSION
            if "agents" not in document or not isinstance(document["agents"], dict):
                raise PresenceError("presence mutation lost agents map")
            self._validate_document(document)
            return {
                "schema": SCHEMA_NAME,
                "version": SCHEMA_VERSION,
                "agents": document["agents"],
                "claims": document.get("claims") or {},
            }

        mutate_secure_json(self.path, _empty_document(), apply)
        return result.get("value")

    def register(
        self,
        *,
        agent_id: str,
        family: str,
        project_id: str,
        checkout_path: str,
        capabilities: Optional[Sequence[str]] = None,
        role: str = "",
        workload: Optional[Dict[str, Any]] = None,
        availability: str = "available",
        wake_evidence_supported: Optional[Sequence[str]] = None,
        cooldown_until: Optional[str] = None,
        cooldown_reason: Optional[str] = None,
    ) -> PresenceRecord:
        """Bind one agent task to exactly one project identity."""
        agent_id = _validate_agent_id(agent_id)
        family = _validate_family(family)
        project_id = _validate_project_id(project_id)
        checkout_path = _validate_checkout(checkout_path)
        availability = _validate_availability(availability)
        now = _iso(self.clock())

        def apply(document: Dict[str, Any]) -> PresenceRecord:
            validated = self._validate_document(document)
            existing = validated["_records"].get(agent_id)
            if existing and existing.project_id != project_id:
                raise PresenceError(
                    f"agent {agent_id} is already registered to {existing.project_id}"
                )
            record = PresenceRecord(
                agent_id=agent_id,
                family=family,
                project_id=project_id,
                checkout_path=checkout_path,
                capabilities=list(capabilities or (existing.capabilities if existing else [])),
                role=role if role or not existing else existing.role,
                workload=dict(workload if workload is not None else (
                    existing.workload if existing else {}
                )),
                availability=availability,
                last_heartbeat=now,
                cooldown_until=(
                    cooldown_until if availability == "cooling-down" else None
                ),
                cooldown_reason=cooldown_reason if availability == "cooling-down" else None,
                wake_evidence_supported=list(
                    wake_evidence_supported
                    or (existing.wake_evidence_supported if existing else [])
                ),
                registered_at=existing.registered_at if existing else now,
                updated_at=now,
            )
            record.validate()
            document["schema"] = SCHEMA_NAME
            document["version"] = SCHEMA_VERSION
            document["agents"] = dict(validated["agents"])
            document["agents"][agent_id] = record.public_dict()
            return record

        return self._mutate(apply)

    def heartbeat(
        self,
        agent_id: str,
        *,
        availability: Optional[str] = None,
        role: Optional[str] = None,
        workload: Optional[Dict[str, Any]] = None,
        cooldown_until: Optional[str] = None,
        cooldown_reason: Optional[str] = None,
    ) -> PresenceRecord:
        agent_id = _validate_agent_id(agent_id)
        now = _iso(self.clock())

        def apply(document: Dict[str, Any]) -> PresenceRecord:
            validated = self._validate_document(document)
            existing = validated["_records"].get(agent_id)
            if existing is None:
                raise PresenceError(f"unknown agent_id: {agent_id}")
            next_availability = existing.availability
            if (
                existing.availability in {"cooling-down", "temporarily-offline"}
                and availability == "available"
            ):
                next_availability = "returned"
            elif availability is not None:
                next_availability = _validate_availability(availability)
            elif existing.availability == "temporarily-offline":
                next_availability = "returned"
            record = PresenceRecord(
                agent_id=existing.agent_id,
                family=existing.family,
                project_id=existing.project_id,
                checkout_path=existing.checkout_path,
                capabilities=list(existing.capabilities),
                role=existing.role if role is None else str(role),
                workload=dict(existing.workload if workload is None else workload),
                availability=next_availability,
                last_heartbeat=now,
                cooldown_until=(
                    existing.cooldown_until if cooldown_until is None else cooldown_until
                ) if next_availability == "cooling-down" else None,
                cooldown_reason=(
                    cooldown_reason if cooldown_reason is not None
                    else existing.cooldown_reason
                ) if next_availability == "cooling-down" else None,
                wake_evidence_supported=list(existing.wake_evidence_supported),
                registered_at=existing.registered_at,
                updated_at=now,
            )
            record.validate()
            document["schema"] = SCHEMA_NAME
            document["version"] = SCHEMA_VERSION
            document["agents"] = dict(validated["agents"])
            document["agents"][agent_id] = record.public_dict()
            return record

        return self._mutate(apply)

    def set_availability(
        self,
        agent_id: str,
        availability: str,
        *,
        cooldown_until: Optional[str] = None,
        cooldown_reason: Optional[str] = None,
        role: Optional[str] = None,
        workload: Optional[Dict[str, Any]] = None,
        touch_heartbeat: bool = True,
    ) -> PresenceRecord:
        agent_id = _validate_agent_id(agent_id)
        availability = _validate_availability(availability)
        now = _iso(self.clock())

        def apply(document: Dict[str, Any]) -> PresenceRecord:
            validated = self._validate_document(document)
            existing = validated["_records"].get(agent_id)
            if existing is None:
                raise PresenceError(f"unknown agent_id: {agent_id}")
            record = PresenceRecord(
                agent_id=existing.agent_id,
                family=existing.family,
                project_id=existing.project_id,
                checkout_path=existing.checkout_path,
                capabilities=list(existing.capabilities),
                role=existing.role if role is None else str(role),
                workload=dict(existing.workload if workload is None else workload),
                availability=availability,
                last_heartbeat=now if touch_heartbeat else existing.last_heartbeat,
                cooldown_until=(
                    existing.cooldown_until if cooldown_until is None else cooldown_until
                ) if availability == "cooling-down" else None,
                cooldown_reason=(
                    cooldown_reason if cooldown_reason is not None else existing.cooldown_reason
                ) if availability == "cooling-down" else None,
                wake_evidence_supported=list(existing.wake_evidence_supported),
                registered_at=existing.registered_at,
                updated_at=now,
            )
            record.validate()
            document["schema"] = SCHEMA_NAME
            document["version"] = SCHEMA_VERSION
            document["agents"] = dict(validated["agents"])
            document["agents"][agent_id] = record.public_dict()
            return record

        return self._mutate(apply)

    def unregister(self, agent_id: str) -> PresenceRecord:
        """Remove a registration so the agent id may bind to another project."""
        agent_id = _validate_agent_id(agent_id)

        def apply(document: Dict[str, Any]) -> PresenceRecord:
            validated = self._validate_document(document)
            existing = validated["_records"].get(agent_id)
            if existing is None:
                raise PresenceError(f"unknown agent_id: {agent_id}")
            agents = dict(validated["agents"])
            del agents[agent_id]
            document["schema"] = SCHEMA_NAME
            document["version"] = SCHEMA_VERSION
            document["agents"] = agents
            return existing

        return self._mutate(apply)

    def query_project(
        self,
        *,
        project_id: Optional[str] = None,
        checkout_path: Optional[str] = None,
        expire: bool = True,
    ) -> List[PresenceRecord]:
        if project_id is None and checkout_path is None:
            raise PresenceError("query_project requires project_id or checkout_path")
        if project_id is not None:
            project_id = _validate_project_id(project_id)
        resolved_checkout = None
        if checkout_path is not None:
            resolved_checkout = str(Path(_validate_checkout(checkout_path)).resolve())
        if expire:
            self.expire_stale()
        records = list(self._read()["_records"].values())
        matched = []
        for record in records:
            if project_id is not None and record.project_id == project_id:
                matched.append(record)
                continue
            if resolved_checkout is not None:
                try:
                    if str(Path(record.checkout_path).resolve()) == resolved_checkout:
                        matched.append(record)
                except OSError:
                    continue
        return sorted(matched, key=lambda item: item.agent_id)

    def expire_stale(
        self,
        *,
        now: Optional[datetime] = None,
        ttl_seconds: Optional[int] = None,
    ) -> List[PresenceRecord]:
        """Mark stale heartbeats temporarily-offline without deleting records."""
        moment = now or self.clock()
        ttl = self.heartbeat_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        cutoff = moment - timedelta(seconds=ttl)
        changed: List[PresenceRecord] = []

        def apply(document: Dict[str, Any]) -> List[PresenceRecord]:
            validated = self._validate_document(document)
            agents = dict(validated["agents"])
            updated_at = _iso(moment)
            local_changed: List[PresenceRecord] = []
            for agent_id, record in validated["_records"].items():
                if record.availability in {"unavailable", "temporarily-offline"}:
                    continue
                if not record.last_heartbeat:
                    continue
                try:
                    last = _parse_iso(record.last_heartbeat)
                except PresenceError:
                    continue
                if last >= cutoff:
                    continue
                stale = PresenceRecord(
                    agent_id=record.agent_id,
                    family=record.family,
                    project_id=record.project_id,
                    checkout_path=record.checkout_path,
                    capabilities=list(record.capabilities),
                    role=record.role,
                    workload=dict(record.workload),
                    availability="temporarily-offline",
                    last_heartbeat=record.last_heartbeat,
                    cooldown_until=None,
                    cooldown_reason=None,
                    wake_evidence_supported=list(record.wake_evidence_supported),
                    registered_at=record.registered_at,
                    updated_at=updated_at,
                )
                agents[agent_id] = stale.public_dict()
                local_changed.append(stale)
            document["schema"] = SCHEMA_NAME
            document["version"] = SCHEMA_VERSION
            document["agents"] = agents
            changed.extend(local_changed)
            return list(local_changed)

        return self._mutate(apply)


def evaluate_claim_protection(
    record: PresenceRecord,
    *,
    now: datetime,
    warning_seconds: int = 600,
    takeover_seconds: int = 1800,
    live_process: Optional[bool] = None,
    recent_branch_activity: Optional[bool] = None,
    resumable: Optional[bool] = None,
) -> Dict[str, Any]:
    """Advisory, fail-closed claim protection evaluation.

    A stale presence record is not enough to permit takeover. The caller must
    also supply affirmative evidence that no process is alive, no branch was
    recently active, and the work is explicitly resumable. This function never
    releases or transfers a GitHub claim.
    """
    if warning_seconds < 0 or takeover_seconds < warning_seconds:
        return {
            "protected": True,
            "phase": "warning",
            "reason": "invalid warning/takeover window configuration",
        }
    if not record.last_heartbeat:
        return {"protected": True, "phase": "warning", "reason": "no heartbeat evidence"}
    try:
        last = _parse_iso(record.last_heartbeat)
    except PresenceError:
        return {"protected": True, "phase": "warning", "reason": "invalid heartbeat evidence"}
    stale_seconds = (now - last).total_seconds()
    if stale_seconds < 0:
        stale_seconds = 0
    if stale_seconds <= warning_seconds:
        return {"protected": True, "phase": "active", "reason": "heartbeat fresh"}
    if stale_seconds <= takeover_seconds:
        return {"protected": True, "phase": "warning", "reason": f"stale {stale_seconds:.0f}s"}
    if record.availability not in {"cooling-down", "temporarily-offline"}:
        return {"protected": True, "phase": "warning", "reason": f"availability is {record.availability}"}
    missing = []
    if live_process is not False:
        missing.append("no-live-process evidence")
    if recent_branch_activity is not False:
        missing.append("no-recent-branch-activity evidence")
    if resumable is not True:
        missing.append("explicitly-resumable evidence")
    if missing:
        return {
            "protected": True,
            "phase": "warning",
            "reason": "takeover blocked: " + ", ".join(missing),
        }
    return {"protected": False, "phase": "takeover", "reason": f"stale {stale_seconds:.0f}s, {record.availability}"}


def query_cooling_agents(
    store: PresenceStore,
    *,
    project_id: Optional[str] = None,
    checkout_path: Optional[str] = None,
) -> List[PresenceRecord]:
    """Return agents in cooling-down state for a project."""
    all_records = store.query_project(
        project_id=project_id,
        checkout_path=checkout_path,
        expire=False,
    )
    return [r for r in all_records if r.availability == "cooling-down"]


def query_role_poll_agents(
    store: PresenceStore,
    *,
    project_id: Optional[str] = None,
    checkout_path: Optional[str] = None,
) -> List[PresenceRecord]:
    """Return project agents eligible for a new, non-claiming role poll."""
    all_records = store.query_project(
        project_id=project_id,
        checkout_path=checkout_path,
        expire=False,
    )
    return [r for r in all_records if r.availability in {"available", "returned"}]


def availability_for_phase(phase: str) -> str:
    return PHASE_TO_AVAILABILITY.get(phase, "available")


def doctor_presence_summary(
    *,
    project: Optional[str],
    agents: Dict[str, Dict[str, Any]],
    store: Optional[PresenceStore] = None,
    catalog_non_guarantees: Optional[Sequence[str]] = None,
    projects_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only presence + wake-limitation summary for the doctor payload."""
    store = store or PresenceStore()
    records: List[PresenceRecord] = []
    project_id = None
    error = None
    if project:
        try:
            project_id = resolve_project_id(Path(project), projects_path=projects_path)
            # Never expire/mutate on doctor: diagnosis must stay read-only.
            records = store.query_project(
                checkout_path=project,
                project_id=project_id,
                expire=False,
            )
        except (PresenceError, OSError) as exc:
            error = type(exc).__name__
            records = []
    by_product: Dict[str, List[Dict[str, Any]]] = {
        name: [] for name in agents
    }
    for record in records:
        product = FAMILY_TO_PRODUCT.get(record.family.lower())
        if product is None:
            for name in agents:
                if record.agent_id.lower().startswith(name.lower()):
                    product = name
                    break
        payload = record.public_dict()
        if product and product in by_product:
            by_product[product].append(payload)
            existing = agents[product].get("last_heartbeat")
            if (
                not existing
                or _parse_iso(record.last_heartbeat) > _parse_iso(str(existing))
            ):
                agents[product]["last_heartbeat"] = record.last_heartbeat
            agents[product]["presence_availability"] = record.availability
    wake_limitations = [
        "App quit, logout, sleep, power loss, credits, and vendor termination "
        "are non-guarantees.",
        "Presence never launches agents or consumes paid wake usage.",
        "Same-task native wake remains vendor-specific; doctor reports evidence only.",
        "Presence heartbeats expire availability without transferring GitHub ownership.",
    ]
    if catalog_non_guarantees:
        wake_limitations = wake_limitations + list(catalog_non_guarantees)
    return {
        "schema": SCHEMA_NAME,
        "project": project,
        "project_id": project_id,
        "error": error,
        "tasks": [record.public_dict() for record in records],
        "by_product": by_product,
        "wake_limitations": wake_limitations,
        "ownership": "GitHub claims remain authoritative; presence never releases or steals claims.",
    }


def sync_runner_presence(
    store: PresenceStore,
    *,
    agent_id: str,
    family: str,
    checkout_path: Path,
    phase: str,
    project_id: Optional[str] = None,
    capabilities: Optional[Sequence[str]] = None,
    role: str = "",
    workload: Optional[Dict[str, Any]] = None,
    cooldown_until: Optional[str] = None,
    cooldown_reason: Optional[str] = None,
    wake_evidence_supported: Optional[Sequence[str]] = None,
) -> Optional[PresenceRecord]:
    """Best-effort presence update for run_fleet; never raises into the runner."""
    try:
        checkout = checkout_path.expanduser().resolve()
        resolved_project = project_id or resolve_project_id(checkout)
        availability = availability_for_phase(phase)
        existing = store.get(agent_id)
        if existing is None:
            return store.register(
                agent_id=agent_id,
                family=family,
                project_id=resolved_project,
                checkout_path=str(checkout),
                capabilities=capabilities or ["factory-loop"],
                role=role,
                workload=workload or {},
                availability=availability,
                wake_evidence_supported=wake_evidence_supported or [],
                cooldown_until=cooldown_until,
                cooldown_reason=cooldown_reason,
            )
        return store.heartbeat(
            agent_id,
            availability=availability,
            role=role if role else None,
            workload=workload,
            cooldown_until=cooldown_until,
            cooldown_reason=cooldown_reason,
        )
    except (PresenceError, OSError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project-scoped agent presence for desktop and headless tasks.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_PRESENCE_PATH,
        help="Presence registry path (default: ~/.aru/agent-presence.json)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="command")

    register = sub.add_parser("register", help="Register or refresh this task's presence")
    register.add_argument("--agent", required=True)
    register.add_argument("--family", required=True)
    register.add_argument("--checkout", type=Path, required=True)
    register.add_argument("--project-id", default="")
    register.add_argument("--role", default="")
    register.add_argument(
        "--availability",
        default="available",
        choices=sorted(AVAILABILITY_STATES),
    )
    register.add_argument("--capability", action="append", default=[])
    register.add_argument("--wake-evidence", action="append", default=[])
    register.add_argument("--cooldown-until", default=None)
    register.add_argument("--cooldown-reason", choices=sorted(COOLDOWN_REASONS))

    heartbeat = sub.add_parser("heartbeat", help="Refresh heartbeat / availability")
    heartbeat.add_argument("--agent", required=True)
    heartbeat.add_argument(
        "--availability",
        default=None,
        choices=sorted(AVAILABILITY_STATES),
    )
    heartbeat.add_argument("--role", default=None)
    heartbeat.add_argument("--cooldown-until", default=None)
    heartbeat.add_argument("--cooldown-reason", choices=sorted(COOLDOWN_REASONS))

    availability = sub.add_parser(
        "set-availability", help="Set availability without changing project binding",
    )
    availability.add_argument("--agent", required=True)
    availability.add_argument(
        "--availability",
        required=True,
        choices=sorted(AVAILABILITY_STATES),
    )
    availability.add_argument("--cooldown-until", default=None)
    availability.add_argument("--cooldown-reason", choices=sorted(COOLDOWN_REASONS))

    unregister = sub.add_parser(
        "unregister",
        help="Remove registration so this agent id may bind to another project",
    )
    unregister.add_argument("--agent", required=True)

    listing = sub.add_parser("list", help="List presence tasks (default command)")
    listing.add_argument("--project-id", default="")
    listing.add_argument("--checkout", type=Path)
    listing.add_argument(
        "--expire",
        action="store_true",
        help="Apply heartbeat TTL mutations before listing",
    )

    expire = sub.add_parser("expire", help="Mark stale heartbeats temporarily-offline")
    _ = expire

    resolve = sub.add_parser(
        "resolve-project-id",
        help="Print the clone-independent project_id for a checkout",
    )
    resolve.add_argument("--checkout", type=Path, required=True)
    resolve.add_argument("--projects-path", type=Path)

    return parser


def _print_record(record: PresenceRecord, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(record.public_dict(), indent=2, sort_keys=True))
        return
    print(
        f"{record.agent_id} project={record.project_id} "
        f"availability={record.availability} "
        f"heartbeat={record.last_heartbeat or 'unknown'}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:  # noqa: C901, PLR0912, PLR0915
    argv_list = list(argv) if argv is not None else list(sys.argv[1:])
    known = {
        "register", "heartbeat", "set-availability", "unregister",
        "list", "expire", "resolve-project-id",
    }
    if not any(token in known for token in argv_list):
        argv_list = ["list", *argv_list]

    args = build_parser().parse_args(argv_list)
    store = PresenceStore(args.path)
    command = args.command or "list"

    try:
        if command == "register":
            checkout = args.checkout.expanduser().resolve()
            project_id = args.project_id or resolve_project_id(checkout)
            record = store.register(
                agent_id=args.agent,
                family=args.family,
                project_id=project_id,
                checkout_path=str(checkout),
                capabilities=args.capability or ["factory-loop"],
                role=args.role,
                availability=args.availability,
                wake_evidence_supported=args.wake_evidence or ["github-recovery"],
                cooldown_until=args.cooldown_until,
                cooldown_reason=args.cooldown_reason,
            )
            _print_record(record, as_json=args.json)
            return 0

        if command == "heartbeat":
            record = store.heartbeat(
                args.agent,
                availability=args.availability,
                role=args.role,
                cooldown_until=args.cooldown_until,
                cooldown_reason=args.cooldown_reason,
            )
            _print_record(record, as_json=args.json)
            return 0

        if command == "set-availability":
            record = store.set_availability(
                args.agent,
                args.availability,
                cooldown_until=args.cooldown_until,
                cooldown_reason=args.cooldown_reason,
            )
            _print_record(record, as_json=args.json)
            return 0

        if command == "unregister":
            record = store.unregister(args.agent)
            if args.json:
                print(json.dumps({"unregistered": record.public_dict()}, indent=2, sort_keys=True))
            else:
                print(f"unregistered {record.agent_id} from {record.project_id}")
            return 0

        if command == "expire":
            changed = store.expire_stale()
            payload = {
                "schema": SCHEMA_NAME,
                "changed": [item.public_dict() for item in changed],
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"expired {len(changed)} stale presence task(s)")
            return 0

        if command == "resolve-project-id":
            checkout = args.checkout.expanduser().resolve()
            project_id = resolve_project_id(
                checkout,
                projects_path=args.projects_path,
            )
            if args.json:
                print(json.dumps({
                    "checkout": str(checkout),
                    "project_id": project_id,
                }, indent=2, sort_keys=True))
            else:
                print(project_id)
            return 0

        # list
        if getattr(args, "expire", False):
            store.expire_stale()
        project_id = getattr(args, "project_id", "") or None
        checkout = getattr(args, "checkout", None)
        if project_id or checkout:
            records = store.query_project(
                project_id=project_id or None,
                checkout_path=str(checkout.resolve()) if checkout else None,
                expire=False,
            )
        else:
            records = list(store._read()["_records"].values())
        payload = {
            "schema": SCHEMA_NAME,
            "path": str(args.path),
            "tasks": [record.public_dict() for record in records],
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"presence tasks={len(records)} path={args.path}")
            for record in records:
                print(
                    f"  {record.agent_id} project={record.project_id} "
                    f"availability={record.availability} "
                    f"heartbeat={record.last_heartbeat or 'unknown'}"
                )
        return 0
    except PresenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
