#!/usr/bin/env python3
"""Installed entry point for bounded Hermes Project Driver operations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "aru_project_driver"

from . import execution, scheduler, permissions
from .config import Config, DriverError
from .controller import Controller
from .kernel import KernelAdapter, KernelAdapterError
from .state import State

def start(config: Config, repo: str) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    observed_stop = state.stop_nonce(repo)  # Capture before any coordinating wait.
    with state.lock(), state.project_lock(repo):
        if state.stop_nonce(repo) != observed_stop:
            raise DriverError("Start was superseded by Stop")
        data = state.project(repo)
        # Start alone may acknowledge this Stop generation, after the native
        # heartbeat exists. A later Stop nonce always defeats this snapshot.
        heartbeat = scheduler.ensure_heartbeat(
            config.hermes_home, repo, config.path, Path(__file__).resolve(),
            hermes_repo=config.hermes_repo, start_nonce=observed_stop,
        )
        if state.stop_nonce(repo) != observed_stop:
            raise DriverError("Start was superseded by Stop")
        was_enabled = data["enabled"]
        data.update(enabled=True, acknowledged_stop=observed_stop,
                    wake_pending_until=0, cooldown_until=0,
                    started_at=data.get("started_at", time.time()))
        if not was_enabled:
            data["generation"] += 1
        state.save(repo, data)
        wake = scheduler.schedule_wake(
            config.hermes_home, repo, config.path, Path(__file__).resolve(),
            reason="project start", event_key=f"start:{data['generation']}",
            hermes_repo=config.hermes_repo,
        )
        state.require_admission(repo, observed_stop)
        return {"status": "started", "project": repo, "heartbeat": heartbeat, "wake": wake}

def stop(config: Config, repo: str, *, timeout_seconds: float = 20) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    deadline = scheduler.deadline_after(timeout_seconds)
    result = {"status": "partial", "project": repo, "stop_intent_persisted": False,
              "spawn_barrier_verified": False, "workers_preserved": True,
              "scheduler": {"verified": False}}
    try:
        with scheduler.bounded(deadline):
            state.request_stop(repo)
            result["stop_intent_persisted"] = True
            # An already admitted Popen may still finish while Stop is written.
            # Crossing this short barrier settles it before acknowledging Stop.
            with state.project_lock(repo, spawn=True):
                result["spawn_barrier_verified"] = True
            with state.project_lock(repo):
                if state.project(repo)["enabled"]:
                    raise DriverError("Stop was superseded by a later explicit Start")
                paused = scheduler.stop_project(config.hermes_home, repo, hermes_repo=config.hermes_repo)
                result["scheduler"] = {**paused, "verified": True}
                if state.project(repo)["enabled"]:
                    raise DriverError("dispatch state changed during Stop readback")
                result.update(status="stopped", enabled=False)
    except (DriverError, scheduler.SchedulerError, OSError, ValueError) as exc:
        result.update(status="partial", reason=str(exc))
    # Do not turn a deadline into another unbounded scheduler observation.
    return result

def status(config: Config, repo: str, *, timeout_seconds: float = 20) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    result = {"project": repo}
    try:
        with scheduler.bounded(scheduler.deadline_after(timeout_seconds)):
            data = state.project(repo)
            workers = [{key: r.get(key) for key in (
                "id", "agent", "issue", "pr", "head", "state", "pid", "exit_code", "worktree",
                "started_at", "finished_at", "wake_error", "outcome", "reason", "retry_blocked",
                "policy_fingerprint", "result_path", "quota_decision", "quota_continuation", "quota_measurement",
                "retry_observation",
            )} for r in state.workers(repo)]
            result.update(enabled=data["enabled"], last_checked_at=data.get("last_checked_at"),
                          last_error=data.get("last_error"), last_observation=data.get("last_observation"),
                          workers=workers)
            if config.project(repo).get("quota_admission"):
                from .state import key, read_json
                result["quota"] = read_json(state.root / "quota-decisions" / (key(repo) + ".json"), {"decisions": []})
                result["quota"]["cooldowns"] = {i: read_json(state.root / "cooldowns" / (
                    key(config.lane(repo, i)["capacity_key"]) + ".json"), {"until": 0}) for i in config.project(repo)["lanes"]}
            result["scheduler"] = scheduler.scheduler_status(
                config.hermes_home, repo, hermes_repo=config.hermes_repo,
            )
    except (DriverError, scheduler.SchedulerError, OSError, ValueError) as exc:
        result.update(status="partial", reason=str(exc), scheduler={"verified": False})
    return result

def _honest_health(config: Config, repo: str, result: dict) -> dict:
    """Never let a quiet or cooling-down precheck launder a recorded Driver failure.

    The native scheduler marks a job `ok` whenever its script exits zero, so a
    heartbeat that skips work while `last_error` is still recorded would
    overwrite the previous degraded run in operator status. Reconcile clears
    `last_error` on success, which restores healthy reporting.
    """
    if not isinstance(result, dict) or result.get("status"):
        return result
    data = State(config.state_dir).project(repo)
    if data.get("enabled") and data.get("last_error"):
        return {**result, "status": "degraded", "last_error": data["last_error"]}
    return result

def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", required=True, type=Path)
    sub = cli.add_subparsers(dest="operation", required=True)
    for operation in ("start", "stop", "status", "tick", "reconcile", "event"):
        command = sub.add_parser(operation)
        command.add_argument("--project", required=True, help="literal configured owner/repository")
        if operation in {"stop", "status"}:
            command.add_argument("--timeout-seconds", type=float, default=20,
                                 help="remaining wall-time budget for this operation (default 20s)")
        if operation == "event":
            command.add_argument("--event-id", required=True)
            command.add_argument("--reason", choices=("event", "worker", "review", "operator"), default="event")
            command.add_argument("--inline", action="store_true",
                                 help="caller already has a Hermes activation; do not queue another")
    handoff = sub.add_parser("handoff", help="validate and deliver a typed dependency handoff")
    handoff.add_argument("--project", required=True, help="source configured owner/repository")
    handoff.add_argument("--source-issue", required=True, type=int)
    handoff.add_argument("--dependency-event", action="store_true",
                         help="prove the contract and wake the source project when satisfied")
    preflight = sub.add_parser("preflight", help="print a no-write Claude capability probe; never execute it")
    preflight.add_argument("--project", required=True)
    preflight.add_argument("--agent", required=True)
    preflight.add_argument("--issue", type=int, required=True)
    preflight.add_argument("--worktree", required=True)
    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--capacity-fd", type=int, required=True)
    return cli

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config(args.config, bind=args.operation != "preflight")
        if args.operation == "preflight":
            record = {"repo": args.project, "agent": args.agent, "issue": args.issue,
                      "worktree": args.worktree, "kind": "implementation", "prompt": ""}
            adapter = config.kernel_adapter(args.project, KernelAdapter)
            result = permissions.compile_policy(config, record, adapter, execution.run_bounded, preflight=True)
            print(json.dumps({**result, "executed": False, "cwd": args.worktree}, sort_keys=True))
            return 0
        if args.operation == "_worker":
            return execution.worker_main(config, args.worker_id, args.capacity_fd)
        controller = Controller(config)
        if args.operation == "event":
            result = controller.event(args.project, args.event_id, args.reason, inline=args.inline)
        elif args.operation == "handoff":
            result = (controller.dependency_event(args.project, args.source_issue)
                      if args.dependency_event
                      else controller.handoff(args.project, args.source_issue))
        elif args.operation in {"tick", "reconcile"}:
            result = getattr(controller, args.operation)(args.project)
            if args.operation == "tick":
                result = _honest_health(config, args.project, result)
        else:
            options = {"timeout_seconds": args.timeout_seconds} if args.operation in {"stop", "status"} else {}
            result = {"start": start, "stop": stop, "status": status}[args.operation](config, args.project, **options)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("status") == "partial" else 0
    except (DriverError, KernelAdapterError, scheduler.SchedulerError, OSError, ValueError) as exc:
        busy = isinstance(exc, DriverError) and str(exc).startswith("another Driver activation")
        print(json.dumps({"wakeAgent": False, "status": "busy" if busy else "error", "reason": str(exc)}))
        return 0 if busy else 1

if __name__ == "__main__":
    raise SystemExit(main())
