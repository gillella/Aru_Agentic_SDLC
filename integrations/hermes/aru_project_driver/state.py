"""Small operational journal. It never stores or assigns issue lifecycle status."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import DriverError

MAX_EVENT_KEYS = 65_536

def key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]
def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists() and default is not None:
        return dict(default)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DriverError(f"operational state unreadable: {path.name}") from exc
    if not isinstance(data, dict):
        raise DriverError("operational state must be an object")
    return data

def _timestamp(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0
def _validate_project(data: dict) -> None:
    if data.get("acknowledged_stop") is not None and not _nonce(data["acknowledged_stop"]):
        raise DriverError("operational Stop acknowledgment is invalid")
    if type(data.get("generation")) is not int or data["generation"] < 0:
        raise DriverError("operational project generation is invalid")
    events = data.get("events")
    if not isinstance(events, list) or len(events) > 256:
        raise DriverError("operational project events are invalid")
    keys = set()
    for event in events:
        if (not isinstance(event, dict) or not isinstance(event.get("key"), str)
                or not re.fullmatch(r"[a-f0-9]{24}", event["key"])
                or event["key"] in keys or not isinstance(event.get("reason"), str)
                or len(event["reason"]) > 120 or not _timestamp(event.get("observed_at"))):
            raise DriverError("operational project event entry is invalid")
        keys.add(event["key"])
    deliveries = data.get("delivery_keys", list(keys))
    if (not isinstance(deliveries, list) or len(deliveries) > MAX_EVENT_KEYS
            or any(not isinstance(k, str) or not re.fullmatch(r"[a-f0-9]{24}", k) for k in deliveries)
            or len(deliveries) != len(set(deliveries)) or not keys.issubset(set(deliveries))
            or len(deliveries) > data["generation"]):
        raise DriverError("operational delivery keys are invalid")
    data["delivery_keys"] = deliveries
    for field in ("cooldown_until", "wake_pending_until", "last_checked_at", "last_reconciled_at",
                  "started_at", "stopped_at"):
        if field in data and not _timestamp(data[field]):
            raise DriverError(f"operational project {field} is invalid")
    handled = data.get("handled_generation", 0)
    if type(handled) is not int or not 0 <= handled <= data["generation"]:
        raise DriverError("operational project handled_generation is invalid")

def _validate_worker(data: dict) -> None:
    from .quota_worker import validate_record
    from . import retries
    validate_record(data)
    retries.validate(data)
    if "retry_blocked" in data and (type(data["retry_blocked"]) is not bool
            or not isinstance(data.get("policy_fingerprint"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", data["policy_fingerprint"])
            or not isinstance(data.get("reason"), str)):
        raise DriverError("worker retry blocker requires typed outcome and policy evidence")
    if data.get("admission_stop") is not None and not _nonce(data["admission_stop"]):
        raise DriverError("worker Stop admission is invalid")
    if any(not isinstance(data.get(field), str) or not data[field] for field in (
        "id", "repo", "agent", "capacity_key", "state",
    )):
        raise DriverError("operational worker identity is invalid")
    if (type(data.get("issue")) is not int or data["issue"] <= 0
            or data["state"] not in {"claiming", "prepared", "launching", "running", "launch_failed", "exited"}
            or not _timestamp(data.get("started_at"))):
        raise DriverError("operational worker state is invalid")
    worktree = data.get("worktree")
    if worktree is not None and (not isinstance(worktree, str) or not Path(worktree).is_absolute()):
        raise DriverError("operational worker worktree is invalid")
    for field in ("pid", "child_pid"):
        if data.get(field) is not None and (type(data[field]) is not int or data[field] <= 0):
            raise DriverError("operational worker process identity is invalid")

def _nonce(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None
class State:
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def lock(self, *, blocking: bool = False):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "coordination.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise DriverError("another Driver activation is coordinating work") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def project_path(self, repo: str) -> Path:
        return self.root / "projects" / f"{key(repo)}.json"

    def stop_intent(self, repo: str) -> dict:
        path = self.root / "stops" / f"{key(repo)}.json"
        if not path.exists():
            return {}
        data = read_json(path)
        if (data.get("repo") != repo or not _nonce(data.get("nonce"))
                or not _timestamp(data.get("stopped_at"))):
            raise DriverError("operational Stop intent is invalid")
        return data

    def stop_nonce(self, repo: str) -> str | None:
        return self.stop_intent(repo).get("nonce")

    def request_stop(self, repo: str) -> str:
        """Fence dispatch without waiting for any coordinator or scheduler.

        This is one operational control value, not issue lifecycle state. Start
        acknowledges a nonce; no normal project snapshot can remove this fence.
        """
        nonce = uuid.uuid4().hex
        path = self.root / "stops" / f"{key(repo)}.json"
        write_json(path, {"repo": repo, "nonce": nonce, "stopped_at": time.time()})
        for directory in (path.parent, self.root):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                # The root sync also persists the first creation of stops/.
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return nonce

    @contextmanager
    def project_lock(self, repo: str, *, spawn: bool = False):
        """Local Start/Stop serialization, or the short check-and-spawn barrier.

        The spawn barrier must never contain provider, GitHub or scheduler calls.
        Bounded Stop supplies an interruptible deadline while crossing either lock.
        """
        directory = self.root / "project-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        suffix = "spawn" if spawn else "control"
        with (directory / f"{key(repo)}.{suffix}.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def require_admission(self, repo: str, nonce: str | None) -> None:
        if not self.project(repo)["enabled"] or self.stop_nonce(repo) != nonce:
            raise DriverError("project stopped or worker admission predates Stop")

    def project(self, repo: str) -> dict:
        result = read_json(self.project_path(repo), {
            "repo": repo, "enabled": False, "generation": 0, "events": [],
            "last_fingerprint": None, "wake_pending_until": 0, "cooldown_until": 0,
        })
        if result.get("repo") != repo or type(result.get("enabled")) is not bool:
            raise DriverError("project operational identity is invalid")
        _validate_project(result)
        stop = self.stop_intent(repo)
        if not stop and result.get("acknowledged_stop") is not None:
            raise DriverError("acknowledged operational Stop intent is missing")
        if stop and stop["nonce"] != result.get("acknowledged_stop"):
            result.update(enabled=False, wake_pending_until=0, stopped_at=stop["stopped_at"])
        return result

    def save(self, repo: str, data: dict) -> None:
        if data.get("repo") != repo:
            raise DriverError("operational state repository mismatch")
        write_json(self.project_path(repo), data)

    def event(self, repo: str, event_id: str, reason: str) -> bool:
        data = self.project(repo)
        if not data["enabled"]:
            return False
        digest = key(event_id)
        if self.has_event(repo, event_id):
            return False
        self.check_event_capacity(repo, event_id)
        # One atomic project write commits both deduplication and generation.
        data["delivery_keys"] = data["delivery_keys"] + [digest]
        data["events"] = (data["events"] + [{
            "key": digest, "reason": reason[:120], "observed_at": time.time(),
        }])[-256:]
        data["generation"] += 1
        self.save(repo, data)
        return True

    def has_event(self, repo: str, event_id: str) -> bool:
        data = self.project(repo)
        return key(event_id) in data["delivery_keys"]

    def check_event_capacity(self, repo: str, event_id: str) -> None:
        data = self.project(repo)
        if key(event_id) not in data["delivery_keys"] and len(data["delivery_keys"]) >= MAX_EVENT_KEYS:
            raise DriverError("event receipt capacity reached; retire the profile and routes before clearing history")

    def worker(self, worker_id: str) -> dict:
        record = read_json(self.worker_path(worker_id))
        _validate_worker(record)
        if record["id"] != worker_id:
            raise DriverError("operational worker receipt identity mismatch")
        return record

    def workers(self, repo: str | None = None) -> list[dict]:
        result = []
        for path in sorted((self.root / "workers").glob("*.json")):
            record = read_json(path)
            _validate_worker(record)
            if self.worker_path(record["id"]) != path:
                raise DriverError("operational worker receipt identity mismatch")
            if repo is None or record.get("repo") == repo:
                result.append(record)
        return result

    def worker_path(self, worker_id: str) -> Path:
        return self.root / "workers" / f"{key(worker_id)}.json"

    def capacity_path(self, capacity_key: str, slot: int = 0) -> Path:
        # Slot 0 keeps the historical single-lock path so existing reservations,
        # receipts and installed state stay valid; extra sessions get own slots.
        suffix = ".lock" if slot == 0 else f".slot{int(slot)}.lock"
        return self.root / "capacity" / f"{key(capacity_key)}{suffix}"

    def _slot_locked(self, path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
        return False

    def capacity_busy(self, capacity_key: str, max_sessions: int = 1) -> bool:
        """True only when every managed session slot on the subscription is reserved."""
        return all(self._slot_locked(self.capacity_path(capacity_key, slot))
                   for slot in range(max(1, int(max_sessions))))

    def capacity_holders(self, capacity_key: str, max_sessions: int = 1) -> set[str]:
        holders = {self.capacity_holder(capacity_key, slot) for slot in range(max(1, int(max_sessions)))}
        holders.discard(None)
        return holders

    def capacity_holder(self, capacity_key: str, slot: int = 0) -> str | None:
        """Identify the current reservation, never infer liveness from an old PID."""
        path = self.capacity_path(capacity_key, slot)
        if not path.exists():
            return None
        with path.open("r+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = stream.read(128).strip()
                return holder or None
            fcntl.flock(stream, fcntl.LOCK_UN)
        return None
