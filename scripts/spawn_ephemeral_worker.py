#!/usr/bin/env python3
"""Launch one governed, task-scoped CLI worker for a pull request.

The GitHub Project Board and the normal Aru lifecycle helpers remain
authoritative.  This command only supplies short-lived execution capacity for
an already-known review or author-feedback task.  It never runs the picker and
never starts a persistent fleet loop.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from create_pr import MODEL_FAMILIES
from doctor_local_agent_integrations import repo_slug_from_remote
from run_fleet import (
    RunnerConfig,
    build_agent_argv,
    normalize_timing,
    safe_identity,
    validate_repo,
)


REGISTRY_VERSION = 1
MAX_EPHEMERAL_WORKERS = 2
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3_600.0
TASK_REVIEW = "code-review"
TASK_FEEDBACK = "address-pr-feedback"
TASKS = (TASK_REVIEW, TASK_FEEDBACK)
ADAPTER_FAMILIES = {"codex": "openai", "claude": "anthropic"}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
EPHEMERAL_PATH_RE = re.compile(r"^ephemeral-(review|feedback)-[0-9]+$")


class LauncherError(RuntimeError):
    """A fail-closed validation or lifecycle error."""


class RegistryFull(LauncherError):
    """The global ephemeral-worker ceiling has been reached."""


@dataclass(frozen=True)
class PRMetadata:
    number: int
    state: str
    head_name: str
    head_sha: str
    author_agent: str
    author_family: str
    reviewer_agents: tuple[str, ...]
    cross_repository: bool = False


@dataclass(frozen=True)
class LauncherConfig:
    repo: Path
    aru_home: Path
    pr: int
    skill: str
    parent_agent: str
    parent_family: str
    worker_agent: str
    worker_family: str
    adapter: str = "auto"
    adapter_command_json: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    state_dir: Path | None = None


@dataclass(frozen=True)
class WorkerResult:
    returncode: int
    timed_out: bool = False


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_registry_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "aru-factory"


def authority_environment() -> dict[str, str]:
    """Preserve credentials while removing ambient repository routing."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"GH_REPO", "GH_HOST"}
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def run_authority_command(
    argv: Sequence[str], cwd: Path
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=authority_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return 127, "", type(exc).__name__
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WorkerRegistry:
    """Machine-global process registry with a locked two-worker ceiling."""

    def __init__(
        self,
        directory: Path,
        *,
        is_alive: Callable[[int], bool] = process_is_alive,
    ) -> None:
        self.directory = directory
        self.path = directory / "ephemeral-workers.json"
        self.lock_path = directory / "ephemeral-workers.lock"
        self.is_alive = is_alive

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LauncherError("ephemeral worker registry is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("version") != REGISTRY_VERSION:
            raise LauncherError("ephemeral worker registry has an unsupported format")
        workers = payload.get("workers")
        if not isinstance(workers, list) or not all(isinstance(item, dict) for item in workers):
            raise LauncherError("ephemeral worker registry has invalid worker entries")
        for worker in workers:
            if not isinstance(worker.get("pid"), int) or not isinstance(worker.get("token"), str):
                raise LauncherError("ephemeral worker registry has an invalid process entry")
        return workers

    def _write(self, workers: list[dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        payload = json.dumps(
            {"version": REGISTRY_VERSION, "workers": workers},
            indent=2,
            sort_keys=True,
        ) + "\n"
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def register(self, record: dict[str, Any]) -> str:
        token = f"{record['pid']}-{time.time_ns()}"
        with self._locked():
            active = [worker for worker in self._read() if self.is_alive(worker["pid"])]
            worker_agent = record.get("worker_agent")
            if any(worker.get("worker_agent") == worker_agent for worker in active):
                raise RegistryFull(
                    f"ephemeral worker identity '{worker_agent}' is already active"
                )
            if len(active) >= MAX_EPHEMERAL_WORKERS:
                raise RegistryFull(
                    f"global ephemeral worker limit ({MAX_EPHEMERAL_WORKERS}) reached"
                )
            active.append({**record, "token": token})
            self._write(active)
        return token

    def unregister(self, token: str) -> None:
        with self._locked():
            workers = [worker for worker in self._read() if worker.get("token") != token]
            self._write(workers)


def _labels_with_prefix(labels: Sequence[Any], prefix: str) -> tuple[str, ...]:
    values = []
    for label in labels:
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        if name.startswith(prefix) and name[len(prefix):]:
            values.append(name[len(prefix):])
    return tuple(sorted(set(values)))


def repository_slug(repo: Path) -> str:
    code, remote, _ = run_authority_command(
        ["git", "remote", "get-url", "origin"], repo
    )
    slug = repo_slug_from_remote(remote if code == 0 else None)
    if not slug:
        raise LauncherError("origin must identify a GitHub owner/repository")
    return slug


def load_pr_metadata(repo: Path, pr: int) -> PRMetadata:
    slug = repository_slug(repo)
    fields = "number,state,headRefName,headRefOid,isCrossRepository,labels"
    code, stdout, _ = run_authority_command(
        ["gh", "pr", "view", str(pr), "--repo", slug, "--json", fields],
        repo,
    )
    if code != 0:
        raise LauncherError(f"could not read PR #{pr} through the configured gh identity")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LauncherError(f"PR #{pr} metadata was not valid JSON") from exc

    labels = payload.get("labels") or []
    authors = _labels_with_prefix(labels, "author:")
    families = _labels_with_prefix(labels, "family:")
    if len(authors) != 1 or len(families) != 1:
        raise LauncherError(
            f"PR #{pr} must have exactly one author:<id> and family:<family> stamp"
        )
    head_sha = str(payload.get("headRefOid") or "")
    head_name = str(payload.get("headRefName") or "")
    if not SHA_RE.fullmatch(head_sha) or not head_name or "\n" in head_name:
        raise LauncherError(f"PR #{pr} has invalid head metadata")
    return PRMetadata(
        number=int(payload.get("number") or pr),
        state=str(payload.get("state") or "").upper(),
        head_name=head_name,
        head_sha=head_sha.lower(),
        author_agent=authors[0],
        author_family=families[0].lower(),
        reviewer_agents=_labels_with_prefix(labels, "reviewer:"),
        cross_repository=bool(payload.get("isCrossRepository")),
    )


def validate_task(config: LauncherConfig, metadata: PRMetadata) -> None:
    if os.environ.get("ARU_CAN_SPAWN", "1") != "1":
        raise LauncherError("recursive ephemeral spawning is disabled (ARU_CAN_SPAWN != 1)")
    if metadata.state != "OPEN":
        raise LauncherError(f"PR #{metadata.number} is not open")
    if config.adapter_command_json:
        raise LauncherError(
            "custom adapter commands have no trusted model-family attestation"
        )
    adapter = config.adapter
    if adapter == "auto":
        adapter = next(
            (
                name
                for name, family in ADAPTER_FAMILIES.items()
                if family == config.worker_family
            ),
            "",
        )
    if not adapter or ADAPTER_FAMILIES.get(adapter) != config.worker_family:
        raise LauncherError(
            f"adapter '{config.adapter}' does not attest worker family "
            f"'{config.worker_family}'"
        )
    if config.skill == TASK_REVIEW:
        if config.worker_agent == config.parent_agent:
            raise LauncherError("peer review requires a worker identity distinct from the parent")
        if config.worker_agent == metadata.author_agent:
            raise LauncherError("an ephemeral worker may not review its own PR")
        if config.worker_family == config.parent_family:
            raise LauncherError("peer review requires worker_family != parent_family")
        if config.worker_family == metadata.author_family:
            raise LauncherError("peer review requires a model family distinct from the PR author")
    elif config.skill == TASK_FEEDBACK:
        if config.worker_agent != metadata.author_agent:
            raise LauncherError(
                f"feedback worker must use stamped author identity '{metadata.author_agent}'"
            )
        if config.worker_family != metadata.author_family:
            raise LauncherError(
                f"feedback worker must use stamped author family '{metadata.author_family}'"
            )
        if metadata.cross_repository:
            raise LauncherError("cross-repository feedback pushes are not supported")
    else:
        raise LauncherError(f"unsupported ephemeral skill '{config.skill}'")


def _git(repo: Path, argv: list[str], purpose: str) -> str:
    code, stdout, _ = run_authority_command(["git", *argv], repo)
    if code != 0:
        raise LauncherError(f"git failed while {purpose}")
    return stdout


def prepare_worktree(
    repo: Path,
    metadata: PRMetadata,
    skill: str,
    pid: int,
) -> tuple[Path, str]:
    path, branch = ephemeral_worktree_spec(repo, metadata, skill, pid)
    worktree_root = path.parent
    worktree_root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise LauncherError(f"ephemeral worktree already exists: {path.name}")

    _git(repo, ["fetch", "--no-tags", "origin", f"pull/{metadata.number}/head"], "fetching PR head")
    fetched_head = _git(repo, ["rev-parse", "FETCH_HEAD"], "verifying fetched PR head")
    if fetched_head.strip().lower() != metadata.head_sha:
        raise LauncherError(f"PR #{metadata.number} head moved during ephemeral setup")
    object_type = _git(
        repo,
        ["--no-replace-objects", "cat-file", "-t", metadata.head_sha],
        "verifying PR commit object",
    )
    if object_type.strip() != "commit":
        raise LauncherError(f"PR #{metadata.number} head is not a commit")

    if skill == TASK_REVIEW:
        _git(
            repo,
            ["--no-replace-objects", "worktree", "add", "--detach", str(path), metadata.head_sha],
            "creating review worktree",
        )
    else:
        _git(repo, ["check-ref-format", "--branch", branch], "validating temporary branch")
        _git(
            repo,
            ["--no-replace-objects", "worktree", "add", "-b", branch, str(path), metadata.head_sha],
            "creating feedback worktree",
        )
    return path, branch


def ephemeral_worktree_spec(
    repo: Path,
    metadata: PRMetadata,
    skill: str,
    pid: int,
) -> tuple[Path, str]:
    task = "review" if skill == TASK_REVIEW else "feedback"
    path = repo / ".worktrees" / f"ephemeral-{task}-{pid}"
    branch = (
        f"ephemeral/feedback-pr-{metadata.number}-{pid}"
        if skill == TASK_FEEDBACK
        else ""
    )
    return path, branch


def _owned_worktree(repo: Path, path: Path) -> bool:
    expected_parent = (repo / ".worktrees").resolve()
    candidate = path.resolve()
    return candidate.parent == expected_parent and bool(EPHEMERAL_PATH_RE.fullmatch(candidate.name))


def cleanup_worktree(repo: Path, path: Path | None, branch: str = "") -> bool:
    if path is None:
        return True
    if not _owned_worktree(repo, path):
        print("[ERROR] Refusing cleanup outside the owned ephemeral worktree path.", file=sys.stderr)
        return False

    if path.exists():
        status_code, status, _ = run_authority_command(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], path
        )
        if status_code != 0 or status:
            print(
                f"[ERROR] Ephemeral worktree {path.name} contains modified, "
                "untracked, or unreadable material; retained for inspection.",
                file=sys.stderr,
            )
            return False

    code, _, _ = run_authority_command(
        ["git", "worktree", "remove", str(path)],
        repo,
    )
    if code != 0:
        listing_code, listing, _ = run_authority_command(
            ["git", "worktree", "list", "--porcelain"],
            repo,
        )
        if listing_code != 0 or f"worktree {path}" in listing:
            print(
                f"[ERROR] Ephemeral worktree {path.name} is dirty, locked, or "
                "uncertain; retained for inspection.",
                file=sys.stderr,
            )
            return False
    if branch:
        exists, _, _ = run_authority_command(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            repo,
        )
        if exists != 0:
            return True
        code, _, _ = run_authority_command(
            ["git", "branch", "-D", branch], repo
        )
        if code != 0:
            print(f"[ERROR] Could not delete owned temporary branch {branch}.", file=sys.stderr)
            return False
    return True


def build_task_prompt(
    config: LauncherConfig,
    metadata: PRMetadata,
    worktree: Path,
) -> str:
    common = (
        f"You are {config.worker_agent}, model family {config.worker_family}, running "
        f"as a governed single-shot ephemeral worker in {worktree}. Read AGENTS.md "
        f"and skills/{config.skill}/SKILL.md completely. Work only on PR "
        f"#{metadata.number} at expected head {metadata.head_sha}. Do not run the "
        "picker, claim another item, start a fleet loop, spawn another worker, merge, "
        "or replace any desktop task. The launcher-created worktree is the required "
        "isolated worktree for this specialized workflow. Complete or safely hand off "
        "this one task, then exit. "
    )
    if config.skill == TASK_REVIEW:
        return common + (
            "The review claim is already held by your worker identity. Perform the full "
            "code-review procedure, submit a substantive GitHub review, and finish with "
            "claim_issue.py --complete-review when clean or --release when blocking "
            "findings remain."
        )
    return common + (
        f"You are the stamped PR author. Follow address-pr-feedback for every current "
        f"thread or author-only gate, test the result, commit on the temporary branch, "
        f"and ordinarily push fast-forward with git push origin HEAD:{metadata.head_name}. "
        f"If and only if the unmet gate is rebased, follow the skill and use "
        f"git push --force-with-lease=refs/heads/{metadata.head_name}:{metadata.head_sha} "
        f"origin HEAD:refs/heads/{metadata.head_name}. Resolve/reply through the "
        "governed workflow; do not touch unrelated author worktrees."
    )


def child_environment(config: LauncherConfig) -> dict[str, str]:
    environment = authority_environment()
    environment.update({
        "ARU_CAN_SPAWN": "0",
        "ARU_EPHEMERAL": "1",
        "ARU_AGENT_ID": config.worker_agent,
        "ARU_MODEL_FAMILY": config.worker_family,
        "ARU_EPHEMERAL_PR": str(config.pr),
        "ARU_EPHEMERAL_SKILL": config.skill,
    })
    return environment


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_worker(
    argv: Sequence[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> WorkerResult:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=environment,
            start_new_session=True,
        )
    except OSError:
        return WorkerResult(127)
    try:
        return WorkerResult(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        return WorkerResult(124, timed_out=True)
    except KeyboardInterrupt:
        _stop_process_group(process)
        return WorkerResult(130)


def _claim_command(config: LauncherConfig, *, release: bool = False) -> list[str]:
    command = [
        sys.executable,
        str(config.aru_home / "scripts" / "claim_issue.py"),
        "--pr",
        str(config.pr),
        "--agent",
        config.worker_agent,
    ]
    if release:
        command.append("--release")
    return command


def claim_review(config: LauncherConfig) -> int:
    code, _, _ = run_authority_command(_claim_command(config), config.repo)
    return code


def release_review(config: LauncherConfig) -> int:
    code, _, _ = run_authority_command(
        _claim_command(config, release=True), config.repo
    )
    return code


def execute(config: LauncherConfig) -> int:
    if os.environ.get("ARU_CAN_SPAWN", "1") != "1":
        raise LauncherError("recursive ephemeral spawning is disabled (ARU_CAN_SPAWN != 1)")
    metadata = load_pr_metadata(config.repo, config.pr)
    validate_task(config, metadata)

    registry = WorkerRegistry(config.state_dir or default_registry_dir())
    token = registry.register({
        "pid": os.getpid(),
        "repo": str(config.repo),
        "pr": config.pr,
        "skill": config.skill,
        "parent_agent": config.parent_agent,
        "worker_agent": config.worker_agent,
        "worker_family": config.worker_family,
        "started_at": utc_timestamp(),
    })
    worktree: Path | None = None
    branch = ""
    claim_active = False
    result_code = 1
    cleanup_ok = True
    try:
        if config.skill == TASK_REVIEW:
            claim_code = claim_review(config)
            if claim_code != 0:
                raise LauncherError(f"could not claim review of PR #{config.pr}")
            claim_active = True

        worktree, branch = prepare_worktree(
            config.repo, metadata, config.skill, os.getpid()
        )
        prompt = build_task_prompt(config, metadata, worktree)
        runner_config = RunnerConfig(
            repo=worktree,
            aru_home=config.aru_home,
            agent=config.worker_agent,
            family=config.worker_family,
            adapter=config.adapter,
            adapter_command_json=config.adapter_command_json,
        )
        argv = build_agent_argv(runner_config, prompt)
        if shutil.which(argv[0]) is None:
            raise LauncherError(f"CLI adapter '{argv[0]}' is not installed")

        result = run_worker(argv, worktree, child_environment(config), config.timeout)
        result_code = result.returncode
        if result.timed_out:
            print(
                f"[WARN] Ephemeral worker timed out after {config.timeout:g}s.",
                file=sys.stderr,
            )

        if claim_active:
            if result_code != 0:
                release_review(config)
                claim_active = False
            else:
                try:
                    fresh = load_pr_metadata(config.repo, config.pr)
                except LauncherError:
                    release_review(config)
                    claim_active = False
                    result_code = 1
                else:
                    if config.worker_agent in fresh.reviewer_agents:
                        release_review(config)
                        claim_active = False
                        print(
                            "[ERROR] Worker exited without completing or releasing its review claim.",
                            file=sys.stderr,
                        )
                        result_code = 1
                    else:
                        claim_active = False
    except (LauncherError, ValueError) as exc:
        print(f"[ERROR] Ephemeral worker failed closed: {exc}", file=sys.stderr)
        if claim_active:
            release_review(config)
            claim_active = False
        result_code = 1
    finally:
        if claim_active:
            release_review(config)
            claim_active = False
        cleanup_ok = cleanup_worktree(config.repo, worktree, branch)
        try:
            registry.unregister(token)
        except LauncherError as exc:
            print(f"[ERROR] Could not unregister ephemeral worker: {exc}", file=sys.stderr)
            cleanup_ok = False
    if not cleanup_ok and result_code == 0:
        return 1
    return result_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch exactly one governed ephemeral PR worker."
    )
    parser.add_argument("--repo", required=True, help="Trusted governed repository path")
    parser.add_argument("--pr", type=int, required=True, help="Target pull request number")
    parser.add_argument("--skill", required=True, choices=TASKS)
    parser.add_argument("--parent-agent", required=True)
    parser.add_argument("--parent-family", required=True, choices=MODEL_FAMILIES)
    parser.add_argument("--worker-agent", required=True)
    parser.add_argument("--worker-family", required=True, choices=MODEL_FAMILIES)
    parser.add_argument("--adapter", choices=("auto", "codex", "claude"), default="auto")
    parser.add_argument("--adapter-command-json", default="")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--aru-home", default=str(Path(__file__).resolve().parents[1]))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_shutdown(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        repo = validate_repo(Path(args.repo))
        aru_home = Path(args.aru_home).expanduser().resolve()
        if not (aru_home / "scripts" / "claim_issue.py").is_file():
            raise ValueError("--aru-home does not contain scripts/claim_issue.py")
        parent_agent = safe_identity(args.parent_agent, "parent agent")
        worker_agent = safe_identity(args.worker_agent, "worker agent")
        timeout = normalize_timing(
            args.timeout,
            "--timeout",
            minimum=1.0,
            maximum=MAX_TIMEOUT_SECONDS,
        )
        state_dir = (
            Path(args.state_dir).expanduser().resolve()
            if args.state_dir
            else default_registry_dir()
        )
        config = LauncherConfig(
            repo=repo,
            aru_home=aru_home,
            pr=args.pr,
            skill=args.skill,
            parent_agent=parent_agent,
            parent_family=args.parent_family,
            worker_agent=worker_agent,
            worker_family=args.worker_family,
            adapter=args.adapter,
            adapter_command_json=args.adapter_command_json,
            timeout=timeout,
            state_dir=state_dir,
        )
        return execute(config)
    except KeyboardInterrupt:
        print("[WARN] Ephemeral worker interrupted; cleanup completed.", file=sys.stderr)
        return 130
    except (LauncherError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
