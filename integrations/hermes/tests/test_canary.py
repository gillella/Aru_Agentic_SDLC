from __future__ import annotations

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import canary  # noqa: E402
from canary import Canary, CanaryError, FakeDriver  # noqa: E402


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "hermes_home": str(tmp_path / "home"),
                                "projects": {"owner/repo": {"repo_dir": str(tmp_path / "repo")}}}))
    return path


def operation(argv):
    return " ".join(argv[1:3]) if argv[0] == "gh" else argv[argv.index("--config") + 2]


def run(config, fake, *, bound=60, intercept=None, timer=None, **kwargs):
    timer = timer if timer is not None else {"now": 1000.0, "wall_offset": 0.0, "sleeps": []}
    def tick(seconds):
        timer["now"] += seconds
        timer["sleeps"].append(seconds)
    wall = lambda: timer["now"] + timer["wall_offset"]  # noqa: E731
    fake.clock = wall
    def execute(argv, *, timeout):
        return intercept(argv, timeout, fake, timer) if intercept else fake(argv, timeout=timeout)
    return Canary(config, "owner/repo", fixtures=2, bound_seconds=bound, poll_seconds=10,
                  runner=execute, clock=lambda: timer["now"], wall_clock=wall, sleep=tick,
                  dry_run=True, **kwargs).run()


def by_name(evidence):
    return {step["name"]: step for step in evidence["steps"]}


def response(result, payload):
    return CompletedProcess(result.args, result.returncode, json.dumps(payload), result.stderr)


def test_observations_do_not_claim_governed_completion_or_live_acceptance(config):
    fake = FakeDriver()
    evidence = run(config, fake)
    steps = by_name(evidence)
    assert evidence["result"] == "unproven" and evidence["error"] is None
    assert evidence["evidence_kind"] == "simulation"
    assert steps["fixture-1"]["ids"]["issue"] == 101 and steps["fixture-2"]["ids"]["issue"] == 102
    assert steps["start"]["ids"] == {"heartbeat_job_id": "hb-1", "wake_job_id": "wake-1"}
    assert steps["dispatch-1"]["ids"]["worker_id"] == "worker-101"
    assert steps["dispatch-1"]["ids"]["pid"] == 101
    assert steps["completion-101"]["ids"]["pr"] == 7 and len(steps["completion-101"]["ids"]["head"]) == 40
    assert steps["completion-101"]["status"] == steps["refill"]["status"] == "unproven"
    assert steps["refill"]["ids"]["worker_id"] == "worker-102"
    assert steps["duplicate-delivery"]["ids"]["second"] is False
    assert steps["stop-with-active-worker"]["status"] == steps["restart"]["status"] == "pass"
    assert all(steps[name]["status"] == "unproven" for name in canary.UNPROVEN_CHECKS)
    assert all(step["finished_at"] >= step["started_at"] for step in evidence["steps"])
    assert [operation(c) for c in fake.calls[-2:]] == ["stop", "status"]
    assert steps["final-stop"]["status"] == "pass" and not fake.enabled
    assert [operation(c) for c in fake.calls if c[0] == "gh"] == ["issue create", "issue create", "pr list"]


def test_no_dispatch_uses_one_budget_and_caps_final_sleep(config):
    timer = {"now": 1000.0, "wall_offset": 0.0, "sleeps": []}
    fake = FakeDriver(dispatch_after=1000)
    evidence = run(config, fake, bound=25, timer=timer)
    steps = by_name(evidence)
    assert evidence["result"] == "unproven"
    assert steps["dispatch-1"]["status"] == "unproven" and "whole-run 25s" in steps["dispatch-1"]["reason"]
    assert {steps[n]["status"] for n in ("completion", "refill", "duplicate-delivery", "restart")} == {"skipped"}
    assert timer["now"] == 1025 and timer["sleeps"] == [10, 10, 5]
    assert fake.timeouts[-2:] == [30, 30]  # One shared cleanup allowance; these fakes consume no time.


def test_missing_refill_cannot_start_event_test_after_shared_deadline(config):
    timer = {"now": 1000.0, "wall_offset": 0.0, "sleeps": []}
    fake = FakeDriver(refill=False)
    evidence = run(config, fake, timer=timer)
    steps = by_name(evidence)
    assert steps["completion-101"]["ids"]["pr"] == 7
    assert steps["refill"]["status"] == steps["duplicate-delivery"]["status"] == "unproven"
    assert timer["now"] == 1060  # Completion and refill did not each receive a new 60 seconds.
    assert "event" not in [operation(c) for c in fake.calls]
    assert evidence["result"] == "unproven" and steps["final-stop"]["status"] == "pass"


@pytest.mark.parametrize("knob,step,fragment", [
    ({"suppress_duplicates": False}, "duplicate-delivery", "ambiguous"),
    ({"preserve_on_stop": False}, "stop-with-active-worker", "preserved=False"),
    ({"duplicate_heartbeat_on_restart": True}, "restart", "heartbeats=2"),
    ({"opens_pr": False}, "completion-101", "without a PR"),
])
def test_failure_paths_remain_failed(config, knob, step, fragment):
    evidence = run(config, FakeDriver(**knob))
    assert by_name(evidence)[step]["status"] == "failed"
    assert fragment in by_name(evidence)[step]["reason"]
    assert evidence["result"] == "failed"


@pytest.mark.parametrize("failure", ["timeout", "oserror", "bad-url"])
def test_failure_after_first_fixture_retains_partial_evidence(config, failure):
    def intercept(argv, timeout, fake, timer):
        if operation(argv) == "issue create" and fake.issues:
            if failure == "timeout":
                raise TimeoutExpired(argv, timeout, output="private output")
            if failure == "oserror":
                raise OSError("private path")
            return CompletedProcess(argv, 0, "https://github.com/other/repo/issues/102", "")
        return fake(argv, timeout=timeout)
    fake = FakeDriver()
    evidence = run(config, fake, intercept=intercept)
    steps = by_name(evidence)
    assert steps["fixture-1"]["ids"]["issue"] == 101
    assert steps["fixture-2"]["finished_at"] is not None
    assert evidence["result"] == ("unproven" if failure == "timeout" else "failed")
    assert "private" not in json.dumps(evidence)
    assert steps["final-stop"]["status"] == "pass" and not fake.enabled


@pytest.mark.parametrize("payload", [None, {}, [{"number": 7, "headRefOid": "stale", "state": "OPEN"}],
                                      [{"number": True, "headRefOid": "f" * 40, "state": "OPEN"}]])
def test_malformed_pr_evidence_retains_dispatch_ids(config, payload):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        return response(result, payload) if operation(argv) == "pr list" else result
    evidence = run(config, FakeDriver(), intercept=intercept)
    steps = by_name(evidence)
    assert evidence["result"] == "failed"
    assert steps["completion-101"]["ids"] == {"issue": 101, "worker_id": "worker-101"}
    assert steps["dispatch-1"]["ids"]["worker_id"] == "worker-101"
    assert steps["completion-101"]["finished_at"] is not None


@pytest.mark.parametrize("change", ["stale", "future", "prepared", "missing-id", "duplicate-id", "bad-inventory"])
def test_stale_or_malformed_worker_data_never_proves_dispatch(config, change):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) != "status" or not fake.enabled:
            return result
        data = json.loads(result.stdout)
        for worker in data["workers"]:
            if change == "stale":
                worker["started_at"] = 900
            elif change == "future":
                worker["started_at"] = timer["now"] + 1000
            elif change == "prepared":
                worker["state"] = "prepared"
            elif change == "missing-id":
                worker.pop("id", None)
        if change == "duplicate-id" and data["workers"]:
            data["workers"].append(dict(data["workers"][0]))
        if change == "bad-inventory":
            data["workers"] = None
        return response(result, data)
    evidence = run(config, FakeDriver(), bound=25, intercept=intercept)
    assert by_name(evidence)["dispatch-1"]["status"] != "pass"
    assert evidence["result"] != "pass"
    assert by_name(evidence)["final-stop"]["status"] == "pass"


def test_nonzero_worker_exit_is_failed_even_if_pr_exists(config):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == "status":
            data = json.loads(result.stdout)
            for worker in data["workers"]:
                if worker["state"] == "exited":
                    worker["exit_code"] = 9
            return response(result, data)
        return result
    evidence = run(config, FakeDriver(), intercept=intercept)
    assert by_name(evidence)["completion-101"]["status"] == "failed"
    assert by_name(evidence)["completion-101"]["ids"]["exit_code"] == 9


def test_external_commands_receive_remaining_time_and_no_start_after_expiry(config):
    calls = []
    def intercept(argv, timeout, fake, timer):
        calls.append((operation(argv), timeout))
        if operation(argv) == "issue create":
            if timeout < 20:
                timer["now"] += timeout
                raise TimeoutExpired(argv, timeout)
            timer["now"] += 20
        return fake(argv, timeout=timeout)
    evidence = run(config, FakeDriver(), bound=25, intercept=intercept)
    assert calls == [("issue create", 25), ("issue create", 5), ("stop", 30), ("status", 30)]
    assert by_name(evidence)["fixture-1"]["ids"]["issue"] == 101
    assert "start" not in by_name(evidence)
    assert evidence["result"] == "unproven"


def test_late_command_response_cannot_pass(config):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == "issue create" and len(fake.issues) == 2:
            timer["now"] += timeout + 1
        return result
    evidence = run(config, FakeDriver(), intercept=intercept)
    steps = by_name(evidence)
    assert steps["fixture-1"]["ids"]["issue"] == 101
    assert steps["fixture-2"]["status"] == "unproven"
    assert "exceeded deadline" in evidence["error"]
    assert steps["final-stop"]["status"] == "pass"


def test_cleanup_budget_shared_by_stop_and_readback_preserves_original_failure(config):
    calls = []
    def intercept(argv, timeout, fake, timer):
        op = operation(argv)
        calls.append((op, timeout))
        if op == "start":
            return CompletedProcess(argv, 1, "not json", "private detail")
        if op == "stop":
            timer["now"] += 20
        if op == "status":
            timer["now"] += timeout
            raise TimeoutExpired(argv, timeout)
        return fake(argv, timeout=timeout)
    evidence = run(config, FakeDriver(), intercept=intercept)
    assert calls[-2:] == [("stop", 30), ("status", 10)]
    assert "driver start returned no JSON" in evidence["error"]
    assert "final Stop failed: cleanup command timed out" in evidence["error"]
    assert by_name(evidence)["final-stop"]["status"] == "failed"
    assert by_name(evidence)["fixture-1"]["ids"]["issue"] == 101


@pytest.mark.parametrize("offset", [-10000, 10000])
def test_wall_clock_changes_do_not_change_observation_budget(config, offset):
    timer = {"now": 1000.0, "wall_offset": 0.0, "sleeps": []}
    def intercept(argv, timeout, fake, timer):
        if operation(argv) == "status":
            timer["wall_offset"] = offset
        return fake(argv, timeout=timeout)
    evidence = run(config, FakeDriver(dispatch_after=1000), bound=25, intercept=intercept, timer=timer)
    assert timer["now"] == 1025 and timer["sleeps"] == [10, 10, 5]
    assert evidence["result"] == "unproven"


def test_project_and_bounds_are_validated(config):
    with pytest.raises(CanaryError, match="owner/repository"):
        Canary(config, "not a repo", runner=FakeDriver())
    with pytest.raises(CanaryError, match="positive"):
        Canary(config, "owner/repo", fixtures=0, runner=FakeDriver())
    with pytest.raises(CanaryError, match="positive"):
        Canary(config, "owner/repo", poll_seconds=float("nan"), runner=FakeDriver())


def test_real_runner_passes_remaining_timeout_to_subprocess(monkeypatch):
    calls = []
    def subprocess_run(argv, **kwargs):
        calls.append(kwargs)
        return CompletedProcess(argv, 0, "{}", "")
    monkeypatch.setattr(canary.subprocess, "run", subprocess_run)
    canary._run(["unused"], timeout=0.125)
    assert calls[0]["timeout"] == 0.125 and calls[0]["check"] is False


def test_cli_dry_run_writes_simulated_unproven_evidence(config, tmp_path, capsys):
    evidence_path = tmp_path / "evidence.json"
    assert canary.main(["--config", str(config), "--project", "owner/repo", "--evidence", str(evidence_path),
                        "--dry-run", "--bound-seconds", "60"]) == 1
    evidence = json.loads(evidence_path.read_text())
    assert evidence["schema"] == "aru.canary/v1" and evidence["dry_run"] is True
    assert evidence["evidence_kind"] == "simulation" and evidence["result"] == "unproven"
    assert json.loads(capsys.readouterr().out)["result"] == "unproven"


@pytest.mark.parametrize("op,field", [("stop", "scheduler"), ("start", "heartbeat")])
@pytest.mark.parametrize("malformed", [None, []])
def test_malformed_nested_stop_restart_responses_preserve_evidence(config, op, field, malformed):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == op and (op != "start" or fake.starts > 1):
            data = json.loads(result.stdout)
            data[field] = malformed
            return response(result, data)
        return result
    evidence = run(config, FakeDriver(), intercept=intercept)
    steps = by_name(evidence)
    assert evidence["result"] == "failed"
    assert steps["completion-101"]["ids"]["worker_id"] == "worker-101"
    assert all(step["finished_at"] is not None for step in evidence["steps"])


def test_cleanup_refuses_remaining_review_continuation(config):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == "status" and not fake.enabled:
            data = json.loads(result.stdout)
            data["scheduler"]["enabled_review_wakes"] = 1
            return response(result, data)
        return result
    evidence = run(config, FakeDriver(dispatch_after=1000), bound=1, intercept=intercept)
    assert evidence["result"] == "failed"
    assert by_name(evidence)["final-stop"]["status"] == "failed"
    assert "disabled dispatch/jobs" in evidence["error"]


@pytest.mark.parametrize("op", ["start", "stop", "status"])
def test_driver_observations_must_match_exact_target(config, op):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == op:
            data = json.loads(result.stdout)
            data["project"] = "other/repo"
            return response(result, data)
        return result
    evidence = run(config, FakeDriver(), intercept=intercept)
    assert evidence["result"] == "failed"
    assert "mismatched project" in evidence["error"]
    assert by_name(evidence)["fixture-1"]["ids"]["issue"] == 101


def test_restart_refuses_replacement_worker_even_with_same_count(config):
    def intercept(argv, timeout, fake, timer):
        result = fake(argv, timeout=timeout)
        if operation(argv) == "status" and fake.starts > 1:
            data = json.loads(result.stdout)
            data["workers"] = [{"id": "unexpected-worker", "state": "running"}]
            return response(result, data)
        return result
    evidence = run(config, FakeDriver(), intercept=intercept)
    assert by_name(evidence)["restart"]["status"] == "failed"
    assert "unexpected_workers=True" in by_name(evidence)["restart"]["reason"]
