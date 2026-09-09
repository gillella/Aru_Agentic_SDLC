from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import driver, execution, kernel, scheduler
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import State, write_json


@pytest.fixture
def config(tmp_path):
    home = tmp_path / "home"
    raw = {
        "version": 1, "hermes_home": str(home), "hermes_repo": str(tmp_path / "runtime"),
        "state_dir": str(home / "state" / "aru_project_driver"), "kernel_root": str(tmp_path / "kernel"),
        "projects": {"owner/repo": {"repo_dir": str(tmp_path / "repo"), "lanes": ["agent-one"]}},
        "lanes": {"agent-one": {
            "family": "openai-codex", "capacity_key": "account-one", "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "pass", "{prompt}"],
            "capacity_command": [sys.executable, "-c", "pass"],
            "probe_command": [sys.executable, "-c", "pass"],
        }},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return Config(path)


def test_start_proves_scheduler_before_opening_stop_gate(config, monkeypatch):
    state = State(config.state_dir)
    def fail(*args, **kwargs):
        assert not state.project("owner/repo")["enabled"]
        raise scheduler.SchedulerError("synthetic incompatible runtime")
    monkeypatch.setattr(scheduler, "ensure_heartbeat", fail)
    with pytest.raises(scheduler.SchedulerError):
        driver.start(config, "owner/repo")
    assert not state.project("owner/repo")["enabled"]


def test_repeated_start_and_restart_preserve_one_identity(config, monkeypatch):
    wakes = []
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: {"heartbeat_job_id": "one"})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: wakes.append(k["event_key"]) or {})
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: {})
    driver.start(config, "owner/repo")
    driver.start(config, "owner/repo")
    assert wakes == ["start:1", "start:1"]
    driver.stop(config, "owner/repo")
    driver.start(config, "owner/repo")
    assert wakes[-1] == "start:2"


def test_stop_gate_remains_closed_even_if_scheduler_pause_fails(config, monkeypatch):
    state = State(config.state_dir)
    data = state.project("owner/repo")
    data["enabled"] = True
    state.save("owner/repo", data)
    def fail(*args, **kwargs):
        assert not state.project("owner/repo")["enabled"]
        raise scheduler.SchedulerError("synthetic scheduler outage")
    monkeypatch.setattr(scheduler, "stop_project", fail)
    result = driver.stop(config, "owner/repo")
    assert result["status"] == "partial"
    assert result["stop_intent_persisted"] and result["spawn_barrier_verified"]
    assert result["scheduler"] == {"verified": False}
    assert not state.project("owner/repo")["enabled"]


@pytest.mark.parametrize("operation", ["start", "stop", "status", "tick", "reconcile", "event", "handoff"])
def test_profile_binding_rejects_other_config_on_all_entrypoints(config, monkeypatch, capsys, operation):
    (config.state_dir / "binding.json").unlink()
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "scheduler_status", lambda *a, **k: {})
    suffix = [operation, "--project", "owner/repo"]
    if operation == "event":
        suffix += ["--event-id", "test"]
    if operation == "handoff":
        suffix += ["--source-issue", "1"]
    assert driver.main(["--config", str(config.path), *suffix]) == 0
    alternate = config.path.with_name("alternate.json")
    alternate.write_text(config.path.read_text())
    assert driver.main(["--config", str(alternate), *suffix]) == 1
    assert "bound to another" in capsys.readouterr().out


def test_worker_config_can_load_existing_binding_during_parent_coordination(config):
    with State(config.state_dir).lock():
        assert Config(config.path).state_dir == config.state_dir


def test_project_lane_must_authorize_project_but_unused_lanes_are_allowed(config):
    raw = json.loads(config.path.read_text())
    raw["lanes"]["unused-agent"] = dict(raw["lanes"]["agent-one"])
    config.path.write_text(json.dumps(raw))
    assert "unused-agent" in Config(config.path).lanes
    raw["lanes"]["agent-one"]["projects"] = []
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="explicitly authorize"):
        Config(config.path)


def test_state_path_cannot_split_the_same_profile_into_two_coordinators(config):
    raw = json.loads(config.path.read_text())
    raw["state_dir"] += "-other"
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="shared locking"):
        Config(config.path)


def test_stopped_cli_outputs_machine_readable_gate_without_runtime_import(config):
    result = subprocess.run([
        sys.executable, driver.__file__, "--config", str(config.path), "tick", "--project", "owner/repo",
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"wakeAgent": False, "reason": "project stopped"}
    assert not result.stderr


@pytest.mark.parametrize("argv", [[], [""], [1], ["python", "bad\0value"]])
def test_invalid_commands_fail_closed(config, argv):
    raw = json.loads(config.path.read_text())
    raw["lanes"]["agent-one"]["capacity_command"] = argv
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="argument array"):
        Config(config.path)


def test_empty_argument_values_are_preserved_without_shell_evaluation(config):
    raw = json.loads(config.path.read_text())
    raw["lanes"]["agent-one"]["probe_command"] = ["claude", "--tools", "", "$(not-a-command)"]
    config.path.write_text(json.dumps(raw))
    parsed = Config(config.path)
    assert parsed.lanes["agent-one"]["probe_command"][-2:] == ["", "$(not-a-command)"]


@pytest.mark.parametrize("timeout", [0, -1, 86401, True, 1.5, "60", None])
def test_execution_deadline_requires_a_bounded_positive_integer(config, timeout):
    raw = json.loads(config.path.read_text())
    raw["lanes"]["agent-one"]["execution_timeout_seconds"] = timeout
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="execution_timeout_seconds"):
        Config(config.path)


def test_handoff_command_has_explicit_source_issue_and_proof_mode(config):
    args = driver.parser().parse_args([
        "--config", str(config.path), "handoff", "--project", "owner/repo",
        "--source-issue", "42", "--dependency-event",
    ])
    assert args.operation == "handoff"
    assert args.source_issue == 42 and args.dependency_event is True


@pytest.fixture
def fake_gh(tmp_path):
    directory = tmp_path / "tools"
    directory.mkdir()
    gh = directory / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    return gh


@pytest.fixture
def no_discoverable_gh(tmp_path, monkeypatch):
    monkeypatch.delenv(kernel.GH_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-on-path"))
    monkeypatch.setattr(kernel, "GH_LOCATIONS", ())


def launchd_like_env(tmp_path):
    # launchd/cron start jobs with a minimal PATH and no shell startup files.
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"}


def test_gh_resolution_prefers_explicit_configuration_and_fails_closed(fake_gh, tmp_path, no_discoverable_gh, monkeypatch):
    with pytest.raises(kernel.KernelAdapterError, match="'gh' is not executable"):
        kernel.resolve_gh()
    monkeypatch.setenv(kernel.GH_ENV, str(fake_gh))
    assert kernel.resolve_gh() == fake_gh
    unexecutable = tmp_path / "gh.txt"
    unexecutable.write_text("")
    for bad in (str(unexecutable), "gh", str(tmp_path / "absent")):
        with pytest.raises(kernel.KernelAdapterError, match="not an executable file"):
            kernel.resolve_gh(bad)
    monkeypatch.delenv(kernel.GH_ENV)
    monkeypatch.setattr(kernel, "GH_LOCATIONS", (str(tmp_path / "missing"), str(fake_gh.parent)))
    assert kernel.resolve_gh() == fake_gh


def test_bounded_environment_leads_with_validated_gh_and_keeps_inherited_entries(fake_gh):
    env = kernel.bounded_environment(fake_gh, {"PATH": "/usr/bin:/custom", "HOME": "/h"})
    entries = env["PATH"].split(os.pathsep)
    assert entries[0] == str(fake_gh.parent) and entries[-1] == "/custom"
    assert entries.count("/usr/bin") == 1 and len(entries) == len(set(entries))
    assert env[kernel.GH_ENV] == str(fake_gh) and env["HOME"] == "/h"


def test_kernel_bridge_refuses_to_spawn_without_executable_gh(tmp_path, no_discoverable_gh, monkeypatch):
    spawned = []
    monkeypatch.setattr(kernel.subprocess, "run", lambda *a, **k: spawned.append(a))
    adapter = kernel.KernelAdapter(tmp_path, tmp_path, "owner/repo")
    with pytest.raises(kernel.KernelAdapterError, match="'gh' is not executable"):
        adapter.snapshot()
    assert spawned == []


def test_kernel_bridge_receives_bounded_environment(fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv(kernel.GH_ENV, str(fake_gh))
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"result": {"ok": True}}), "")
    monkeypatch.setattr(kernel.subprocess, "run", fake_run)
    assert kernel.KernelAdapter(tmp_path, tmp_path, "owner/repo").revalidate(1) == {"ok": True}
    assert seen["command"][:3] == [sys.executable, kernel.__file__, "--kernel-bridge"]
    assert seen["shell"] is False
    assert seen["env"]["PATH"].split(os.pathsep)[0] == str(fake_gh.parent)
    assert seen["env"][kernel.GH_ENV] == str(fake_gh)


def test_generated_wrapper_bakes_gh_environment_and_fails_job_on_degraded_precheck(config, fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv(kernel.GH_ENV, str(fake_gh))
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'path': os.environ['PATH'].split(os.pathsep),"
        " 'gh': os.environ.get('ARU_DRIVER_GH')}))\n"
        "print(os.environ['PROBE_GATE'])\n"
    )
    wrapper = config.hermes_home / "scripts" / scheduler._wrapper(config.hermes_home, "owner/repo", config.path, probe)
    assert "{" not in "".join(scheduler._environment_source(*scheduler._executable_environment()))

    def run(gate):
        env = {**launchd_like_env(tmp_path), "PROBE_GATE": gate}
        return subprocess.run([sys.executable, str(wrapper)], env=env, capture_output=True, text=True, timeout=30)
    healthy = run(json.dumps({"wakeAgent": False}))
    assert healthy.returncode == 0 and not healthy.stderr
    seen, gate = [json.loads(line) for line in healthy.stdout.splitlines() if line.strip()]
    assert seen["argv"] == ["--config", str(config.path), "tick", "--project", "owner/repo"]
    assert seen["path"][0] == str(fake_gh.parent) and "/usr/bin" in seen["path"]
    assert seen["gh"] == str(fake_gh)
    assert gate == {"wakeAgent": False}
    for status in ("degraded", "error"):
        failed = run(json.dumps({"wakeAgent": False, "status": status, "reason": "synthetic"}))
        assert failed.returncode == 1
        assert json.loads(failed.stdout.splitlines()[-1])["status"] == status
    assert run("not json").returncode == 0


def test_heartbeat_under_minimal_path_reaches_bounded_precheck_and_reports_degraded(config, fake_gh, tmp_path, monkeypatch):
    state = State(config.state_dir)
    data = state.project("owner/repo")
    data["enabled"] = True
    state.save("owner/repo", data)
    monkeypatch.setenv(kernel.GH_ENV, str(fake_gh))
    script = scheduler._wrapper(config.hermes_home, "owner/repo", config.path, Path(driver.__file__))
    wrapper = config.hermes_home / "scripts" / script
    fake_gh.unlink()  # the baked CLI disappears before the scheduler fires

    def heartbeat():
        return subprocess.run([sys.executable, str(wrapper)], env=launchd_like_env(tmp_path),
                              capture_output=True, text=True, timeout=60)
    first = heartbeat()
    gate = json.loads(first.stdout.splitlines()[-1])
    assert first.returncode == 1 and not first.stderr
    assert gate["status"] == "degraded" and "GitHub CLI" in gate["reason"]
    assert "No such file" not in gate["reason"]
    assert gate["wakeAgent"] is True
    recorded = state.project("owner/repo")
    assert "GitHub CLI" in recorded["last_error"] and recorded["cooldown_until"] > 0
    # The cooling-down heartbeat still reports the recorded failure instead of `ok`.
    second = heartbeat()
    gate = json.loads(second.stdout.splitlines()[-1])
    assert second.returncode == 1
    assert gate == {"wakeAgent": False, "reason": "cooldown or pending activation",
                    "status": "degraded", "last_error": recorded["last_error"]}


def test_stopped_or_healthy_prechecks_are_not_marked_degraded(config):
    state = State(config.state_dir)
    data = state.project("owner/repo")
    data["last_error"] = "stale"
    state.save("owner/repo", data)
    quiet = {"wakeAgent": False, "reason": "project stopped"}
    assert driver._honest_health(config, "owner/repo", quiet) == quiet
    data["enabled"] = True
    state.save("owner/repo", data)
    assert driver._honest_health(config, "owner/repo", quiet)["status"] == "degraded"
    explicit = {"wakeAgent": False, "status": "busy"}
    assert driver._honest_health(config, "owner/repo", explicit) == explicit
    data["last_error"] = None
    state.save("owner/repo", data)
    assert driver._honest_health(config, "owner/repo", quiet) == quiet


def test_generated_environment_bakes_each_directory_once(fake_gh, tmp_path, monkeypatch):
    # Discovered directory that is also a well-known location appears once, first.
    monkeypatch.setattr(kernel, "GH_LOCATIONS", (str(fake_gh.parent), "/usr/local/bin", "/usr/bin"))
    monkeypatch.setenv(kernel.GH_ENV, str(fake_gh))
    entries, gh = scheduler._executable_environment()
    assert gh == fake_gh and entries[0] == str(fake_gh.parent)
    assert len(entries) == len(set(entries)) and entries.count("/usr/bin") == 1
    entries_line = scheduler._environment_source(entries, gh)[1]
    assert entries_line.count(str(fake_gh.parent) + "'") == 1  # baked literal names it once
    # Without discovery the fixed lists are still deduplicated.
    monkeypatch.delenv(kernel.GH_ENV)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setattr(kernel, "GH_LOCATIONS", ("/usr/bin", "/usr/local/bin"))
    entries, gh = scheduler._executable_environment()
    assert gh is None and entries == ["/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin", "/sbin"]


@pytest.mark.parametrize("gate", ["valid", "stale", "denied", "completed", "stopped", "lane-family", "receipt-head"])
def test_review_child_revalidates_before_actual_execution_and_uses_completion_wake(config, monkeypatch, gate):
    state = State(config.state_dir)
    data = state.project("owner/repo")
    data["enabled"] = gate != "stopped"
    state.save("owner/repo", data)
    worktree = Path(config.project("owner/repo")["repo_dir"]) / ".worktrees" / "review-pr-9"
    worktree.mkdir(parents=True)
    binding = {"repo": "owner/repo", "pr": 9, "head": "a" * 40, "issue": 1,
               "reviewer": "agent-one", "authority": "openai-codex", "reviewer_actor": "review-bot",
               "author": "writer", "author_actor": "author-user", "verdict": None}
    prompt = execution.prompt_for("owner/repo", 1, "agent-one", config.kernel_root,
                                  str(worktree), "review", 9, binding["head"], binding)
    assert "Review only: do not edit source" in prompt
    assert "before submission" in prompt and "review-bot" in prompt and binding["head"] in prompt
    capacity = state.capacity_path("account-one")
    capacity.parent.mkdir(parents=True)
    descriptor = os.open(capacity, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {"id": "review-worker", "repo": "owner/repo", "agent": "agent-one", "issue": 1,
              "capacity_key": "account-one", "state": "launching", "started_at": 1,
              "worktree": str(worktree), "kind": "review", "pr": 9, "head": binding["head"],
              "review": binding, "prompt": prompt}
    if gate == "lane-family":
        config.lanes["agent-one"]["family"] = "claude-code"
    elif gate == "receipt-head":
        record["head"] = "b" * 40
    write_json(state.worker_path(record["id"]), record)
    reads, wakes = [], []
    def current(number, expected):
        reads.append((number, expected))
        if gate in {"stale", "denied"}:
            raise kernel.KernelAdapterError("head changed" if gate == "stale" else "access denied")
        return {**binding, "verdict": "APPROVE" if gate == "completed" else None}
    monkeypatch.setattr(execution, "KernelAdapter", lambda *args: SimpleNamespace(
        review_binding=current, review_worktree=lambda binding: str(worktree)))
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *args, **kwargs: wakes.append(kwargs))
    result = execution.worker_main(config, record["id"], descriptor)
    receipt = state.worker(record["id"])
    assert result == (0 if gate == "valid" else 1)
    assert ("child_pid" in receipt) is (gate == "valid")
    assert receipt["state"] == "exited" and receipt["review"]["verdict"] is None
    assert not state.capacity_busy("account-one")
    assert len(wakes) == (0 if gate == "stopped" else 1)
    assert len(reads) == (0 if gate in {"stopped", "lane-family", "receipt-head"} else 1)


def _lock_holder(path):
    # A separate OS process owns the real flock until the test releases stdin.
    path.parent.mkdir(parents=True, exist_ok=True)
    script = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
    process = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    return process


def test_stop_does_not_wait_for_other_projects_coordination(config, monkeypatch):
    state = State(config.state_dir)
    data = state.project("owner/repo")
    data["enabled"] = True
    state.save("owner/repo", data)
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: {"enabled": False})
    holder = _lock_holder(config.state_dir / "coordination.lock")
    try:
        began = time.monotonic()
        result = driver.stop(config, "owner/repo", timeout_seconds=.5)
        assert time.monotonic() - began < 1
        assert holder.poll() is None  # Stop did not terminate or wait out the other project.
        assert result["status"] == "stopped" and result["spawn_barrier_verified"]
        assert not state.project("owner/repo")["enabled"]
        state.save("owner/repo", data)  # Its old in-flight snapshot can still finish.
        assert not state.project("owner/repo")["enabled"]
    finally:
        holder.communicate(timeout=3)


def test_stop_fence_is_partial_until_inflight_spawn_barrier_is_settled(config):
    from aru_project_driver.state import key

    state = State(config.state_dir)
    holder = _lock_holder(config.state_dir / "project-locks" / f"{key('owner/repo')}.spawn.lock")
    try:
        began = time.monotonic()
        result = driver.stop(config, "owner/repo", timeout_seconds=.15)
        assert time.monotonic() - began < 1
        assert result["status"] == "partial" and result["stop_intent_persisted"]
        assert result["spawn_barrier_verified"] is False
        assert result["scheduler"] == {"verified": False}
        assert not state.project("owner/repo")["enabled"]
    finally:
        holder.communicate(timeout=3)


def test_start_begun_before_stop_cannot_acknowledge_intervening_nonce(config, monkeypatch):
    import contextlib

    state = State(config.state_dir)
    original = State.lock
    @contextlib.contextmanager
    def stop_before_lock(self, **kwargs):
        state.request_stop("owner/repo")
        with original(self, **kwargs):
            yield
    monkeypatch.setattr(State, "lock", stop_before_lock)
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: pytest.fail("stale Start scheduled"))
    with pytest.raises(DriverError, match="superseded"):
        driver.start(config, "owner/repo")
    assert not state.project("owner/repo")["enabled"]


def test_stop_during_heartbeat_setup_defeats_start(config, monkeypatch):
    state = State(config.state_dir)
    def heartbeat(*a, **k):
        state.request_stop("owner/repo")
        return {"heartbeat_job_id": "one"}
    monkeypatch.setattr(scheduler, "ensure_heartbeat", heartbeat)
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("stale Start woke"))
    with pytest.raises(DriverError, match="superseded"):
        driver.start(config, "owner/repo")
    assert not state.project("owner/repo")["enabled"]


def test_stuck_native_stop_and_status_return_truthful_partial_state(config, monkeypatch):
    state = State(config.state_dir)
    def stuck(*a, **k):
        # A broad native error handler must not swallow the operation deadline.
        try:
            time.sleep(10)
        except Exception:
            time.sleep(10)
    monkeypatch.setattr(scheduler, "stop_project", stuck)
    began = time.monotonic()
    result = driver.stop(config, "owner/repo", timeout_seconds=.1)
    assert time.monotonic() - began < 1
    assert result["status"] == "partial" and result["spawn_barrier_verified"]
    assert result["scheduler"] == {"verified": False}
    assert not state.project("owner/repo")["enabled"]
    monkeypatch.setattr(scheduler, "scheduler_status", stuck)
    began = time.monotonic()
    result = driver.status(config, "owner/repo", timeout_seconds=.1)
    assert time.monotonic() - began < 1
    assert result["status"] == "partial" and result["enabled"] is False
    assert result["scheduler"] == {"verified": False}


def test_cli_partial_stop_is_nonzero(config, monkeypatch, capsys):
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: time.sleep(10))
    assert driver.main(["--config", str(config.path), "stop", "--project", "owner/repo",
                        "--timeout-seconds", ".05"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial" and result["stop_intent_persisted"]


def test_deadline_at_context_exit_cannot_keep_successful_stop_status(config, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def expire_at_exit(_):
        yield
        raise scheduler.SchedulerError("scheduler deadline expired")
    monkeypatch.setattr(scheduler, "bounded", expire_at_exit)
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: {"enabled": False})
    result = driver.stop(config, "owner/repo")
    assert result["status"] == "partial" and "deadline" in result["reason"]
    assert result["stop_intent_persisted"] and result["spawn_barrier_verified"]
    assert result["scheduler"]["verified"]  # Retain completed evidence, but never success.


def test_new_explicit_start_winning_control_supersedes_older_stop(config, monkeypatch):
    import contextlib

    state = State(config.state_dir)
    original = State.project_lock
    restarted = []
    @contextlib.contextmanager
    def restart_before_control(self, repo, *, spawn=False):
        if not spawn and not restarted:
            restarted.append(True)
            assert state.stop_nonce(repo) is not None
            driver.start(config, repo)
        with original(self, repo, spawn=spawn):
            yield
    monkeypatch.setattr(State, "project_lock", restart_before_control)
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: {"heartbeat_job_id": "one"})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "stop_project", lambda *a, **k: pytest.fail("older Stop paused newer Start"))
    result = driver.stop(config, "owner/repo")
    assert result["status"] == "partial" and "superseded" in result["reason"]
    assert state.project("owner/repo")["enabled"]
