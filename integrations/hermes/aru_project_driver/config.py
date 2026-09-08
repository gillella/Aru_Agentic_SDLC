"""Explicit machine-local configuration. No credentials or shell evaluation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .kernel import RUNNER_PROFILES, runner_profile_for_account


class DriverError(RuntimeError):
    """An action cannot safely proceed."""


def absolute(value: object, name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise DriverError(f"{name} must be an absolute path")
    return Path(value).resolve()


def command(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value or not value[0] or any(
        not isinstance(part, str) or "\x00" in part for part in value
    ):
        raise DriverError(f"{name} must be a nonempty argument array")
    return value


class Config:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise DriverError(f"cannot read Driver config: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise DriverError("Driver config requires version 1")
        self.raw = raw
        self.state_dir = absolute(raw.get("state_dir"), "state_dir")
        self.hermes_home = absolute(raw.get("hermes_home"), "hermes_home")
        expected_state = (self.hermes_home / "state" / "aru_project_driver").resolve()
        if self.state_dir != expected_state:
            raise DriverError("state_dir must be HERMES_HOME/state/aru_project_driver for shared locking")
        binding = expected_state / "binding.json"
        if binding.exists():
            try:
                existing = json.loads(binding.read_text())
            except (OSError, ValueError) as exc:
                raise DriverError("Driver profile binding is unreadable") from exc
            if existing != {"config": str(self.path), "state_dir": str(self.state_dir)}:
                raise DriverError("Hermes profile is bound to another Driver configuration")
        self.hermes_repo = absolute(raw.get("hermes_repo"), "hermes_repo")
        self.kernel_root = absolute(raw.get("kernel_root"), "kernel_root")
        self.projects = raw.get("projects")
        self.lanes = raw.get("lanes")
        if not isinstance(self.projects, dict) or not self.projects:
            raise DriverError("projects must explicitly name at least one repository")
        if not isinstance(self.lanes, dict) or not self.lanes:
            raise DriverError("lanes must explicitly name at least one coding identity")
        for repo, project in self.projects.items():
            self._project(repo, project)
        for identity, lane in self.lanes.items():
            self._lane(identity, lane)
        self._bind()

    def _bind(self) -> None:
        from .state import State, read_json, write_json

        path = self.state_dir / "binding.json"
        expected = {"config": str(self.path), "state_dir": str(self.state_dir)}
        if path.exists():
            if read_json(path) != expected:
                raise DriverError("Hermes profile is bound to another Driver configuration")
            return
        # Establish the binding once, before any entrypoint can touch the journal.
        # Existing bindings need no lock: a child may load config while its parent
        # still holds the coordination lock during launch.
        with State(self.state_dir).lock():
            if read_json(path, expected) != expected:
                raise DriverError("Hermes profile is bound to another Driver configuration")
            if not path.exists():
                write_json(path, expected)

    def _project(self, repo: str, project: dict) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise DriverError("project must be a literal owner/repository")
        if not isinstance(project, dict):
            raise DriverError(f"invalid project configuration: {repo}")
        absolute(project.get("repo_dir"), "repo_dir")
        lanes = project.get("lanes")
        if (not isinstance(lanes, list) or not lanes or any(not isinstance(i, str) for i in lanes)
                or len(lanes) != len(set(lanes))):
            raise DriverError(f"{repo}: lanes must be a nonempty unique list")
        if any(identity not in self.lanes for identity in lanes):
            raise DriverError(f"{repo}: unknown lane")
        for identity in lanes:
            lane = self.lanes[identity]
            if (not isinstance(lane, dict) or not isinstance(lane.get("projects"), list)
                    or repo not in lane["projects"]):
                raise DriverError(f"{repo}: referenced lane must explicitly authorize this project")
        for key, default in (("max_workers", 4), ("max_review_backlog", 4)):
            value = project.get(key, default)
            if type(value) is not int or not 1 <= value <= 32:
                raise DriverError(f"{repo}: {key} must be between 1 and 32")
        self._runner_profile(repo, project.get("runner_profile"))
        if type(project.get("auto_triage", False)) is not bool:
            raise DriverError("auto_triage must explicitly be true or false")
        routes = project.get("webhook_subscriptions", [])
        if not isinstance(routes, list) or any(
            not isinstance(route, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", route) for route in routes
        ):
            raise DriverError("webhook_subscriptions must name native subscription identifiers")
        handoff_to = project.get("handoff_to", [])
        if (not isinstance(handoff_to, list)
                or any(not isinstance(target, str) for target in handoff_to)
                or len(handoff_to) != len(set(handoff_to))
                or any(target not in self.projects for target in handoff_to)
                or repo in handoff_to):
            raise DriverError("handoff_to must name distinct configured projects other than itself")

    def _runner_profile(self, repo: str, declared: object) -> None:
        """Refuse a declaration that is unknown or contradicts the account policy."""
        if declared is None:
            return
        if declared not in RUNNER_PROFILES:
            raise DriverError(f"{repo}: unknown runner_profile: {declared!r}")
        assigned = runner_profile_for_account(repo)
        # Kernel admission derives the profile from the account table alone, so a
        # declaration can only confirm an assignment, never substitute for one.
        if assigned is None:
            raise DriverError(
                f"{repo}: runner_profile {declared} cannot be declared for an account "
                "with no assigned profile"
            )
        if declared != assigned:
            raise DriverError(
                f"{repo}: runner_profile {declared} contradicts the {assigned} "
                "profile assigned to its account"
            )

    def _lane(self, identity: str, lane: dict) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", identity):
            raise DriverError("lane identity must be a kernel-compatible agent id")
        if not isinstance(lane, dict):
            raise DriverError(f"invalid lane: {identity}")
        if not isinstance(lane.get("capacity_key"), str) or not lane["capacity_key"]:
            raise DriverError(f"{identity}: capacity_key identifies the shared subscription")
        if not isinstance(lane.get("family"), str) or not lane["family"]:
            raise DriverError(f"{identity}: family is required")
        argv = command(lane.get("command"), "command")
        if sum(part.count("{prompt}") for part in argv) != 1:
            raise DriverError(f"{identity}: command must contain exactly one {{prompt}}")
        command(lane.get("capacity_command"), "capacity_command")
        command(lane.get("probe_command"), "probe_command")
        timeout = lane.get("execution_timeout_seconds", 3600)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise DriverError(f"{identity}: execution_timeout_seconds must be between 1 and 86400")
        allowed = lane.get("projects")
        if not isinstance(allowed, list) or not allowed or any(
            not isinstance(repo, str) or repo not in self.projects for repo in allowed
        ):
            raise DriverError(f"{identity}: explicitly allowed projects are required")

    def project(self, repo: str) -> dict:
        if repo not in self.projects:
            raise DriverError("repository is not configured for this Driver")
        return self.projects[repo]

    def runner_profile(self, repo: str) -> str:
        """Return the one profile this repository verifies on, or refuse.

        A declaration is optional but, when present, has already been validated
        against the account policy. An account with neither is refused rather
        than defaulted onto another account's runners.
        """
        declared = self.project(repo).get("runner_profile")
        profile = declared or runner_profile_for_account(repo)
        if profile is None:
            raise DriverError(f"{repo}: no runner profile is declared or assigned to its account")
        return profile

    def lane(self, repo: str, identity: str) -> dict:
        project = self.project(repo)
        lane = self.lanes.get(identity)
        if identity not in project["lanes"] or not lane or repo not in lane["projects"]:
            raise DriverError("coding identity is not authorized for this project")
        return lane
