from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import canary  # noqa: E402
from canary import Canary, CanaryError, FakeDriver  # noqa: E402


@pytest.fixture
def config(tmp_path):
    home = tmp_path / "home"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "hermes_home": str(home),
                                "projects": {"owner/repo": {"repo_dir": str(tmp_path / "repo")}}}))
    return path


def run(config, fake, **kwargs):
    clock = {"now": 1000.0}
    def tick(seconds):
        clock["now"] += seconds
    canary_run = Canary(config, "owner/repo", fixtures=2, bound_seconds=kwargs.pop("bound", 60), poll_seconds=10,
                        runner=fake, clock=lambda: clock["now"], sleep=tick, dry_run=True, **kwargs)
    return canary_run.run()


def by_name(evidence):
    return {step["name"]: step for step in evidence["steps"]}


def test_happy_path_records_every_step_with_exact_ids(config):
    fake = FakeDriver()
    evidence = run(config, fake)
    steps = by_name(evidence)
    assert evidence["result"] == "pass" and evidence["error"] is None
    assert steps["fixture-1"]["ids"]["issue"] == 101 and steps["fixture-2"]["ids"]["issue"] == 102
    assert steps["start"]["ids"] == {"heartbeat_job_id": "hb-1", "wake_job_id": "wake-1"}
    assert steps["dispatch-1"]["ids"] == {"worker_id": "worker-101", "agent": "lane-a", "issue": 101}
    assert steps["completion-101"]["ids"]["pr"] == 7 and len(steps["completion-101"]["ids"]["head"]) == 40
    assert steps["refill"]["ids"]["worker_id"] == "worker-102"  # dispatched with no operator command
    assert steps["duplicate-delivery"]["ids"]["second"] is False
    assert steps["stop-with-active-worker"]["status"] == "pass" and steps["restart"]["status"] == "pass"
    assert all(step["finished_at"] >= step["started_at"] for step in evidence["steps"])
    # The canary always ends with the project stopped.
    assert [c for c in fake.calls if "stop" in c][-1] == fake.calls[-1]
    assert not any(c[0] == "gh" and c[1:3] not in (["issue", "create"], ["pr", "list"]) for c in fake.calls)


def test_no_dispatch_within_bound_is_unproven_not_pass(config):
    fake = FakeDriver(dispatch_after=1000)
    evidence = run(config, fake, bound=30)
    steps = by_name(evidence)
    assert evidence["result"] == "unproven"
    assert steps["dispatch-1"]["status"] == "unproven" and "within 30s" in steps["dispatch-1"]["reason"]
    assert {steps[n]["status"] for n in ("completion", "refill", "duplicate-delivery", "restart")} == {"skipped"}
    assert fake.enabled is False  # stopped in finally


def test_missing_refill_is_unproven_and_later_steps_still_report(config):
    evidence = run(config, FakeDriver(refill=False))
    steps = by_name(evidence)
    assert steps["refill"]["status"] == "unproven"
    assert steps["duplicate-delivery"]["status"] == "pass"
    assert "stop-with-active-worker" not in steps  # no live worker to test Stop against
    assert evidence["result"] == "unproven"


@pytest.mark.parametrize("knob,step,fragment", [
    ({"suppress_duplicates": False}, "duplicate-delivery", "second wake"),
    ({"preserve_on_stop": False}, "stop-with-active-worker", "preserved=False"),
    ({"duplicate_heartbeat_on_restart": True}, "restart", "heartbeats=2"),
    ({"opens_pr": False}, "completion-101", "without a PR"),
])
def test_failure_paths_are_recorded_as_failed(config, knob, step, fragment):
    evidence = run(config, FakeDriver(**knob))
    steps = by_name(evidence)
    assert steps[step]["status"] == "failed" and fragment in steps[step]["reason"]
    assert evidence["result"] == "failed"


def test_driver_error_is_recorded_and_project_still_stopped(config):
    fake = FakeDriver()
    broken = lambda argv: fake(argv) if "start" not in argv else canary.subprocess.CompletedProcess(argv, 1, "not json", "boom")  # noqa: E731
    evidence = run(config, broken)
    assert evidence["result"] == "failed" and "driver start returned no JSON" in evidence["error"]
    assert fake.calls[-1][-3:] == ["stop", "--project", "owner/repo"]


def test_project_and_bounds_are_validated(config):
    with pytest.raises(CanaryError, match="owner/repository"):
        Canary(config, "not a repo", runner=FakeDriver())
    with pytest.raises(CanaryError, match="positive"):
        Canary(config, "owner/repo", fixtures=0, runner=FakeDriver())


def test_cli_dry_run_writes_evidence_and_never_touches_the_network(config, tmp_path, capsys):
    evidence_path = tmp_path / "evidence.json"
    assert canary.main(["--config", str(config), "--project", "owner/repo", "--evidence", str(evidence_path),
                        "--dry-run", "--bound-seconds", "60"]) == 0
    evidence = json.loads(evidence_path.read_text())
    assert evidence["schema"] == "aru.canary/v1" and evidence["dry_run"] is True and evidence["result"] == "pass"
    assert json.loads(capsys.readouterr().out)["result"] == "pass"
