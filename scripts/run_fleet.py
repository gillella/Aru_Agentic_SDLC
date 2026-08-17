#!/usr/bin/env python3
"""Optional headless worker for one Aru factory CLI agent identity.

The GitHub Project Board remains the queue.  This process only decides when to
start the next finite local-agent session and when to wait.  A child session
claims and executes exactly one governed lifecycle unit through the existing
skills and helpers; when that session exits, this process re-reads GitHub and
starts with fresh context.

This does not control or resume a desktop-app conversation. Loop mode is
intentionally operator-owned: idle, complete, blocked, GitHub
errors, child failures, and credit/rate-limit exhaustion all wait and retry.
Only an explicit stop request or termination signal ends a healthy loop.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


STATE_VERSION = 2
IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RECOVERABLE_PHASES = {
    "waiting",
    "complete_watch",
    "blocked_wait",
    "error_wait",
    "agent_unavailable_wait",
}
MAX_WAIT_SECONDS = 86_400.0
MAX_HELPER_TIMEOUT_SECONDS = 3_600.0
MAX_CHILD_STDERR_CHARS = 65_536


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class IterationResult:
    phase: str
    delay: float
    work_type: str = "idle"
    work_number: int | None = None
    child_returncode: int | None = None


@dataclass
class RunnerConfig:
    repo: Path
    aru_home: Path
    agent: str
    family: str
    adapter: str = "auto"
    adapter_command_json: str = ""
    initial_wait: float = 15.0
    max_wait: float = 900.0
    jitter: float = 0.2
    helper_timeout: float = 120.0
    cooldown_recheck_seconds: float = 300.0
    state_dir: Path | None = None


def classify_child_failure(returncode: int, stderr: str = "") -> str:
    lower = stderr.lower()
    if returncode == 75 or "rate limit" in lower or "429" in lower or "rate_limit" in lower:
        return "rate-limited"
    if returncode == 73 or "credit" in lower or "402" in lower or "billing" in lower or "quota" in lower:
        return "credit-exhausted"
    if returncode == 69 or "unavailable" in lower or "503" in lower or "service unavailable" in lower:
        return "provider-outage"
    return "child-crash"


def post_availability_transition(
    config: RunnerConfig,
    project_id: str,
    event: dict[str, Any],
) -> None:
    """Best-effort project-channel transition; GitHub remains authoritative."""
    if not project_id:
        return
    argv = [
        sys.executable,
        str(config.aru_home / "scripts" / "slack_notify.py"),
        "--agent", config.agent,
        "--family", config.family,
        "--event", "availability",
        "--project-id", project_id,
        "--state", str(event.get("state") or ""),
        "--text", str(event.get("text") or ""),
    ]
    reason = str(event.get("cooldown_reason") or "")
    retry_at = str(event.get("retry_at") or "")
    if reason:
        argv.extend(("--cooldown-reason", reason))
    if retry_at:
        argv.extend(("--retry-at", retry_at))
    work_type = str(event.get("work_type") or "")
    work_number = event.get("work_number")
    if isinstance(work_number, int):
        argv.extend(("--issue" if work_type == "issue" else "--pr", str(work_number)))
    dedupe_key = str(event.get("dedupe_key") or "")
    if dedupe_key:
        argv.extend(("--dedupe-key", dedupe_key))
    result = run_command(argv, config.repo, config.helper_timeout)
    if result.returncode != 0:
        raise RuntimeError("availability transition helper rejected the event")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def default_state_dir(repo: Path) -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    identity = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
    return root / "aru-factory" / identity


def safe_identity(value: str, label: str) -> str:
    candidate = value.strip()
    if not candidate or not IDENTITY_RE.fullmatch(candidate):
        raise ValueError(f"{label} must match {IDENTITY_RE.pattern}")
    return candidate


def run_command(
    argv: Sequence[str], cwd: Path, timeout: float = 120.0,
) -> CommandResult:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        return CommandResult(124, "", "command timed out")
    return CommandResult(process.returncode, stdout, stderr)


def run_agent(
    argv: Sequence[str],
    cwd: Path,
    on_start: Callable[[int], None] | None = None,
) -> CommandResult:
    """Run a signal-isolated child and retain stderr only for classification."""
    try:
        child = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"[run_fleet] agent launch failed: {type(exc).__name__}", file=sys.stderr)
        return CommandResult(127, "", str(exc))
    if on_start is not None:
        on_start(child.pid)
    stderr_tail = [""]

    def drain_stderr() -> None:
        tail = ""
        stream = child.stderr
        if stream is None:
            return
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            sys.stderr.write(chunk)
            sys.stderr.flush()
            tail = (tail + chunk)[-MAX_CHILD_STDERR_CHARS:]
        stderr_tail[0] = tail

    stderr_thread = threading.Thread(
        target=drain_stderr,
        name=f"{configurable_thread_name(argv)}-stderr",
        daemon=True,
    )
    stderr_thread.start()
    returncode = child.wait()
    stderr_thread.join()
    return CommandResult(returncode, "", stderr_tail[0])


def configurable_thread_name(argv: Sequence[str]) -> str:
    """Return a non-sensitive diagnostic name without embedding arguments."""
    executable = Path(argv[0]).name if argv else "agent"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", executable)
    return cleaned[:32] or "agent"


def parse_json_result(result: CommandResult) -> dict[str, Any] | None:
    if not result.stdout.strip():
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def work_identity(work: dict[str, Any]) -> tuple[str, int | None]:
    work_type = str(work.get("type") or "idle")
    raw_number = work.get("issue") if work_type == "issue" else work.get("pr")
    return work_type, raw_number if isinstance(raw_number, int) else None


def state_fingerprint(fleet: dict[str, Any], work: dict[str, Any]) -> str:
    work_type, work_number = work_identity(work)
    stable = {
        "fleet_state": fleet.get("state"),
        "summary": fleet.get("summary"),
        "open_issues": fleet.get("open_issues_count"),
        "open_prs": fleet.get("open_prs_count"),
        "active_claims": fleet.get("active_claims"),
        "work_type": work_type,
        "work_number": work_number,
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def build_prompt(config: RunnerConfig, work: dict[str, Any]) -> str:
    work_type, work_number = work_identity(work)
    subject = work_type if work_number is None else f"{work_type} #{work_number}"
    return (
        f"You are {config.agent}, model family {config.family}, in the trusted "
        f"repository {config.repo}. The durable Aru runner observed eligible "
        f"work ({subject}). Read AGENTS.md and the run-aru-factory skill, then "
        "execute exactly one governed Aru Code next unit. Re-run the canonical "
        "picker with your stable agent id and family and claim through the "
        "framework helpers; the observed item is advisory because another "
        "worker may win the race. Complete or safely hand off that one unit, "
        "then exit this child session. Do not start a second unit and do not "
        "bypass Issue-First, worktrees, tests, CI, independent review, or the "
        "merge helper. Recover existing work for this identity before claiming "
        "anything new."
    )


def build_agent_argv(config: RunnerConfig, prompt: str) -> list[str]:
    repo = str(config.repo)
    if config.adapter_command_json:
        try:
            raw = json.loads(config.adapter_command_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--adapter-command-json must be a JSON argv array") from exc
        if not isinstance(raw, list) or not raw or not all(isinstance(v, str) and v for v in raw):
            raise ValueError("--adapter-command-json must be a non-empty JSON array of strings")
        argv = [token.replace("{repo}", repo).replace("{prompt}", prompt) for token in raw]
        if not any("{prompt}" in token for token in raw):
            argv.append(prompt)
        return argv

    adapter = config.adapter.lower()
    if adapter == "auto":
        adapter = {"openai": "codex", "anthropic": "claude"}.get(config.family.lower(), "")
    if adapter == "codex":
        return ["codex", "exec", "--full-auto", "-C", repo, prompt]
    if adapter == "claude":
        return [
            "claude", "--print", "--permission-mode", "auto",
            "--output-format", "stream-json", "--verbose", prompt,
        ]
    raise ValueError(
        "No built-in adapter for this family; choose codex/claude or pass "
        "--adapter-command-json"
    )


class StateStore:
    """Process metadata only.  GitHub remains the work queue."""

    def __init__(self, directory: Path, agent: str):
        self.directory = directory
        self.path = directory / f"{agent}.json"
        self.stop_path = directory / f"{agent}.stop"
        self.lock_path = directory / f"{agent}.lock"
        self.control_lock_path = directory / f"{agent}.control.lock"

    def _acquire_control_lock(self) -> Any:
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = self.control_lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def acquire_lock(self, *, clear_stop: bool = False) -> Any | None:
        """Hold one process per repository/agent identity until release."""
        control = self._acquire_control_lock()
        try:
            handle = self.lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
            if clear_stop:
                self._clear_stop_unlocked()
            return handle
        finally:
            self.release_lock(control)

    @staticmethod
    def release_lock(handle: Any) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def write(self, payload: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def request_stop(self) -> None:
        control = self._acquire_control_lock()
        try:
            self.stop_path.write_text(timestamp() + "\n", encoding="utf-8")
        finally:
            self.release_lock(control)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def clear_stop(self) -> None:
        control = self._acquire_control_lock()
        try:
            self._clear_stop_unlocked()
        finally:
            self.release_lock(control)

    def _clear_stop_unlocked(self) -> None:
        try:
            self.stop_path.unlink()
        except OSError:
            pass


class FleetRunner:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        command_runner: Callable[[Sequence[str], Path], CommandResult] = run_command,
        agent_runner: Callable[[Sequence[str], Path], int | CommandResult] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = utc_now,
        presence_store: Any | None = None,
        project_id: str | None = None,
        transition_notifier: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.command_runner = command_runner
        self.agent_runner = agent_runner
        self.sleeper = sleeper
        self.random_value = random_value
        self.clock = clock
        directory = config.state_dir or default_state_dir(config.repo)
        self.store = StateStore(directory, config.agent)
        self.presence_store = presence_store
        self.project_id = project_id
        self.transition_notifier = transition_notifier
        self.stop_signal = False
        self.cycle = 0
        self.retry_count = 0
        self.last_fingerprint = ""
        self.last_child_fingerprint = ""
        self.active_cooldown_reason: str | None = None
        self.active_cooldown_id: str | None = None
        if self.presence_store is not None:
            try:
                existing = self.presence_store.get(config.agent)
            except (OSError, RuntimeError, ValueError):
                existing = None
            if existing is not None and existing.availability == "cooling-down":
                self.active_cooldown_reason = existing.cooldown_reason or ""
                self.active_cooldown_id = (
                    f"recovered:{existing.updated_at or existing.cooldown_until or 'unknown'}"
                )

    def _log(self, event: str, **fields: Any) -> None:
        record = {
            "time": timestamp(self.clock()),
            "event": event,
            "agent": self.config.agent,
            "family": self.config.family,
            "cycle": self.cycle,
        }
        record.update(fields)
        try:
            print(json.dumps(record, sort_keys=True), flush=True)
        except (BrokenPipeError, OSError):
            # A rotated or detached log consumer is not the factory's process
            # owner. State polling and explicit stop must keep working.
            pass

    def _write_state(
        self,
        phase: str,
        *,
        fleet_state: str = "unknown",
        delay: float = 0.0,
        terminal_reason: str = "",
        child_pid: int | None = None,
        cooldown_reason: str | None = None,
    ) -> None:
        effective_cooldown_reason = (
            cooldown_reason
            if cooldown_reason is not None
            else self.active_cooldown_reason
        )
        retry_at = ""
        if delay > 0:
            retry_at = timestamp(self.clock() + timedelta(seconds=delay))
        try:
            self.store.write({
                "version": STATE_VERSION,
                "pid": os.getpid(),
                "child_pid": child_pid,
                "agent": self.config.agent,
                "family": self.config.family,
                "repository": str(self.config.repo),
                "phase": phase,
                "cycle": self.cycle,
                "retry_count": self.retry_count,
                "fleet_state": fleet_state,
                "state_fingerprint": self.last_fingerprint,
                "next_retry_at": retry_at,
                "cooldown_reason": effective_cooldown_reason or "",
                "terminal_reason": terminal_reason,
                "updated_at": timestamp(self.clock()),
            })
        except OSError as exc:
            print(
                f"[run_fleet] process metadata unavailable: {type(exc).__name__}",
                file=sys.stderr,
            )
        self._sync_presence(
            phase,
            delay=delay,
            cooldown_reason=effective_cooldown_reason,
        )

    def _notify_transition(self, event: dict[str, Any]) -> None:
        if self.transition_notifier is None:
            return
        try:
            self.transition_notifier(event)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._log("availability_notice_failed", error=type(exc).__name__)

    def _exit_cooldown(self, work: dict[str, Any]) -> None:
        """Re-admit only after a successful fresh GitHub picker response."""
        if self.active_cooldown_reason is None:
            return
        previous_reason = self.active_cooldown_reason
        cooldown_id = self.active_cooldown_id or f"cycle:{self.cycle}:recovered"
        work_type, work_number = work_identity(work)
        self.active_cooldown_reason = None
        self.active_cooldown_id = None
        self._log(
            "cooldown_exit",
            previous_reason=previous_reason or "unknown",
            work_type=work_type,
            work_number=work_number,
        )
        self._notify_transition({
            "state": "returned",
            "text": "agent returned after a fresh GitHub eligibility query",
            "work_type": work_type,
            "work_number": work_number,
            "dedupe_key": f"availability:{cooldown_id}:returned",
        })

    def _sync_presence(self, phase: str, *, delay: float = 0.0, cooldown_reason: str | None = None) -> None:
        """Update project-scoped presence; never affects GitHub claims."""
        if self.presence_store is None:
            return
        try:
            from agent_presence import sync_runner_presence
        except ImportError:
            return
        cooldown = ""
        if delay > 0:
            cooldown = timestamp(self.clock() + timedelta(seconds=delay))
        presence_phase = phase
        if (
            self.active_cooldown_reason is not None
            and phase not in {"stopped", "stopping"}
        ):
            presence_phase = "agent_unavailable_wait"
        sync_runner_presence(
            self.presence_store,
            agent_id=self.config.agent,
            family=self.config.family,
            checkout_path=self.config.repo,
            phase=presence_phase,
            project_id=self.project_id,
            role="fleet-runner",
            workload={
                "cycle": self.cycle,
                "phase": phase,
                "retry_count": self.retry_count,
            },
            cooldown_until=cooldown or None,
            cooldown_reason=cooldown_reason,
            wake_evidence_supported=["github-recovery"],
        )

    def _run_fleet_status(self) -> dict[str, Any]:
        argv = [
            sys.executable,
            str(self.config.aru_home / "scripts" / "fleet_status.py"),
            "--repo-dir", str(self.config.repo), "--json",
        ]
        if self.command_runner is run_command:
            result = run_command(argv, self.config.repo, self.config.helper_timeout)
        else:
            result = self.command_runner(argv, self.config.repo)
        parsed = parse_json_result(result)
        if parsed is None:
            return {
                "state": "error",
                "summary": f"fleet status unavailable (exit {result.returncode})",
            }
        return parsed

    def _run_picker(self) -> dict[str, Any]:
        argv = [
            sys.executable,
            str(self.config.aru_home / "scripts" / "fetch_next_work.py"),
            "--agent", self.config.agent,
            "--family", self.config.family,
            "--json",
        ]
        if self.command_runner is run_command:
            result = run_command(argv, self.config.repo, self.config.helper_timeout)
        else:
            result = self.command_runner(argv, self.config.repo)
        parsed = parse_json_result(result)
        if parsed is None:
            return {
                "work": {
                    "type": "error",
                    "reason": f"picker unavailable (exit {result.returncode})",
                }
            }
        return parsed

    def _observe(self, fleet: dict[str, Any], work: dict[str, Any]) -> float:
        fingerprint = state_fingerprint(fleet, work)
        if fingerprint == self.last_fingerprint:
            self.retry_count += 1
        else:
            self.retry_count = 0
            self.last_fingerprint = fingerprint
        base = min(
            self.config.max_wait,
            self.config.initial_wait * (2 ** min(self.retry_count, 16)),
        )
        spread = base * self.config.jitter
        return max(0.0, min(self.config.max_wait, base - spread + (2 * spread * self.random_value())))

    def _park(
        self,
        phase: str,
        fleet: dict[str, Any],
        work: dict[str, Any],
        cooldown_reason: str | None = None,
    ) -> IterationResult:
        delay = self._observe(fleet, work)
        if phase == "agent_unavailable_wait" and delay > self.config.cooldown_recheck_seconds:
            delay = self.config.cooldown_recheck_seconds
        work_type, work_number = work_identity(work)
        fleet_state = str(fleet.get("state") or "unknown")
        self._write_state(phase, fleet_state=fleet_state, delay=delay, cooldown_reason=cooldown_reason)
        self._log(
            "park",
            phase=phase,
            fleet_state=fleet_state,
            cooldown_reason=cooldown_reason,
            work_type=work_type,
            work_number=work_number,
            retry=self.retry_count,
            wait_seconds=round(delay, 3),
        )
        return IterationResult(phase, delay, work_type, work_number)

    def run_iteration(self) -> IterationResult:
        if self._stop_requested():
            return IterationResult("stopping", 0.0)
        self.cycle += 1
        fleet = self._run_fleet_status()
        fleet_state = str(fleet.get("state") or "error")
        if self._stop_requested():
            return IterationResult("stopping", 0.0)

        if fleet_state == "complete":
            return self._park("complete_watch", fleet, {"type": "idle"})
        if fleet_state == "blocked":
            return self._park("blocked_wait", fleet, {"type": "idle"})
        if fleet_state == "error":
            return self._park("error_wait", fleet, {"type": "error"})

        selection = self._run_picker()
        work = selection.get("work") if isinstance(selection.get("work"), dict) else {"type": "error"}
        work_type, work_number = work_identity(work)
        if self._stop_requested():
            return IterationResult("stopping", 0.0, work_type, work_number)
        if work_type == "idle":
            return self._park("waiting", fleet, work)
        if work_type == "error":
            return self._park("error_wait", fleet, work)

        current_fingerprint = state_fingerprint(fleet, work)
        if current_fingerprint == self.last_child_fingerprint:
            # A nominally successful child that leaves the same unit visible
            # must not create a credit-burning tight loop.  Wait once, then
            # retry with a fresh context; a changed board bypasses the wait.
            self.last_child_fingerprint = ""
            return self._park("waiting", fleet, work)
        self.last_fingerprint = current_fingerprint
        self._write_state("active", fleet_state=fleet_state)
        self._log(
            "child_start",
            work_type=work_type,
            work_number=work_number,
            adapter=self.config.adapter,
        )
        try:
            argv = build_agent_argv(self.config, build_prompt(self.config, work))
        except ValueError:
            self.retry_count += 1
            return self._park("agent_unavailable_wait", fleet, work)

        def record_child(child_pid: int) -> None:
            self._write_state(
                "active", fleet_state=fleet_state, child_pid=child_pid,
            )
            self._log(
                "child_running",
                work_type=work_type,
                work_number=work_number,
                child_pid=child_pid,
            )

        if self._stop_requested():
            return IterationResult("stopping", 0.0, work_type, work_number)
        if self.agent_runner is None:
            child_result: int | CommandResult = run_agent(
                argv, self.config.repo, record_child,
            )
        else:
            child_result = self.agent_runner(argv, self.config.repo)
        if isinstance(child_result, CommandResult):
            child_code = child_result.returncode
            child_stderr = child_result.stderr
        else:
            child_code = child_result
            child_stderr = ""
        self._log(
            "child_exit",
            work_type=work_type,
            work_number=work_number,
            returncode=child_code,
        )
        if child_code == 0:
            self._exit_cooldown(work)
            self.retry_count = 0
            self.last_child_fingerprint = current_fingerprint
            self._write_state("starting", fleet_state=fleet_state)
            return IterationResult("active", 0.0, work_type, work_number, child_code)

        reason = classify_child_failure(child_code, child_stderr)
        entering_cooldown = self.active_cooldown_reason is None
        previous_reason = self.active_cooldown_reason
        self.active_cooldown_reason = reason
        result = self._park("agent_unavailable_wait", fleet, work, cooldown_reason=reason)
        if entering_cooldown:
            retry_at = timestamp(self.clock() + timedelta(seconds=result.delay))
            self.active_cooldown_id = f"cycle:{self.cycle}:{retry_at}:{reason}"
            self._log(
                "cooldown_enter",
                cooldown_reason=reason,
                retry_at=retry_at,
                retry_count=self.retry_count,
            )
            self._notify_transition({
                "state": "cooling-down",
                "text": f"{reason}; eligibility recheck scheduled",
                "cooldown_reason": reason,
                "retry_at": retry_at,
                "work_type": work_type,
                "work_number": work_number,
                "dedupe_key": (
                    f"availability:{self.active_cooldown_id}:cooling-down"
                ),
            })
        elif previous_reason != reason:
            self._log(
                "cooldown_reason_updated",
                previous_reason=previous_reason or "unknown",
                cooldown_reason=reason,
                retry_count=self.retry_count,
            )
        return IterationResult(
            result.phase, result.delay, work_type, work_number, child_code,
        )

    def _stop_requested(self) -> bool:
        try:
            return self.stop_signal or self.store.stop_requested()
        except OSError:
            return self.stop_signal

    def _wait(self, delay: float) -> None:
        remaining = delay
        while remaining > 0 and not self._stop_requested():
            step = min(1.0, remaining)
            self.sleeper(step)
            remaining -= step

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.stop_signal = True
        self._log("stop_signal", signal=signum, drain="active child is allowed to finish")

    def run_loop(self) -> int:
        lock = self.store.acquire_lock(clear_stop=True)
        if lock is None:
            self._log("runner_refused", reason="identity_already_running")
            print(
                f"[run_fleet] {self.config.agent} already has a live runner",
                file=sys.stderr,
            )
            return 2
        previous_handlers: dict[int, Any] = {}
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            self._write_state("starting")
            self._log("runner_start", mode="loop")
            while not self._stop_requested():
                try:
                    result = self.run_iteration()
                except Exception as exc:
                    # A coding-agent loop must not turn an unexpected adapter,
                    # parsing, or local IO exception into operator-owned
                    # termination. Persist only the exception type; exception
                    # messages can contain command output or local paths.
                    result = self._park(
                        "error_wait",
                        {"state": "error", "summary": "runner iteration failed"},
                        {"type": "error"},
                    )
                    self._log("iteration_error", error_type=type(exc).__name__)
                if result.delay > 0:
                    self._wait(result.delay)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._write_state("stopped", terminal_reason="operator_stop")
            self._log("runner_stop", terminal_reason="operator_stop")
            self.store.release_lock(lock)
        return 0

    def run_once(self) -> int:
        lock = self.store.acquire_lock(clear_stop=True)
        if lock is None:
            self._log("runner_refused", reason="identity_already_running")
            return 2
        previous_handlers: dict[int, Any] = {}
        terminal = "once_error"
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            self._write_state("starting")
            self._log("runner_start", mode="once")
            result = self.run_iteration()
            recoverable_failure = result.phase in {"error_wait", "agent_unavailable_wait"}
            code = result.child_returncode or (1 if recoverable_failure else 0)
            if self._stop_requested():
                terminal = "operator_stop"
            else:
                terminal = "once_complete" if code == 0 else "once_error"
            return code
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._write_state("stopped", terminal_reason=terminal)
            self._log("runner_stop", terminal_reason=terminal)
            self.store.release_lock(lock)


def validate_repo(repo: Path) -> Path:
    resolved = repo.expanduser().resolve()
    result = run_command(["git", "rev-parse", "--show-toplevel"], resolved)
    if result.returncode != 0:
        raise ValueError(f"not a git repository: {resolved}")
    top = Path(result.stdout.strip()).resolve()
    if top != resolved:
        raise ValueError(f"--repo must be the checkout root ({top})")
    return resolved


def require_finite(value: float, option: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{option} must be finite")
    return value


def normalize_timing(
    value: float,
    option: str,
    *,
    minimum: float,
    maximum: float,
    cap_upper: bool = False,
) -> float:
    value = require_finite(value, option)
    if value > maximum and not cap_upper:
        raise ValueError(f"{option} must be at most {maximum:g} seconds")
    return max(minimum, min(maximum, value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one optional headless Aru factory CLI worker.",
    )
    parser.add_argument("mode", choices=("once", "loop", "status", "stop"))
    parser.add_argument("--repo", default=".", help="Trusted isolated repository checkout")
    parser.add_argument("--aru-home", default=os.environ.get("ARU_SDLC_HOME", ""))
    parser.add_argument("--agent", required=True, help="Stable factory agent id")
    parser.add_argument("--family", default="", help="Stable model family")
    parser.add_argument("--adapter", choices=("auto", "codex", "claude"), default="auto")
    parser.add_argument(
        "--adapter-command-json", default="",
        help="JSON argv array for another local CLI; supports {repo} and {prompt} tokens",
    )
    parser.add_argument("--state-dir", default="", help="Override process metadata directory")
    parser.add_argument(
        "--presence-path",
        default="",
        help="Optional agent-presence.json path (default: ~/.aru/agent-presence.json)",
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="Optional governed project_id; otherwise derived from --repo",
    )
    parser.add_argument("--initial-wait", type=float, default=15.0)
    parser.add_argument("--max-wait", type=float, default=900.0)
    parser.add_argument("--jitter", type=float, default=0.2)
    parser.add_argument(
        "--helper-timeout", type=float, default=120.0,
        help="Seconds before a stuck status or picker command becomes a retryable error",
    )
    parser.add_argument(
        "--cooldown-recheck", type=float, default=300.0,
        help="Seconds between cooldown probes (capped at 300)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo = validate_repo(Path(args.repo))
        agent = safe_identity(args.agent, "agent")
        family = safe_identity(args.family, "family") if args.mode in {"once", "loop"} else args.family
        initial_wait = normalize_timing(
            args.initial_wait,
            "--initial-wait",
            minimum=0.1,
            maximum=MAX_WAIT_SECONDS,
        )
        max_wait = normalize_timing(
            args.max_wait,
            "--max-wait",
            minimum=0.1,
            maximum=MAX_WAIT_SECONDS,
        )
        jitter = normalize_timing(
            args.jitter,
            "--jitter",
            minimum=0.0,
            maximum=1.0,
            cap_upper=True,
        )
        helper_timeout = normalize_timing(
            args.helper_timeout,
            "--helper-timeout",
            minimum=1.0,
            maximum=MAX_HELPER_TIMEOUT_SECONDS,
        )
        cooldown_recheck = normalize_timing(
            args.cooldown_recheck,
            "--cooldown-recheck",
            minimum=0.1,
            maximum=300.0,
            cap_upper=True,
        )
    except ValueError as exc:
        parser.error(str(exc))

    project_id = args.project_id.strip() if args.project_id else ""
    if project_id:
        try:
            from agent_presence import PROJECT_ID_RE
        except ImportError:
            PROJECT_ID_RE = None
        if PROJECT_ID_RE is not None and not PROJECT_ID_RE.fullmatch(project_id):
            parser.error(f"invalid project_id: {project_id}")

    directory = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir(repo)
    store = StateStore(directory, agent)
    if args.mode == "status":
        state = store.read()
        if state is None:
            print(f"No runner state found for {agent} in {directory}", file=sys.stderr)
            return 1
        state = dict(state)
        state["stop_requested"] = store.stop_requested()
        if args.as_json:
            print(json.dumps(state, indent=2, sort_keys=True))
        else:
            print(
                f"{agent}: phase={state.get('phase')} cycle={state.get('cycle')} "
                f"fleet={state.get('fleet_state')} retry={state.get('retry_count')}"
            )
        return 0
    if args.mode == "stop":
        store.request_stop()
        print(f"Stop requested for {agent}; an active child will drain before exit.")
        return 0

    aru_home_raw = args.aru_home or str(Path(__file__).resolve().parents[1])
    aru_home = Path(aru_home_raw).expanduser().resolve()
    config = RunnerConfig(
        repo=repo,
        aru_home=aru_home,
        agent=agent,
        family=family,
        adapter=args.adapter,
        adapter_command_json=args.adapter_command_json,
        initial_wait=initial_wait,
        max_wait=max_wait,
        jitter=jitter,
        helper_timeout=helper_timeout,
        cooldown_recheck_seconds=cooldown_recheck,
        state_dir=directory,
    )
    presence_store = None
    try:
        from agent_presence import PresenceStore, resolve_project_id

        presence_path = (
            Path(args.presence_path).expanduser().resolve()
            if args.presence_path
            else None
        )
        presence_store = PresenceStore(presence_path) if presence_path else PresenceStore()
        if not project_id:
            project_id = resolve_project_id(repo)
    except Exception:
        presence_store = None
    runner = FleetRunner(
        config,
        presence_store=presence_store,
        project_id=project_id or None,
        transition_notifier=(
            lambda event: post_availability_transition(config, project_id, event)
        ),
    )
    return runner.run_loop() if args.mode == "loop" else runner.run_once()


if __name__ == "__main__":
    sys.exit(main())
