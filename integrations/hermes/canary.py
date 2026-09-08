#!/usr/bin/env python3
"""Bounded live canary for the Hermes Project Driver refill path.

Drives only the installed Driver entrypoints (``start``, ``status``, ``event``,
``stop``) and the canonical kernel helpers, records every observation with a
timestamp and exact identifiers, and writes one JSON evidence file. A step that
cannot be observed within its bound is recorded as ``unproven``, never as pass.
``--dry-run`` substitutes injected fakes for every external command so the
sequence and its failure paths are testable without touching GitHub or a
scheduler; a live run is a separate operator-approved action.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA = "aru.canary/v1"
Runner = Callable[[list[str]], subprocess.CompletedProcess]


class CanaryError(RuntimeError):
    """The canary cannot continue safely; partial evidence is still written."""


@dataclass
class Step:
    name: str
    status: str = "unproven"  # pass | failed | unproven | skipped
    reason: str = ""
    ids: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def close(self, status: str, reason: str = "", **ids: Any) -> "Step":
        self.status, self.reason, self.finished_at = status, reason, time.time()
        self.ids.update({k: v for k, v in ids.items() if v is not None})
        return self


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    # Fixed executables and literal arguments; never a shell.
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)


class Canary:
    """One bounded refill canary against one configured Driver project."""

    def __init__(self, config: Path, project: str, *, fixtures: int = 2, bound_seconds: int = 900,
                 poll_seconds: float = 15.0, runner: Runner = _run, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, dry_run: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project):
            raise CanaryError("project must be a literal owner/repository")
        if fixtures < 1 or bound_seconds < 1:
            raise CanaryError("fixtures and bound_seconds must be positive")
        self.config, self.project = Path(config), project
        self.fixtures, self.bound, self.poll = fixtures, bound_seconds, poll_seconds
        self.runner, self.clock, self.sleep, self.dry_run = runner, clock, sleep, dry_run
        raw = json.loads(self.config.read_text())
        self.driver = Path(raw["hermes_home"]) / "scripts" / "aru_project_driver" / "driver.py"
        self.repo_dir = Path(raw["projects"][project]["repo_dir"])
        self.steps: list[Step] = []

    # -- external commands ---------------------------------------------------
    def _driver(self, *args: str) -> dict:
        """Run one installed Driver operation and parse its single JSON object."""
        result = self.runner([sys.executable, str(self.driver), "--config", str(self.config), *args])
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (ValueError, IndexError) as exc:
            raise CanaryError(f"driver {args[0]} returned no JSON: {result.stderr[-300:]}") from exc
        if not isinstance(payload, dict):
            raise CanaryError(f"driver {args[0]} returned a non-object")
        if result.returncode and payload.get("status") != "busy":
            raise CanaryError(f"driver {args[0]} failed: {payload.get('reason', result.stderr[-300:])}")
        return payload

    def _gh(self, *args: str) -> str:
        result = self.runner(["gh", *args])
        if result.returncode:
            raise CanaryError(f"gh {args[0]} failed: {result.stderr[-300:]}")
        return result.stdout.strip()

    def _status(self) -> dict:
        return self._driver("status", "--project", self.project)

    def _wait(self, predicate: Callable[[dict], Any], *, bound: int | None = None) -> Any:
        """Poll status until predicate returns a truthy value or the bound elapses."""
        deadline = self.clock() + (bound or self.bound)
        while True:
            status = self._status()
            found = predicate(status)
            if found:
                return found
            if self.clock() >= deadline:
                return None
            self.sleep(self.poll)

    @staticmethod
    def _workers(status: dict, issue: int | None = None) -> list[dict]:
        workers = [w for w in status.get("workers", []) if isinstance(w, dict)]
        return [w for w in workers if issue is None or w.get("issue") == issue]

    # -- steps ---------------------------------------------------------------
    def _step(self, name: str) -> Step:
        step = Step(name, started_at=self.clock())
        self.steps.append(step)
        return step

    def create_fixture(self, index: int) -> int:
        step = self._step(f"fixture-{index}")
        body = (
            "## Outcome\n\nCanary fixture: append one dated line to CANARY.md and open a PR.\n\n"
            "## Acceptance Criteria\n\n- [ ] CANARY.md gains one line naming this issue. "
            "(verify: `grep -c canary CANARY.md`)\n\ntouches: CANARY.md\n"
        )
        url = self._gh("issue", "create", "--repo", self.project, "--title",
                       f"chore(canary): refill fixture {index}", "--label", "status:backlog",
                       "--label", "type:chore", "--body", body)
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        step.close("pass", "fixture issue created", issue=number, url=url)
        return number

    def start(self) -> dict:
        step = self._step("start")
        started = self._driver("start", "--project", self.project)
        heartbeat, wake = started.get("heartbeat", {}), started.get("wake", {})
        ok = started.get("status") == "started" and heartbeat.get("enabled") is True
        step.close("pass" if ok else "failed", "" if ok else "start did not enable the heartbeat",
                   heartbeat_job_id=heartbeat.get("heartbeat_job_id"), wake_job_id=wake.get("wake_job_id"))
        if not ok:
            raise CanaryError("Driver start failed")
        return started

    def observe_dispatch(self, issue: int, name: str) -> dict | None:
        """Wait for the Driver to claim and launch a worker for the fixture on its own."""
        step = self._step(name)
        worker = self._wait(lambda s: next(iter(self._workers(s, issue)), None))
        if worker is None:
            step.close("unproven", f"no worker for #{issue} within {self.bound}s")
            return None
        step.close("pass", "worker launched by the Driver", worker_id=worker.get("id"),
                   agent=worker.get("agent"), issue=issue)
        return worker

    def observe_completion(self, issue: int, worker_id: str) -> dict | None:
        step = self._step(f"completion-{issue}")
        done = self._wait(lambda s: next((w for w in self._workers(s, issue)
                                          if w.get("id") == worker_id and w.get("state") == "exited"), None))
        if done is None:
            step.close("unproven", f"worker {worker_id} did not exit within {self.bound}s")
            return None
        prs = self._gh("pr", "list", "--repo", self.project, "--search", f"Closes #{issue} in:body",
                       "--json", "number,headRefOid", "--jq", ".[0] | \"\\(.number) \\(.headRefOid)\"")
        number, _, head = prs.partition(" ")
        if not number:
            step.close("failed", "worker exited without a PR closing the fixture", exit_code=done.get("exit_code"))
            return done
        step.close("pass", "worker exited and its PR exists", exit_code=done.get("exit_code"),
                   pr=int(number), head=head)
        return done

    def duplicate_delivery(self) -> None:
        step = self._step("duplicate-delivery")
        event_id = f"canary-{int(self.clock())}"
        before = self._status()["scheduler"].get("enabled_wakes", 0)
        first = self._driver("event", "--project", self.project, "--event-id", event_id, "--reason", "event")
        second = self._driver("event", "--project", self.project, "--event-id", event_id, "--reason", "event")
        after = self._status()["scheduler"].get("enabled_wakes", 0)
        suppressed = second.get("wakeAgent") is not True and after - before <= 1
        step.close("pass" if suppressed else "failed",
                   "second delivery of the same event created no new wake" if suppressed
                   else "duplicate delivery created a second wake", event_id=event_id,
                   first=first.get("wakeAgent"), second=second.get("wakeAgent"), wakes_before=before, wakes_after=after)

    def stop_during_worker(self, issue: int) -> None:
        step = self._step("stop-with-active-worker")
        live = next((w for w in self._workers(self._status(), issue) if w.get("state") in {"launching", "running"}), None)
        stopped = self._driver("stop", "--project", self.project)
        status = self._status()
        preserved = live is not None and any(w.get("id") == live["id"] for w in self._workers(status, issue))
        gate_closed = status.get("enabled") is False and status["scheduler"].get("enabled_heartbeats", 1) == 0
        if live is None:
            step.close("unproven", "no active worker was running when Stop was issued",
                       paused=stopped.get("scheduler", {}).get("paused_job_ids"))
        else:
            ok = preserved and gate_closed
            step.close("pass" if ok else "failed",
                       "Stop closed dispatch and preserved the live worker" if ok else
                       f"preserved={preserved} gate_closed={gate_closed}", worker_id=live["id"],
                       paused=stopped.get("scheduler", {}).get("paused_job_ids"))

    def restart(self, expected_workers: int) -> None:
        step = self._step("restart")
        started = self._driver("start", "--project", self.project)
        status = self._status()
        one_heartbeat = status["scheduler"].get("enabled_heartbeats") == 1
        same_workers = len([w for w in self._workers(status) if w.get("state") in {"launching", "running"}]) <= expected_workers
        ok = started.get("status") == "started" and one_heartbeat and same_workers
        step.close("pass" if ok else "failed", "" if ok else f"heartbeats={status['scheduler'].get('enabled_heartbeats')} duplicate_claims={not same_workers}",
                   heartbeat_job_id=started.get("heartbeat", {}).get("heartbeat_job_id"))

    # -- orchestration -------------------------------------------------------
    def run(self) -> dict:
        started_at = self.clock()
        error = None
        try:
            fixtures = [self.create_fixture(i + 1) for i in range(self.fixtures)]
            self.start()
            first = self.observe_dispatch(fixtures[0], "dispatch-1")
            if first is not None:
                self.observe_completion(fixtures[0], first["id"])
                if len(fixtures) > 1:
                    # Refill: the next fixture must be dispatched without any operator command.
                    second = self.observe_dispatch(fixtures[1], "refill")
                    if second is not None:
                        # Exercise Stop while that worker is still live, then restart.
                        self.stop_during_worker(fixtures[1])
                        self.restart(expected_workers=1)
                    self.duplicate_delivery()
            else:
                for name in ("completion", "refill", "duplicate-delivery", "stop-with-active-worker", "restart"):
                    self._step(name).close("skipped", "first dispatch was not observed")
        except CanaryError as exc:
            error = str(exc)
        finally:
            # Always leave the project stopped; never leave the canary running unattended.
            try:
                self._driver("stop", "--project", self.project)
            except CanaryError as exc:
                error = error or f"final stop failed: {exc}"
        statuses = {s.status for s in self.steps}
        result = "failed" if error or "failed" in statuses else "unproven" if statuses - {"pass"} else "pass"
        return {"schema": SCHEMA, "project": self.project, "dry_run": self.dry_run, "started_at": started_at,
                "finished_at": self.clock(), "result": result, "error": error,
                "steps": [{"name": s.name, "status": s.status, "reason": s.reason, "ids": s.ids,
                           "started_at": s.started_at, "finished_at": s.finished_at} for s in self.steps]}


class FakeDriver:
    """Dry-run stand-in for the installed Driver and gh; deterministic, no network.

    Knobs model the failure paths the canary must report honestly: a Driver
    that never dispatches, one that never refills, duplicate deliveries that
    create a second wake, a Stop that loses the live worker, and a restart that
    duplicates the heartbeat.
    """

    def __init__(self, *, dispatch_after: int = 1, complete_after: int = 4, refill: bool = True,
                 suppress_duplicates: bool = True, preserve_on_stop: bool = True,
                 duplicate_heartbeat_on_restart: bool = False, opens_pr: bool = True):
        self.knobs = dict(dispatch_after=dispatch_after, complete_after=complete_after, refill=refill,
                          suppress_duplicates=suppress_duplicates, preserve_on_stop=preserve_on_stop,
                          duplicate_heartbeat_on_restart=duplicate_heartbeat_on_restart, opens_pr=opens_pr)
        self.enabled, self.heartbeats, self.wakes, self.polls, self.starts = False, 0, 0, 0, 0
        self.issues: list[int] = []
        self.workers: list[dict] = []
        self.events: set[str] = set()
        self.calls: list[list[str]] = []

    def _dispatch(self) -> None:
        launched = {w["issue"] for w in self.workers}
        pending = [n for n in self.issues if n not in launched]
        running = any(w["state"] == "running" for w in self.workers)
        if not pending or running or (launched and not self.knobs["refill"]):
            return  # one worker at a time, like a max_workers: 1 project
        if self.polls >= self.knobs["dispatch_after"] * (len(launched) + 1):
            self.workers.append({"id": f"worker-{pending[0]}", "agent": "lane-a", "issue": pending[0],
                                 "state": "running", "since_poll": self.polls})

    def _advance(self) -> None:
        self.polls += 1
        for worker in self.workers:
            if worker["state"] == "running" and self.polls - worker["since_poll"] >= self.knobs["complete_after"]:
                worker["state"], worker["exit_code"] = "exited", 0
        if self.enabled:
            self._dispatch()

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        out: Any
        if argv[0] == "gh":
            if argv[1:3] == ["issue", "create"]:
                self.issues.append(len(self.issues) + 101)
                out = f"https://example.test/issues/{self.issues[-1]}"
            elif argv[1:3] == ["pr", "list"]:
                out = "7 " + "f" * 40 if self.knobs["opens_pr"] else ""
            else:
                return subprocess.CompletedProcess(argv, 1, "", "unsupported fake gh command")
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        op = argv[argv.index("--config") + 2]
        if op == "start":
            self.enabled = True
            self.starts += 1
            # A faulty restart would create a second heartbeat instead of reusing the paused one.
            self.heartbeats = 2 if self.starts > 1 and self.knobs["duplicate_heartbeat_on_restart"] else 1
            self.wakes += 1
            out = {"status": "started", "heartbeat": {"enabled": True, "heartbeat_job_id": "hb-1"}, "wake": {"wake_job_id": f"wake-{self.wakes}"}}
        elif op == "stop":
            self.enabled, self.heartbeats = False, 0
            if not self.knobs["preserve_on_stop"]:
                self.workers = [w for w in self.workers if w["state"] != "running"]
            out = {"status": "stopped", "scheduler": {"paused_job_ids": ["hb-1"]}}
        elif op == "event":
            event_id = argv[argv.index("--event-id") + 1]
            duplicate = event_id in self.events
            self.events.add(event_id)
            if not duplicate or not self.knobs["suppress_duplicates"]:
                self.wakes += 1
            out = {"wakeAgent": not duplicate or not self.knobs["suppress_duplicates"]}
        elif op == "status":
            self._advance()
            out = {"enabled": self.enabled, "last_error": None, "workers": [dict(w) for w in self.workers],
                   "scheduler": {"enabled_heartbeats": self.heartbeats, "enabled_wakes": self.wakes}}
        else:
            return subprocess.CompletedProcess(argv, 1, json.dumps({"status": "error", "reason": "unknown op"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(out) + "\n", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--fixtures", type=int, default=2)
    parser.add_argument("--bound-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true", help="use scripted fakes; never call gh or the Driver")
    args = parser.parse_args(argv)
    runner: Runner = FakeDriver() if args.dry_run else _run
    try:
        canary = Canary(args.config, args.project, fixtures=args.fixtures, bound_seconds=args.bound_seconds,
                        runner=runner, dry_run=args.dry_run, sleep=(lambda _s: None) if args.dry_run else time.sleep)
        evidence = canary.run()
    except (CanaryError, OSError, ValueError, KeyError) as exc:
        evidence = {"schema": SCHEMA, "project": args.project, "result": "failed", "error": str(exc), "steps": []}
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": evidence["result"], "evidence": str(args.evidence)}))
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
