#!/usr/bin/env python3
"""Bounded live canary for the Hermes Project Driver refill path.

Uses the installed Driver entrypoints (``start``, ``status``, ``event``,
``stop``) and ``gh`` to record bounded observations. It does not verify the
complete governed lifecycle: missing acceptance evidence remains ``unproven``.
One monotonic deadline covers observations; final Stop has a separate allowance.
``--dry-run`` substitutes injected fakes for every external command so the
sequence and its failure paths are testable without touching GitHub or a
scheduler; a live run is a separate operator-approved action.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA = "aru.canary/v1"
Runner = Callable[..., subprocess.CompletedProcess]
CLEANUP_SECONDS = 30
UNPROVEN_CHECKS = {
    "canonical-fixture-readiness": "GitHub Project membership, Ready validation and exclusive claims are not verified",
    "governed-completion-and-refill": "exact-head CI/review, confirmed merge/Done and subsequent automatic dispatch are not verified",
    "stale-delivery": "stale delivery is not exercised by the duplicate CLI event check",
    "lost-worker-recovery": "lost-worker recovery is not exercised by restarting an active worker",
    "stop-dispatch-suppression": "an immediate Stop snapshot does not observe suppression of later dispatch",
}


class CanaryError(RuntimeError):
    """The canary cannot continue safely; partial evidence is still written."""


class DeadlineExpired(CanaryError):
    """The shared observation budget is exhausted; only bounded cleanup remains."""


@dataclass
class Step:
    name: str
    status: str = "unproven"  # pass | failed | unproven | skipped
    reason: str = ""
    ids: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    clock: Callable[[], float] = time.time

    def close(self, status: str, reason: str = "", **ids: Any) -> "Step":
        self.status, self.reason, self.finished_at = status, reason, self.clock()
        self.ids.update({k: v for k, v in ids.items() if v is not None})
        return self


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    # Fixed executables and literal arguments; never a shell.
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)


class Canary:
    """One bounded refill canary against one configured Driver project."""

    def __init__(self, config: Path, project: str, *, fixtures: int = 2, bound_seconds: int = 900,
                 poll_seconds: float = 15.0, runner: Runner = _run, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, dry_run: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", project):
            raise CanaryError("project must be a literal owner/repository")
        if fixtures < 1 or bound_seconds < 1 or not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise CanaryError("fixtures, bound_seconds and poll_seconds must be positive")
        self.config, self.project = Path(config), project
        self.fixtures, self.bound, self.poll = fixtures, bound_seconds, poll_seconds
        self.runner, self.clock, self.sleep, self.dry_run = runner, clock, sleep, dry_run
        self.wall_clock = wall_clock
        raw = json.loads(self.config.read_text())
        self.driver = Path(raw["hermes_home"]) / "scripts" / "aru_project_driver" / "driver.py"
        self.repo_dir = Path(raw["projects"][project]["repo_dir"])
        self.steps: list[Step] = []
        self.deadline = self.cleanup_deadline = 0.0
        self.started_at = 0.0

    # -- external commands ---------------------------------------------------
    def _command(self, argv: list[str], *, cleanup: bool = False) -> subprocess.CompletedProcess:
        remaining = (self.cleanup_deadline if cleanup else self.deadline) - self.clock()
        if remaining <= 0:
            raise DeadlineExpired("cleanup deadline expired" if cleanup else "whole-run observation deadline expired")
        try:
            result = self.runner(argv, timeout=min(120, remaining))
        except subprocess.TimeoutExpired as exc:
            # Do not include argv/output: worker commands may contain private configuration.
            if not cleanup and remaining <= 120:
                raise DeadlineExpired("whole-run observation command timed out") from exc
            raise CanaryError("cleanup command timed out" if cleanup else "observation command timed out") from exc
        # Also fail closed for an injected/nonconforming runner that overruns its timeout.
        if self.clock() > (self.cleanup_deadline if cleanup else self.deadline):
            raise DeadlineExpired("cleanup command exceeded deadline" if cleanup else "observation command exceeded deadline")
        return result

    def _driver(self, *args: str, cleanup: bool = False) -> dict:
        """Run one installed Driver operation and parse its single JSON object."""
        result = self._command([sys.executable, str(self.driver), "--config", str(self.config), *args], cleanup=cleanup)
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        except (ValueError, IndexError) as exc:
            raise CanaryError(f"driver {args[0]} returned no JSON") from exc
        if not isinstance(payload, dict):
            raise CanaryError(f"driver {args[0]} returned a non-object")
        if result.returncode and payload.get("status") != "busy":
            raise CanaryError(f"driver {args[0]} failed (exit {result.returncode})")
        if args[0] in {"start", "stop", "status"} and payload.get("project") != self.project:
            raise CanaryError(f"driver {args[0]} returned a mismatched project")
        return payload

    def _gh(self, *args: str) -> str:
        result = self._command(["gh", *args])
        if result.returncode:
            raise CanaryError(f"gh {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def _status(self, *, cleanup: bool = False) -> dict:
        status = self._driver("status", "--project", self.project, cleanup=cleanup)
        scheduler = status.get("scheduler")
        if (status.get("project") != self.project or type(status.get("enabled")) is not bool
                or not isinstance(scheduler, dict)
                or any(type(scheduler.get(k)) is not int or scheduler[k] < 0
                       for k in ("enabled_heartbeats", "enabled_wakes", "enabled_review_wakes"))):
            raise CanaryError("malformed Driver status/scheduler observation")
        self._workers(status)  # Validate even an empty or unrelated inventory.
        return status

    def _wait(self, predicate: Callable[[dict], Any]) -> Any:
        """Poll status until predicate returns a truthy value or the bound elapses."""
        while self.clock() < self.deadline:
            status = self._status()
            found = predicate(status)
            if found:
                return found
            self.sleep(min(self.poll, max(0, self.deadline - self.clock())))
        return None

    @staticmethod
    def _workers(status: dict, issue: int | None = None) -> list[dict]:
        workers = status.get("workers")
        if not isinstance(workers, list) or any(not isinstance(w, dict) for w in workers):
            raise CanaryError("malformed worker inventory")
        ids = [w.get("id") for w in workers]
        if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise CanaryError("malformed or duplicate worker identities")
        return [w for w in workers if issue is None or w.get("issue") == issue]

    def _fresh_worker(self, worker: dict) -> bool:
        """Only a current, identified running receipt supports a dispatch observation."""
        started = worker.get("started_at")
        if (not isinstance(worker.get("id"), str) or not worker["id"]
                or not isinstance(worker.get("agent"), str) or not worker["agent"]
                or type(worker.get("issue")) is not int
                or type(started) not in (int, float) or not math.isfinite(started)):
            raise CanaryError("malformed worker identity/timestamp")
        return (worker.get("state") == "running" and type(worker.get("pid")) is int
                and worker["pid"] > 0 and self.started_at <= started <= self.wall_clock())

    # -- steps ---------------------------------------------------------------
    def _step(self, name: str) -> Step:
        step = Step(name, started_at=self.wall_clock(), clock=self.wall_clock)
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
        step.ids["url"] = url
        match = re.fullmatch(rf"https://github\.com/{re.escape(self.project)}/issues/([1-9][0-9]*)", url)
        if not match:
            raise CanaryError("fixture creation returned no exact repository issue URL")
        number = int(match[1])
        step.close("pass", "fixture issue created", issue=number, url=url)
        return number

    def start(self) -> dict:
        step = self._step("start")
        started = self._driver("start", "--project", self.project)
        heartbeat, wake = started.get("heartbeat", {}), started.get("wake", {})
        ok = (started.get("status") == "started" and isinstance(heartbeat, dict) and isinstance(wake, dict)
              and heartbeat.get("enabled") is True
              and isinstance(heartbeat.get("heartbeat_job_id"), str) and bool(heartbeat["heartbeat_job_id"])
              and isinstance(wake.get("wake_job_id"), str) and bool(wake["wake_job_id"]))
        step.close("pass" if ok else "failed", "" if ok else "start did not enable the heartbeat",
                   heartbeat_job_id=heartbeat.get("heartbeat_job_id") if isinstance(heartbeat, dict) else None,
                   wake_job_id=wake.get("wake_job_id") if isinstance(wake, dict) else None)
        if not ok:
            raise CanaryError("Driver start failed")
        return started

    def observe_dispatch(self, issue: int, name: str) -> dict | None:
        """Observe a fresh running receipt without claiming GitHub lifecycle proof."""
        step = self._step(name)
        worker = self._wait(lambda s: next((w for w in self._workers(s, issue) if self._fresh_worker(w)), None))
        if worker is None:
            step.close("unproven", f"no fresh running worker for #{issue} within the whole-run {self.bound}s budget")
            return None
        step.close("unproven" if name == "refill" else "pass",
                   "second worker receipt observed; dispatch after governed merge/Done is not verified" if name == "refill"
                   else "fresh running worker receipt observed; exclusive GitHub claim is not verified",
                   worker_id=worker["id"], agent=worker["agent"], issue=issue, pid=worker["pid"],
                   worker_started_at=worker["started_at"])
        return worker

    def observe_completion(self, issue: int, worker_id: str) -> dict | None:
        step = self._step(f"completion-{issue}")
        step.ids.update(issue=issue, worker_id=worker_id)
        done = self._wait(lambda s: next((w for w in self._workers(s, issue)
                                          if w.get("id") == worker_id and w.get("state") == "exited"), None))
        if done is None:
            step.close("unproven", f"worker {worker_id} did not exit within the whole-run {self.bound}s budget")
            return None
        finished = done.get("finished_at")
        if (type(done.get("exit_code")) is not int or type(finished) not in (int, float)
                or not math.isfinite(finished) or not self.started_at <= finished <= self.wall_clock()):
            raise CanaryError("malformed worker exit evidence")
        if done["exit_code"] != 0:
            step.close("failed", "worker exited unsuccessfully", exit_code=done["exit_code"])
            return done
        prs = json.loads(self._gh("pr", "list", "--repo", self.project, "--state", "all", "--limit", "2",
                                 "--search", f"Closes #{issue} in:body", "--json", "number,headRefOid,state"))
        if not isinstance(prs, list):
            raise CanaryError("malformed PR inventory")
        if not prs:
            step.close("failed", "worker exited without a PR closing the fixture", exit_code=done.get("exit_code"))
            return done
        if len(prs) != 1:
            step.close("unproven", "multiple PR candidates; exact closing PR is unproven")
            return done
        pr = prs[0]
        if (not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] < 1
                or not isinstance(pr.get("headRefOid"), str) or not re.fullmatch(r"[0-9a-f]{40}", pr["headRefOid"])
                or pr.get("state") not in {"OPEN", "CLOSED", "MERGED"}):
            raise CanaryError("malformed PR identity/head/state")
        step.close("unproven", "worker exited and PR candidate observed; exact-head CI/review and confirmed merge/Done are not verified",
                   exit_code=done["exit_code"], pr=pr["number"], head=pr["headRefOid"], pr_state=pr["state"])
        return done

    def duplicate_delivery(self) -> None:
        step = self._step("duplicate-delivery")
        event_id = f"canary-{int(self.clock())}"
        step.ids["event_id"] = event_id
        before = self._status()["scheduler"].get("enabled_wakes", 0)
        step.ids["wakes_before"] = before
        first = self._driver("event", "--project", self.project, "--event-id", event_id, "--reason", "event")
        step.ids["first"] = first.get("wakeAgent")
        second = self._driver("event", "--project", self.project, "--event-id", event_id, "--reason", "event")
        after = self._status()["scheduler"].get("enabled_wakes", 0)
        suppressed = first.get("wakeAgent") is True and second.get("wakeAgent") is False and 0 <= after - before <= 1
        step.close("pass" if suppressed else "failed",
                   "duplicate CLI delivery suppressed in scheduler snapshot; claims/workers not verified" if suppressed
                   else "duplicate delivery suppression is absent or ambiguous", event_id=event_id,
                   first=first.get("wakeAgent"), second=second.get("wakeAgent"), wakes_before=before, wakes_after=after)

    def stop_during_worker(self, issue: int) -> None:
        step = self._step("stop-with-active-worker")
        live = next((w for w in self._workers(self._status(), issue) if self._fresh_worker(w)), None)
        if live:
            step.ids["worker_id"] = live["id"]
        stopped = self._driver("stop", "--project", self.project)
        if stopped.get("status") != "stopped" or not isinstance(stopped.get("scheduler"), dict):
            raise CanaryError("malformed Stop response")
        status = self._status()
        preserved = live is not None and any(w.get("id") == live["id"] for w in self._workers(status, issue))
        gate_closed = status["enabled"] is False and all(
            status["scheduler"][k] == 0 for k in ("enabled_heartbeats", "enabled_wakes", "enabled_review_wakes"))
        if live is None:
            step.close("unproven", "no active worker was running when Stop was issued",
                       paused=stopped.get("scheduler", {}).get("paused_job_ids"))
        else:
            ok = preserved and gate_closed
            step.close("pass" if ok else "failed",
                       "Stop snapshot disabled heartbeat and retained the worker receipt; later dispatch not observed" if ok else
                       f"preserved={preserved} gate_closed={gate_closed}", worker_id=live["id"],
                       paused=stopped.get("scheduler", {}).get("paused_job_ids"))

    def restart(self, expected_workers: set[str]) -> None:
        step = self._step("restart")
        started = self._driver("start", "--project", self.project)
        if not isinstance(started.get("heartbeat"), dict):
            raise CanaryError("malformed restart heartbeat response")
        status = self._status()
        one_heartbeat = status["scheduler"].get("enabled_heartbeats") == 1
        same_workers = {w.get("id") for w in self._workers(status)
                        if w.get("state") in {"launching", "running"}} <= expected_workers
        ok = started.get("status") == "started" and one_heartbeat and same_workers
        step.close("pass" if ok else "failed", "restart snapshot has one heartbeat and no unexpected live worker identities" if ok
                   else f"heartbeats={status['scheduler'].get('enabled_heartbeats')} unexpected_workers={not same_workers}",
                   heartbeat_job_id=started.get("heartbeat", {}).get("heartbeat_job_id"))

    # -- orchestration -------------------------------------------------------
    def run(self) -> dict:
        self.started_at = self.wall_clock()
        self.deadline = self.clock() + self.bound
        error = None
        try:
            fixtures = [self.create_fixture(i + 1) for i in range(self.fixtures)]
            self.start()
            first = self.observe_dispatch(fixtures[0], "dispatch-1")
            if first is not None:
                self.observe_completion(fixtures[0], first["id"])
                if len(fixtures) > 1:
                    # This observes another receipt; it does not prove ordering after merge/Done.
                    second = self.observe_dispatch(fixtures[1], "refill")
                    if second is not None:
                        # Exercise Stop while that worker is still live, then restart.
                        self.stop_during_worker(fixtures[1])
                        self.restart(expected_workers={second["id"]})
                    self.duplicate_delivery()
            else:
                for name in ("completion", "refill", "duplicate-delivery", "stop-with-active-worker", "restart"):
                    self._step(name).close("skipped", "first dispatch was not observed")
        except (CanaryError, OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc) if isinstance(exc, CanaryError) else f"invalid observation or command failure ({type(exc).__name__})"
            status = "unproven" if isinstance(exc, DeadlineExpired) else "failed"
            if self.steps and self.steps[-1].finished_at is None:
                self.steps[-1].close(status, error)
        finally:
            # Safety cleanup has one separate finite allowance, even after observation expiry.
            self.cleanup_deadline = self.clock() + CLEANUP_SECONDS
            cleanup = self._step("final-stop")
            try:
                stopped = self._driver("stop", "--project", self.project, cleanup=True)
                status = self._status(cleanup=True)
                if (stopped.get("status") != "stopped" or status["enabled"] is not False
                        or status["scheduler"]["enabled_heartbeats"] != 0
                        or status["scheduler"]["enabled_wakes"] != 0
                        or status["scheduler"]["enabled_review_wakes"] != 0):
                    raise CanaryError("final Stop did not confirm disabled dispatch/jobs")
                cleanup.close("pass", "final Stop confirmed disabled dispatch/jobs; no worker termination requested")
            except (CanaryError, OSError, ValueError, KeyError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, CanaryError) else f"cleanup command failure ({type(exc).__name__})"
                cleanup.close("failed", reason)
                error = f"{error}; final Stop failed: {reason}" if error else f"final Stop failed: {reason}"
        for name, reason in UNPROVEN_CHECKS.items():
            self._step(name).close("unproven", reason)
        statuses = {s.status for s in self.steps}
        result = "failed" if "failed" in statuses else "unproven" if statuses - {"pass"} else "pass"
        return {"schema": SCHEMA, "project": self.project, "dry_run": self.dry_run,
                "evidence_kind": "simulation" if self.dry_run else "observations",
                "started_at": self.started_at, "finished_at": self.wall_clock(), "result": result, "error": error,
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
                 duplicate_heartbeat_on_restart: bool = False, opens_pr: bool = True,
                 clock: Callable[[], float] = time.time):
        self.knobs = dict(dispatch_after=dispatch_after, complete_after=complete_after, refill=refill,
                          suppress_duplicates=suppress_duplicates, preserve_on_stop=preserve_on_stop,
                          duplicate_heartbeat_on_restart=duplicate_heartbeat_on_restart, opens_pr=opens_pr)
        self.enabled, self.heartbeats, self.wakes, self.polls, self.starts = False, 0, 0, 0, 0
        self.issues: list[int] = []
        self.workers: list[dict] = []
        self.events: set[str] = set()
        self.calls: list[list[str]] = []
        self.clock = clock
        self.timeouts: list[float] = []

    def _dispatch(self) -> None:
        launched = {w["issue"] for w in self.workers}
        pending = [n for n in self.issues if n not in launched]
        running = any(w["state"] == "running" for w in self.workers)
        if not pending or running or (launched and not self.knobs["refill"]):
            return  # one worker at a time, like a max_workers: 1 project
        if self.polls >= self.knobs["dispatch_after"] * (len(launched) + 1):
            self.workers.append({"id": f"worker-{pending[0]}", "agent": "lane-a", "issue": pending[0],
                                 "state": "running", "since_poll": self.polls, "pid": pending[0],
                                 "started_at": self.clock()})

    def _advance(self) -> None:
        self.polls += 1
        for worker in self.workers:
            if worker["state"] == "running" and self.polls - worker["since_poll"] >= self.knobs["complete_after"]:
                worker["state"], worker["exit_code"] = "exited", 0
                worker["finished_at"] = self.clock()
        if self.enabled:
            self._dispatch()

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        self.timeouts.append(timeout)
        out: Any
        if argv[0] == "gh":
            if argv[1:3] == ["issue", "create"]:
                self.issues.append(len(self.issues) + 101)
                project = argv[argv.index("--repo") + 1]
                out = f"https://github.com/{project}/issues/{self.issues[-1]}"
            elif argv[1:3] == ["pr", "list"]:
                out = json.dumps([{"number": 7, "headRefOid": "f" * 40, "state": "OPEN"}]
                                 if self.knobs["opens_pr"] else [])
            else:
                return subprocess.CompletedProcess(argv, 1, "", "unsupported fake gh command")
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        op = argv[argv.index("--config") + 2]
        project = argv[argv.index("--project") + 1]
        if op == "start":
            self.enabled = True
            self.starts += 1
            # A faulty restart would create a second heartbeat instead of reusing the paused one.
            self.heartbeats = 2 if self.starts > 1 and self.knobs["duplicate_heartbeat_on_restart"] else 1
            self.wakes += 1
            out = {"project": project, "status": "started", "heartbeat": {"enabled": True, "heartbeat_job_id": "hb-1"}, "wake": {"wake_job_id": f"wake-{self.wakes}"}}
        elif op == "stop":
            self.enabled, self.heartbeats, self.wakes = False, 0, 0
            if not self.knobs["preserve_on_stop"]:
                self.workers = [w for w in self.workers if w["state"] != "running"]
            out = {"project": project, "status": "stopped", "scheduler": {"paused_job_ids": ["hb-1"]}}
        elif op == "event":
            event_id = argv[argv.index("--event-id") + 1]
            duplicate = event_id in self.events
            self.events.add(event_id)
            if not duplicate or not self.knobs["suppress_duplicates"]:
                self.wakes += 1
            out = {"wakeAgent": not duplicate or not self.knobs["suppress_duplicates"]}
        elif op == "status":
            self._advance()
            out = {"project": project, "enabled": self.enabled, "last_error": None,
                   "workers": [dict(w) for w in self.workers],
                   "scheduler": {"enabled_heartbeats": self.heartbeats, "enabled_wakes": self.wakes,
                                 "enabled_review_wakes": 0}}
        else:
            return subprocess.CompletedProcess(argv, 1, json.dumps({"status": "error", "reason": "unknown op"}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(out) + "\n", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--fixtures", type=int, default=2)
    parser.add_argument("--bound-seconds", type=int, default=900,
                        help="whole-run observation budget; final Stop has an additional 30-second allowance")
    parser.add_argument("--dry-run", action="store_true", help="use scripted fakes; never call gh or the Driver")
    args = parser.parse_args(argv)
    runner: Runner = FakeDriver() if args.dry_run else _run
    try:
        canary = Canary(args.config, args.project, fixtures=args.fixtures, bound_seconds=args.bound_seconds,
                        runner=runner, dry_run=args.dry_run, sleep=(lambda _s: None) if args.dry_run else time.sleep)
        evidence = canary.run()
    except (CanaryError, OSError, ValueError, KeyError, TypeError) as exc:
        evidence = {"schema": SCHEMA, "project": args.project, "dry_run": args.dry_run,
                    "evidence_kind": "simulation" if args.dry_run else "observations",
                    "result": "failed", "error": f"canary setup failed ({type(exc).__name__})", "steps": []}
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": evidence["result"], "evidence": str(args.evidence)}))
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
