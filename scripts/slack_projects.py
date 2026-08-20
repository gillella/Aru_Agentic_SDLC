#!/usr/bin/env python3
# line-ceiling: 811
"""Secure multi-project registry for the Slack control-room bridge."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from common import select_governed_projects


SCHEMA_VERSION = 1
DEFAULT_REGISTRY_PATH = Path.home() / ".aru" / "projects.json"
DEFAULT_AUDIT_PATH = Path.home() / ".aru" / "slack-audit.json"
REGISTRY_PATH = DEFAULT_REGISTRY_PATH
AUDIT_PATH = DEFAULT_AUDIT_PATH
PROJECT_ID_RE = re.compile(r"^proj_[A-Za-z0-9_-]{3,64}$")
SECRET_RE = re.compile(r"(?:xox[baprs]-|xapp-|Bearer\s+)\S+", re.IGNORECASE)
IDENTITY_TIMEOUT_SECONDS = 15


class RegistryError(RuntimeError):
    """The registry is unavailable, invalid, or cannot satisfy a request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_directory(path: Path) -> None:  # noqa: C901, PLR0912
    if path.is_symlink():
        raise RegistryError(f"unsafe registry directory: {path}")
    if path.exists():
        if not path.is_dir():
            raise RegistryError(f"unsafe registry directory: {path}")
        info = path.stat()
        if info.st_uid != os.getuid():
            raise RegistryError(f"registry directory is not owned by the current user: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise RegistryError(f"registry directory is writable by another user: {path}")
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
                    raise RegistryError(f"registry directory changed while securing it: {path}")
                if stat.S_IMODE(opened.st_mode) & 0o022:
                    raise RegistryError(f"registry directory is writable by another user: {path}")
                os.fchmod(descriptor, 0o700)
            except OSError as exc:
                raise RegistryError(f"cannot secure registry directory {path}: {exc}") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return
    path.mkdir(parents=True, mode=0o700)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
            raise RegistryError(f"unsafe registry directory after creation: {path}")
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise RegistryError(f"cannot secure registry directory {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RegistryError(f"not a regular file: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RegistryError(f"file must be private (0600): {path}")


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
        raise RegistryError(f"cannot open lock file {lock_path}: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_unlocked(path: Path, default: Any = None) -> Any:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise RegistryError(f"file does not exist: {path}")
    _private_file(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc


def _write_unlocked(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    if path.exists():
        _private_file(path)
    temp_name: Optional[str] = None
    try:
        encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if SECRET_RE.search(encoded):
            raise RegistryError("refusing to persist credential-shaped data")
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
        raise RegistryError(f"cannot write {path}: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def read_secure_json(path: Path, default: Any = None) -> Any:
    with _file_lock(path):
        return _read_unlocked(path, default)


def mutate_secure_json(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    """Lock, validate, update, and atomically replace one private JSON file."""
    with _file_lock(path):
        current = _read_unlocked(path, default)
        updated = updater(copy.deepcopy(current))
        _write_unlocked(path, updated)
        return updated


secure_read_json = read_secure_json
secure_mutate_json = mutate_secure_json


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    github_repo_id: str
    github_repo_database_id: Optional[int]
    project_v2_id: str
    repo_slug: str
    local_path: str
    slack_team_id: str
    slack_channel_id: str
    lifecycle: str
    created_at: str
    updated_at: str
    updated_by: str
    closed_at: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ProjectRecord":
        try:
            record = cls(**value)
        except (TypeError, KeyError) as exc:
            raise RegistryError(f"invalid project record: {exc}") from exc
        record.validate()
        return record

    def validate(self) -> None:
        if not PROJECT_ID_RE.fullmatch(self.project_id):
            raise RegistryError(f"invalid project_id: {self.project_id}")
        required = {
            "github_repo_id": self.github_repo_id,
            "project_v2_id": self.project_v2_id,
            "repo_slug": self.repo_slug,
            "local_path": self.local_path,
            "slack_team_id": self.slack_team_id,
            "slack_channel_id": self.slack_channel_id,
            "updated_by": self.updated_by,
        }
        for name, value in required.items():
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(f"invalid {name}")
        if not self.slack_team_id.startswith("T"):
            raise RegistryError("slack_team_id must start with T")
        if not self.slack_channel_id.startswith(("C", "G")):
            raise RegistryError("slack_channel_id must start with C or G")
        if not Path(self.local_path).is_absolute():
            raise RegistryError("local_path must be absolute")
        if self.lifecycle not in {"active", "closed"}:
            raise RegistryError(f"invalid lifecycle: {self.lifecycle}")

    @property
    def healthy(self) -> bool:
        return Path(self.local_path).is_dir()

    def public_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["runtime_health"] = "healthy" if self.healthy else "degraded_unreachable"
        return value


IdentityProvider = Callable[[Path], Dict[str, Any]]


def _bounded_json(command: List[str], cwd: Optional[str] = None) -> Any:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            timeout=IDENTITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RegistryError(
            f"identity command timed out after {IDENTITY_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise RegistryError(f"identity command failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        raise RegistryError(result.stderr.strip() or "identity command returned no data")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"identity command returned invalid JSON: {exc}") from exc


def _discover_governed_project(repo_slug: str) -> Dict[str, Any]:
    owner, repo_name = repo_slug.split("/", 1)
    query = """
    query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        projectsV2(first:100) {
          nodes {
            id number title
            owner {
              ... on User { login }
              ... on Organization { login }
            }
            repositories(first:100) { nodes { nameWithOwner } }
          }
        }
      }
    }
    """
    response = _bounded_json([
        "gh", "api", "graphql", "-f", f"query={query}",
        "-F", f"owner={owner}", "-F", f"repo={repo_name}",
    ])
    try:
        available = response["data"]["repository"]["projectsV2"]["nodes"]
    except (KeyError, TypeError) as exc:
        raise RegistryError("cannot query governed ProjectV2 boards") from exc
    projects = select_governed_projects(available, repo_slug)
    if len(projects) != 1 or not projects[0].get("id"):
        raise RegistryError("cannot resolve one governed ProjectV2 board")
    return projects[0]


def discover_checkout_identity(local_path: Path) -> Dict[str, Any]:
    if not local_path.is_dir():
        raise RegistryError(f"checkout is unavailable: {local_path}")
    try:
        repo = _bounded_json(
            ["gh", "repo", "view", "--json", "id,nameWithOwner"],
            cwd=str(local_path),
        )
        repo_node_id = repo["id"]
        repo_slug = repo["nameWithOwner"]
        if (not isinstance(repo_node_id, str) or not repo_node_id.strip()
                or not isinstance(repo_slug, str) or not repo_slug.strip()):
            raise RegistryError("repository identity is missing or invalid")
        rest_repo = _bounded_json(
            ["gh", "api", f"repos/{repo_slug}"], cwd=str(local_path),
        )
        rest_node_id = rest_repo["node_id"]
        rest_slug = rest_repo["full_name"]
        if (not isinstance(rest_node_id, str) or not rest_node_id.strip()
                or not isinstance(rest_slug, str) or not rest_slug.strip()):
            raise RegistryError("repository identity is missing or invalid")
        if rest_node_id != repo_node_id or rest_slug != repo_slug:
            raise RegistryError("repository identity APIs returned mismatched data")
        database_id = rest_repo["id"]
        if (isinstance(database_id, bool) or not isinstance(database_id, int)
                or database_id <= 0):
            raise RegistryError("repository database id is missing or invalid")
        project = _discover_governed_project(repo["nameWithOwner"])
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot verify GitHub identity for {local_path}: {exc}") from exc
    return {
        "github_repo_id": repo_node_id,
        "github_repo_database_id": database_id,
        "project_v2_id": str(project["id"]),
        "repo_slug": repo_slug,
        "local_path": str(local_path.resolve()),
    }


class ProjectRegistry:
    def __init__(
        self,
        path: Path = DEFAULT_REGISTRY_PATH,
        audit_path: Path = DEFAULT_AUDIT_PATH,
        identity_provider: IdentityProvider = discover_checkout_identity,
    ) -> None:
        self.path = Path(path)
        self.audit_path = Path(audit_path)
        self.identity_provider = identity_provider

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "projects": {}, "migrations": {}}

    @staticmethod
    def _validate_document(document: Any) -> Dict[str, Any]:
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise RegistryError("unsupported or missing registry schema_version")
        if not isinstance(document.get("projects"), dict) or not isinstance(document.get("migrations"), dict):
            raise RegistryError("registry projects and migrations must be objects")
        records: Dict[str, ProjectRecord] = {}
        bindings: set[tuple[str, str]] = set()
        for project_id, value in document["projects"].items():
            record = ProjectRecord.from_dict(value)
            if record.project_id != project_id:
                raise RegistryError(f"project key mismatch: {project_id}")
            binding = (record.slack_team_id, record.slack_channel_id)
            if binding in bindings:
                raise RegistryError(f"duplicate Slack binding: {binding[0]}:{binding[1]}")
            bindings.add(binding)
            records[project_id] = record
        return {"document": document, "records": records}

    def _read(self) -> Dict[str, Any]:
        return self._validate_document(read_secure_json(self.path, self._empty()))

    def list(self, include_closed: bool = True) -> List[ProjectRecord]:
        records = self._read()["records"].values()
        return sorted(
            (record for record in records if include_closed or record.lifecycle == "active"),
            key=lambda record: record.project_id,
        )

    def get(self, project_id: str, active_only: bool = True) -> ProjectRecord:
        record = self._read()["records"].get(project_id)
        if record is None or (active_only and record.lifecycle != "active"):
            raise RegistryError(f"unknown or closed project_id: {project_id}")
        return record

    def find_by_checkout(self, local_path: Path, active_only: bool = True) -> ProjectRecord:
        """Resolve the registry binding for a checked-out directory.

        Agents (Cursor, Antigravity, Claude, Codex) share one Slack bot and
        must not get per-agent Slack users or tokens; this lets any factory
        agent resolve the project binding from the repo it is working in.
        An unknown or ambiguous checkout fails closed so a missing binding is
        loud instead of silently unrouted.
        """
        wanted = Path(local_path).expanduser().resolve()
        matches = []
        for record in self.list(include_closed=not active_only):
            if active_only and record.lifecycle != "active":
                continue
            candidate = Path(record.local_path).expanduser().resolve()
            if candidate == wanted:
                matches.append(record)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RegistryError(
                f"no project binding for checkout {wanted}. Bind it once with: "
                "python3 scripts/slack_projects.py migrate --local-path <repo> --operator <you>"
            )
        raise RegistryError(f"ambiguous project binding for {wanted}")

    def resolve(self, team_id: str, channel_id: str) -> ProjectRecord:
        matches = [
            record
            for record in self._read()["records"].values()
            if record.lifecycle == "active"
            and record.slack_team_id == team_id
            and record.slack_channel_id == channel_id
        ]
        if len(matches) != 1:
            raise RegistryError(f"no unique active route for {team_id}:{channel_id}")
        return matches[0]

    def _mutate(self, updater: Callable[[Dict[str, Any]], Any]) -> Any:
        result: Dict[str, Any] = {}

        def apply(document: Dict[str, Any]) -> Dict[str, Any]:
            self._validate_document(document)
            result["value"] = updater(document)
            self._validate_document(document)
            return document

        mutate_secure_json(self.path, self._empty(), apply)
        return result.get("value")

    def create(
        self,
        local_path: Path,
        team_id: str,
        channel_id: str,
        operator: str,
        project_id: Optional[str] = None,
    ) -> ProjectRecord:
        identity = self.identity_provider(Path(local_path))
        identifier = project_id or f"proj_{uuid.uuid4().hex}"
        timestamp = _now()
        record = ProjectRecord(
            project_id=identifier,
            github_repo_id=str(identity["github_repo_id"]),
            github_repo_database_id=identity.get("github_repo_database_id"),
            project_v2_id=str(identity["project_v2_id"]),
            repo_slug=str(identity["repo_slug"]),
            local_path=str(Path(identity["local_path"]).resolve()),
            slack_team_id=team_id,
            slack_channel_id=channel_id,
            lifecycle="active",
            created_at=timestamp,
            updated_at=timestamp,
            updated_by=operator,
            closed_at=None,
        )
        record.validate()

        def add(document: Dict[str, Any]) -> ProjectRecord:
            records = [ProjectRecord.from_dict(item) for item in document["projects"].values()]
            if identifier in document["projects"]:
                raise RegistryError(f"project_id already exists: {identifier}")
            if any(
                item.slack_team_id == team_id and item.slack_channel_id == channel_id
                for item in records
            ):
                raise RegistryError(f"Slack binding is reserved: {team_id}:{channel_id}")
            if any(
                item.lifecycle == "active"
                and item.github_repo_id == record.github_repo_id
                and item.project_v2_id == record.project_v2_id
                for item in records
            ):
                raise RegistryError("repository/project already has an active binding")
            document["projects"][identifier] = asdict(record)
            return record

        created = self._mutate(add)
        self.audit("create", operator, created.project_id)
        return created

    def close(self, project_id: str, operator: str) -> ProjectRecord:
        changed = {"value": False}

        def apply(document: Dict[str, Any]) -> ProjectRecord:
            existing = document["projects"].get(project_id)
            if existing is None:
                raise RegistryError(f"unknown project_id: {project_id}")
            current = ProjectRecord.from_dict(existing)
            if current.lifecycle == "closed":
                return current
            timestamp = _now()
            updated = replace(
                current, lifecycle="closed", closed_at=timestamp,
                updated_at=timestamp, updated_by=operator,
            )
            document["projects"][project_id] = asdict(updated)
            changed["value"] = True
            return updated

        closed = self._mutate(apply)
        if changed["value"]:
            self.audit("close", operator, project_id)
        return closed

    def recover(
        self,
        project_id: str,
        operator: str,
        local_path: Optional[Path] = None,
        repo_slug: Optional[str] = None,
    ) -> ProjectRecord:
        current = self.get(project_id, active_only=False)
        candidate_path = Path(local_path) if local_path else Path(current.local_path)
        identity = self.identity_provider(candidate_path)
        if str(identity["github_repo_id"]) != current.github_repo_id:
            raise RegistryError("recovery checkout has a different GitHub repository identity")
        if str(identity["project_v2_id"]) != current.project_v2_id:
            raise RegistryError("recovery checkout has a different ProjectV2 identity")
        discovered_slug = str(identity["repo_slug"])
        if repo_slug and repo_slug != discovered_slug:
            raise RegistryError("requested repo slug does not match the verified checkout")

        def apply(document: Dict[str, Any]) -> ProjectRecord:
            latest = ProjectRecord.from_dict(document["projects"].get(project_id, {}))
            if latest.github_repo_id != current.github_repo_id or latest.project_v2_id != current.project_v2_id:
                raise RegistryError("project identity changed during recovery")
            updated = replace(
                latest,
                repo_slug=discovered_slug,
                local_path=str(Path(identity["local_path"]).resolve()),
                updated_at=_now(),
                updated_by=operator,
            )
            document["projects"][project_id] = asdict(updated)
            return updated

        recovered = self._mutate(apply)
        self.audit("recover", operator, project_id)
        return recovered

    def verify(self, project_id: str) -> Dict[str, Any]:
        record = self.get(project_id, active_only=False)
        result = record.public_dict()
        result["identity_verified"] = False
        if record.healthy:
            try:
                identity = self.identity_provider(Path(record.local_path))
                result["identity_verified"] = (
                    str(identity["github_repo_id"]) == record.github_repo_id
                    and str(identity["project_v2_id"]) == record.project_v2_id
                )
            except RegistryError as exc:
                result["verification_error"] = str(exc)
        return result

    def audit(
        self,
        action: str,
        operator: str,
        project_id: Optional[str] = None,
        detail: Optional[str] = None,
        throttle_key: Optional[str] = None,
        throttle_seconds: int = 60,
    ) -> bool:
        timestamp = datetime.now(timezone.utc).timestamp()
        appended = {"value": False}

        def add(document: Any) -> Dict[str, Any]:
            if not isinstance(document, dict) or not isinstance(document.get("events"), list):
                raise RegistryError("invalid audit log")
            if throttle_key:
                recent = any(
                    event.get("throttle_key") == throttle_key
                    and timestamp - float(event.get("epoch", 0)) < throttle_seconds
                    for event in document["events"]
                    if isinstance(event, dict)
                )
                if recent:
                    return document
            event = {"action": action, "operator": operator, "at": _now(), "epoch": timestamp}
            if project_id:
                event["project_id"] = project_id
            if detail:
                event["detail"] = detail
            if throttle_key:
                event["throttle_key"] = throttle_key
            document["events"].append(event)
            document["events"] = document["events"][-1000:]
            appended["value"] = True
            return document

        mutate_secure_json(self.audit_path, {"events": []}, add)
        return appended["value"]

    def migrate_legacy(
        self,
        values: Dict[str, str],
        local_path: Path,
        operator: str,
    ) -> Optional[ProjectRecord]:
        team_id = values.get("SLACK_TEAM_ID", "")
        channel_id = values.get("SLACK_CHANNEL_ID", "")
        if not team_id or not channel_id:
            raise RegistryError("legacy Slack team/channel binding is missing")
        identity = self.identity_provider(local_path)
        result: Dict[str, Optional[ProjectRecord]] = {"record": None}
        performed = {"value": False}

        def apply(document: Dict[str, Any]) -> Dict[str, Any]:
            self._validate_document(document)
            marker = document["migrations"].get("legacy_singleton_v1")
            if marker:
                project_id = marker.get("project_id") if isinstance(marker, dict) else ""
                if project_id in document["projects"]:
                    result["record"] = ProjectRecord.from_dict(document["projects"][project_id])
                    return document
                raise RegistryError("legacy migration evidence is corrupt")
            if any(
                item.get("slack_team_id") == team_id and item.get("slack_channel_id") == channel_id
                for item in document["projects"].values()
            ):
                raise RegistryError("legacy channel is bound without migration evidence")
            if any(
                item.get("lifecycle") == "active"
                and str(item.get("github_repo_id")) == str(identity["github_repo_id"])
                and str(item.get("project_v2_id")) == str(identity["project_v2_id"])
                for item in document["projects"].values()
            ):
                raise RegistryError("repository/project already has an active binding")
            timestamp = _now()
            project_id = f"proj_{uuid.uuid4().hex}"
            record = ProjectRecord(
                project_id=project_id,
                github_repo_id=str(identity["github_repo_id"]),
                github_repo_database_id=identity.get("github_repo_database_id"),
                project_v2_id=str(identity["project_v2_id"]),
                repo_slug=str(identity["repo_slug"]),
                local_path=str(Path(identity["local_path"]).resolve()),
                slack_team_id=team_id,
                slack_channel_id=channel_id,
                lifecycle="active",
                created_at=timestamp,
                updated_at=timestamp,
                updated_by=operator,
                closed_at=None,
            )
            record.validate()
            document["projects"][project_id] = asdict(record)
            document["migrations"]["legacy_singleton_v1"] = {
                "project_id": project_id, "at": timestamp, "operator": operator,
            }
            result["record"] = record
            performed["value"] = True
            return document

        mutate_secure_json(self.path, self._empty(), apply)
        if performed["value"] and result["record"]:
            self.audit("migrate", operator, result["record"].project_id)
        return result["record"]


def load_registry(path: Path = REGISTRY_PATH) -> Dict[str, Any]:
    registry = ProjectRegistry(path)
    return {
        "version": SCHEMA_VERSION,
        "projects": [record.public_dict() for record in registry.list(include_closed=True)],
    }


def project_by_id(project_id: str, path: Path = REGISTRY_PATH, active_only: bool = True) -> Dict[str, Any]:
    return ProjectRegistry(path).get(project_id, active_only).public_dict()


def resolve_project(team_id: str, channel_id: str, path: Path = REGISTRY_PATH) -> Dict[str, Any]:
    return ProjectRegistry(path).resolve(team_id, channel_id).public_dict()


def verify_project(project_id: str, path: Path = REGISTRY_PATH) -> Dict[str, Any]:
    checked = ProjectRegistry(path).verify(project_id)
    health = checked.pop("runtime_health")
    return {"project": checked, "health": health}


def audit_event(
    action: str,
    detail: str,
    path: Path = AUDIT_PATH,
    throttle_key: str = "",
    throttle_seconds: int = 60,
) -> bool:
    return ProjectRegistry(DEFAULT_REGISTRY_PATH, path).audit(
        action, "system", detail=SECRET_RE.sub("[redacted]", detail),
        throttle_key=throttle_key, throttle_seconds=throttle_seconds,
    )


def record_seen_event(
    project_id: str,
    team_id: str,
    channel_id: str,
    event_id: str,
    path: Path,
) -> bool:
    key = "|".join((project_id, team_id, channel_id, event_id))
    already = {"value": False}

    def update(document: Any) -> Dict[str, Any]:
        if not isinstance(document, dict) or not isinstance(document.get("ids"), dict):
            raise RegistryError("invalid seen-event store")
        if key in document["ids"]:
            already["value"] = True
            return document
        document["ids"][key] = _now()
        document["ids"] = dict(list(document["ids"].items())[-2000:])
        return document

    mutate_secure_json(path, {"ids": {}}, update)
    return already["value"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-file", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--local-path", type=Path, required=True)
    create.add_argument("--team-id", required=True)
    create.add_argument("--channel-id", required=True)
    create.add_argument("--operator", required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--team-id", required=True)
    resolve.add_argument("--channel-id", required=True)
    subparsers.add_parser("list").add_argument("--active-only", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-id", required=True)
    close = subparsers.add_parser("close")
    close.add_argument("--project-id", required=True)
    close.add_argument("--operator", required=True)
    recover = subparsers.add_parser("recover")
    recover.add_argument("--project-id", required=True)
    recover.add_argument("--operator", required=True)
    recover.add_argument("--local-path", type=Path)
    recover.add_argument("--repo-slug")
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--local-path", type=Path, required=True)
    migrate.add_argument("--operator", required=True)
    migrate.add_argument("--env-file", type=Path)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    registry = ProjectRegistry(args.registry_file, args.audit_file)
    try:
        if args.command == "create":
            value: Any = registry.create(
                args.local_path, args.team_id, args.channel_id, args.operator
            ).public_dict()
        elif args.command == "resolve":
            value = registry.resolve(args.team_id, args.channel_id).public_dict()
        elif args.command == "list":
            value = [record.public_dict() for record in registry.list(not args.active_only)]
        elif args.command == "verify":
            value = registry.verify(args.project_id)
        elif args.command == "close":
            value = registry.close(args.project_id, args.operator).public_dict()
        elif args.command == "recover":
            value = registry.recover(
                args.project_id, args.operator, args.local_path, args.repo_slug
            ).public_dict()
        elif args.command == "migrate":
            from slack_notify import ENV_PATH, load_slack_env

            record = registry.migrate_legacy(
                load_slack_env(args.env_file or ENV_PATH), args.local_path, args.operator,
            )
            value = record.public_dict() if record else None
        else:
            raise RegistryError(f"unsupported command: {args.command}")
    except RegistryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, "result": value}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
