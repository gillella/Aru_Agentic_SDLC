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

from . import execution, scheduler
from .config import Config, DriverError
from .controller import Controller
from .kernel import KernelAdapterError
from .state import State, read_json, write_json


def _bind(config: Config) -> None:
    """One Hermes profile must use one coordinator configuration/account journal."""
    path = config.hermes_home / "state" / "aru_project_driver" / "binding.json"
    expected = {"config": str(config.path), "state_dir": str(config.state_dir)}
    existing = read_json(path, expected)
    if existing != expected:
        raise DriverError("Hermes profile is bound to another Driver config/state directory")
    write_json(path, expected)


def start(config: Config, repo: str) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    with state.lock():
        _bind(config)
        data = state.project(repo)
        # The persistent stop gate stays closed until the native scheduler
        # proves it created the recurring recovery path.
        heartbeat = scheduler.ensure_heartbeat(
            config.hermes_home, repo, config.path, Path(__file__).resolve(),
            hermes_repo=config.hermes_repo,
        )
        was_enabled = data["enabled"]
        data.update(enabled=True, wake_pending_until=0, cooldown_until=0,
                    started_at=data.get("started_at", time.time()))
        if not was_enabled:
            data["generation"] += 1
        state.save(repo, data)
        # A failed immediate wake still leaves the verified heartbeat available.
        wake = scheduler.schedule_wake(
            config.hermes_home, repo, config.path, Path(__file__).resolve(),
            reason="project start", event_key=f"start:{data['generation']}",
            hermes_repo=config.hermes_repo,
        )
        return {"status": "started", "project": repo, "heartbeat": heartbeat, "wake": wake}


def stop(config: Config, repo: str) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    with state.lock(blocking=True):
        data = state.project(repo)
        data.update(enabled=False, wake_pending_until=0, stopped_at=time.time())
        state.save(repo, data)
        result = scheduler.stop_project(config.hermes_home, repo, hermes_repo=config.hermes_repo)
        return {"status": "stopped", "project": repo, "scheduler": result,
                "workers_preserved": True}


def status(config: Config, repo: str) -> dict:
    config.project(repo)
    state = State(config.state_dir)
    data = state.project(repo)
    workers = [{key: r.get(key) for key in (
        "id", "agent", "issue", "pr", "head", "state", "pid", "exit_code", "worktree",
        "started_at", "finished_at", "wake_error",
    )} for r in state.workers(repo)]
    return {"project": repo, "enabled": data["enabled"],
            "last_checked_at": data.get("last_checked_at"), "last_error": data.get("last_error"),
            "last_observation": data.get("last_observation"), "workers": workers,
            "scheduler": scheduler.scheduler_status(
                config.hermes_home, repo, hermes_repo=config.hermes_repo,
            )}


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", required=True, type=Path)
    sub = cli.add_subparsers(dest="operation", required=True)
    for operation in ("start", "stop", "status", "tick", "reconcile", "event"):
        command = sub.add_parser(operation)
        command.add_argument("--project", required=True, help="literal configured owner/repository")
        if operation == "event":
            command.add_argument("--event-id", required=True)
            command.add_argument("--reason", choices=("event", "worker", "review", "operator"), default="event")
            command.add_argument("--inline", action="store_true",
                                 help="caller already has a Hermes activation; do not queue another")
    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--capacity-fd", type=int, required=True)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config(args.config)
        if args.operation == "_worker":
            return execution.worker_main(config, args.worker_id, args.capacity_fd)
        controller = Controller(config)
        if args.operation == "event":
            result = controller.event(args.project, args.event_id, args.reason, inline=args.inline)
        elif args.operation in {"tick", "reconcile"}:
            result = getattr(controller, args.operation)(args.project)
        else:
            result = {"start": start, "stop": stop, "status": status}[args.operation](config, args.project)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DriverError, KernelAdapterError, scheduler.SchedulerError, OSError, ValueError) as exc:
        busy = isinstance(exc, DriverError) and str(exc).startswith("another Driver activation")
        print(json.dumps({"wakeAgent": False, "status": "busy" if busy else "error", "reason": str(exc)}))
        return 0 if busy else 1


if __name__ == "__main__":
    raise SystemExit(main())
