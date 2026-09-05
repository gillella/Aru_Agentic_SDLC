from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import capacity, execution, scheduler
from aru_project_driver.config import Config, DriverError
from aru_project_driver.controller import Controller
from aru_project_driver.state import State, read_json, write_json


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repository"
    worktree = repo / ".worktrees" / "feat-issue-1"
    worktree.mkdir(parents=True)
    home = tmp_path / "hermes-home"
    runtime = tmp_path / "hermes-runtime"
    runtime.mkdir()
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    lanes = {}
    for identity in ("model-one", "model-two"):
        lanes[identity] = {
            "family": "openai-codex", "capacity_key": "same-subscription",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "pass", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        }
    raw = {
        "version": 1, "state_dir": str(home / "state" / "aru_project_driver"),
        "hermes_home": str(home), "hermes_repo": str(runtime), "kernel_root": str(kernel),
        "projects": {"owner/repo": {"repo_dir": str(repo), "lanes": list(lanes)}},
        "lanes": lanes,
    }
    path = tmp_path / "driver-config.json"
    path.write_text(json.dumps(raw))
    config = Config(path)
    state = State(config.state_dir)
    project = state.project("owner/repo")
    project["enabled"] = True
    state.save("owner/repo", project)
    return config, state, worktree


def reserve_worker(config, state, worktree, *, worker_id="test-worker"):
    capacity_path = state.capacity_path("same-subscription")
    capacity_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(capacity_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {
        "id": worker_id, "repo": "owner/repo", "agent": "model-one", "issue": 1,
        "kind": "implementation", "pr": None, "head": None,
        "worktree": str(worktree), "capacity_key": "same-subscription",
        "started_at": time.time(), "state": "launching", "pid": None,
        "prompt": "A local test task with no external operations.",
    }
    write_json(state.worker_path(worker_id), record)
    return descriptor, record


def wait_until(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail("local child process did not reach its expected state")


def test_bounded_preflight_timeout_and_missing_executable_fail_closed(tmp_path):
    with pytest.raises(DriverError, match="preflight unavailable"):
        execution.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1)
    with pytest.raises(DriverError, match="preflight unavailable"):
        execution.run_bounded([str(tmp_path / "missing-command")], tmp_path)


def test_cursor_capacity_ignores_unrelated_agent_executable(monkeypatch):
    process = SimpleNamespace(returncode=0, stdout="42 /usr/local/bin/agent\n")
    monkeypatch.setattr(capacity.subprocess, "run", lambda *a, **k: process)
    assert capacity.observe("xai-cursor")["available"] is True
    process.stdout += "43 /usr/local/bin/cursor-agent\n"
    assert capacity.observe("xai-cursor")["available"] is False


@pytest.mark.parametrize("payload,returncode", [
    ('{"available": "yes"}', 0), ('{"available": 1}', 0),
    ('{"available": true}', 1), ('[]', 0), ('not-json', 0),
])
def test_unknown_capacity_cannot_authorize_a_worker(setup, monkeypatch, payload, returncode):
    config, state, _ = setup
    monkeypatch.setattr(execution, "run_bounded", lambda *args: SimpleNamespace(
        stdout=payload, returncode=returncode,
    ))
    with pytest.raises(DriverError, match="capacity"):
        execution.availability(config, "owner/repo", "model-one", state)


def test_capacity_observation_does_not_expose_extra_fields_or_invent_quota(setup, monkeypatch):
    config, state, _ = setup
    monkeypatch.setattr(execution, "run_bounded", lambda *args: SimpleNamespace(
        stdout=json.dumps({"available": True, "reason": "idle", "credential": "secret"}),
        returncode=0,
    ))
    assert execution.availability(config, "owner/repo", "model-one", state) == {
        "available": True, "reason": "idle",
    }


def test_failed_probe_cools_down_other_models_on_the_same_subscription(setup, monkeypatch):
    config, state, _ = setup
    monkeypatch.setattr(execution, "run_bounded", lambda *args: SimpleNamespace(
        stdout="NOT OK", returncode=1,
    ))
    assert execution.probe(config, "owner/repo", "model-one", state) is False
    monkeypatch.setattr(execution, "run_bounded", lambda *args: pytest.fail(
        "shared-account cooldown must suppress another model's observer",
    ))
    result = execution.availability(config, "owner/repo", "model-two", state)
    assert result["available"] is False
    assert result["reset_at"] > time.time()


def test_launch_rejects_stopped_project_and_nonisolated_directory(setup):
    config, state, worktree = setup
    with pytest.raises(DriverError, match="isolated worktree"):
        execution.launch(config, "owner/repo", "model-one", 1, str(worktree.parents[1]))
    project = state.project("owner/repo")
    project["enabled"] = False
    state.save("owner/repo", project)
    with pytest.raises(DriverError, match="stopped"):
        execution.launch(config, "owner/repo", "model-one", 1, str(worktree))
    assert state.workers() == []


def test_failed_process_launch_preserves_receipt_and_releases_capacity(setup, monkeypatch):
    config, state, worktree = setup
    def failed_launch(*args, **kwargs):
        raise OSError("synthetic local process failure")
    monkeypatch.setattr(execution.subprocess, "Popen", failed_launch)
    with pytest.raises(DriverError, match="claim and worktree preserved"):
        execution.launch(config, "owner/repo", "model-one", 1, str(worktree))
    records = state.workers("owner/repo")
    assert len(records) == 1
    assert records[0]["state"] == "launch_failed"
    assert records[0]["issue"] == 1 and worktree.is_dir()
    assert state.capacity_busy("same-subscription") is False


def test_worker_receipt_is_durable_before_failed_wake_and_runtime_is_explicit(setup, monkeypatch):
    config, state, worktree = setup
    descriptor, record = reserve_worker(config, state, worktree)
    observed = []
    def unavailable_scheduler(*args, **kwargs):
        receipt = read_json(state.worker_path(record["id"]))
        assert receipt["state"] == "exited" and receipt["exit_code"] == 0
        assert state.capacity_busy("same-subscription") is False
        assert kwargs["hermes_repo"] == config.hermes_repo
        observed.append(receipt)
        raise scheduler.SchedulerError("synthetic native wake outage")
    monkeypatch.setattr(scheduler, "schedule_wake", unavailable_scheduler)
    assert execution.worker_main(config, record["id"], descriptor) == 0
    receipt = read_json(state.worker_path(record["id"]))
    assert len(observed) == 1
    assert receipt["state"] == "exited" and receipt["exit_code"] == 0
    assert receipt["wake_error"] == "SchedulerError"
    assert worktree.is_dir()


def test_stop_before_execution_keeps_receipt_and_does_not_schedule(setup, monkeypatch):
    config, state, worktree = setup
    descriptor, record = reserve_worker(config, state, worktree)
    project = state.project("owner/repo")
    project["enabled"] = False
    state.save("owner/repo", project)
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: pytest.fail(
        "a stopped project must not start a child",
    ))
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *args, **kwargs: pytest.fail(
        "a stopped project must not recreate an enabled wake",
    ))
    assert execution.worker_main(config, record["id"], descriptor) != 0
    receipt = read_json(state.worker_path(record["id"]))
    assert receipt["state"] == "exited"
    assert "stopped" in receipt["reason"]
    assert state.capacity_busy("same-subscription") is False


def test_worker_finishing_after_stop_does_not_recreate_a_wake(setup, monkeypatch):
    config, state, worktree = setup
    descriptor, record = reserve_worker(config, state, worktree)
    def finish_after_stop():
        project = state.project("owner/repo")
        project["enabled"] = False
        state.save("owner/repo", project)
        return 0
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        pid=12345, wait=finish_after_stop,
    ))
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *args, **kwargs: pytest.fail(
        "completion after Stop must not recreate a scheduler job",
    ))
    assert execution.worker_main(config, record["id"], descriptor) == 0
    receipt = read_json(state.worker_path(record["id"]))
    assert receipt["state"] == "exited" and receipt["exit_code"] == 0
    assert state.capacity_busy("same-subscription") is False


def test_worker_rejects_capacity_descriptor_from_another_device(setup, monkeypatch):
    config, state, worktree = setup
    descriptor, record = reserve_worker(config, state, worktree)
    actual_fstat = execution.os.fstat
    def different_device(fd):
        result = actual_fstat(fd)
        return SimpleNamespace(st_ino=result.st_ino, st_dev=result.st_dev + 1)
    monkeypatch.setattr(execution.os, "fstat", different_device)
    try:
        with pytest.raises(DriverError, match="capacity reservation"):
            execution.worker_main(config, record["id"], descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)
    assert read_json(state.worker_path(record["id"]))["state"] == "launching"


def test_real_detached_worker_holds_account_lock_and_keeps_failed_wake_receipt(setup, monkeypatch):
    config, state, worktree = setup
    entered = worktree / "entered"
    release = worktree / "release"
    raw = json.loads(config.path.read_text())
    raw["lanes"]["model-one"]["command"] = [
        sys.executable, "-c",
        "from pathlib import Path\nimport os,sys,time\n"
        "Path(sys.argv[1]).write_text(str(os.getsid(0)))\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.02)\n",
        str(entered), str(release), "{prompt}",
    ]
    config.path.write_text(json.dumps(raw))
    config = Config(config.path)
    monkeypatch.setenv("HERMES_HOME", str(config.hermes_home))
    # The configured empty temp runtime intentionally cannot create a native wake.
    launched = execution.launch(config, "owner/repo", "model-one", 1, str(worktree))
    try:
        wait_until(entered.exists)
        assert int(entered.read_text()) == launched["pid"]
        assert state.capacity_busy("same-subscription") is True
        assert execution.availability(config, "owner/repo", "model-two", state)["available"] is False
        with pytest.raises(DriverError, match="reserved by another worker"):
            execution.launch(config, "owner/repo", "model-two", 2, str(worktree))
        assert len(state.workers()) == 1
    finally:
        release.touch()
        wait_until(lambda: not state.capacity_busy("same-subscription"))
        wait_until(lambda: "wake_error" in read_json(state.worker_path(launched["id"])))
        os.waitpid(launched["pid"], 0)
    receipt = read_json(state.worker_path(launched["id"]))
    assert receipt["state"] == "exited" and receipt["exit_code"] == 0
    assert receipt["finished_at"] >= receipt["started_at"]
    assert receipt["wake_error"] == "SchedulerError"
    assert not (config.hermes_home / "cron" / "jobs.json").exists()


def test_other_project_holder_does_not_count_or_revive_a_stale_local_receipt(setup):
    config, state, worktree = setup
    stale = {
        "id": "old-worker", "repo": "owner/repo", "agent": "model-one",
        "issue": 1, "state": "running", "capacity_key": "same-subscription",
        "started_at": 1, "worktree": str(worktree),
    }
    current = {
        **stale, "id": "current-worker", "repo": "owner/other", "agent": "model-two",
        "issue": 2, "started_at": 2,
    }
    for record in (stale, current):
        write_json(state.worker_path(record["id"]), record)
    capacity = state.capacity_path("same-subscription")
    capacity.parent.mkdir(parents=True)
    capacity.write_text("current-worker\n")
    controller = Controller(config, sync_reviews=lambda *args: {})
    snapshot = {
        "issues": [{"number": 1, "status": "In Review", "agents": ["model-one"]}],
        "prs": [{"number": 7, "issues": [1], "author_agent": "model-one"}],
    }
    adapter = SimpleNamespace(next_work=lambda identity: {
        "type": "merge", "pr": 7, "head": "a" * 40,
    })
    with capacity.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert controller._worker_count("owner/repo") == 0
        assert controller._worker_count("owner/other") == 1
        actions, resumes = controller._existing("owner/repo", snapshot, adapter, [])
    assert resumes == []
    assert len(actions) == 1 and actions[0]["type"] == "merge"
    assert actions[0]["issue"] == 1
