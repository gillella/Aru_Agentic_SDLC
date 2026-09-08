from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import driver, kernel, scheduler
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import State


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
    with pytest.raises(scheduler.SchedulerError):
        driver.stop(config, "owner/repo")
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
