"""Small operational journal. It never stores or assigns issue lifecycle status."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DriverError


def key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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

    def project(self, repo: str) -> dict:
        result = read_json(self.project_path(repo), {
            "repo": repo, "enabled": False, "generation": 0, "events": [],
            "last_fingerprint": None, "wake_pending_until": 0, "cooldown_until": 0,
        })
        if result.get("repo") != repo or type(result.get("enabled")) is not bool:
            raise DriverError("project operational identity is invalid")
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
        if any(event["key"] == digest for event in data["events"]):
            return False
        data["events"] = (data["events"] + [{
            "key": digest, "reason": reason[:120], "observed_at": time.time(),
        }])[-256:]
        data["generation"] += 1
        self.save(repo, data)
        return True

    def workers(self, repo: str | None = None) -> list[dict]:
        result = []
        for path in sorted((self.root / "workers").glob("*.json")):
            record = read_json(path)
            if repo is None or record.get("repo") == repo:
                result.append(record)
        return result

    def worker_path(self, worker_id: str) -> Path:
        return self.root / "workers" / f"{key(worker_id)}.json"

    def capacity_path(self, capacity_key: str) -> Path:
        return self.root / "capacity" / f"{key(capacity_key)}.lock"

    def capacity_busy(self, capacity_key: str) -> bool:
        path = self.capacity_path(capacity_key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
        return False

    def capacity_holder(self, capacity_key: str) -> str | None:
        """Identify the current reservation, never infer liveness from an old PID."""
        path = self.capacity_path(capacity_key)
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
