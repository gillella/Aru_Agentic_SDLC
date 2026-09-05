from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import driver, scheduler
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


def test_profile_binding_rejects_other_config_on_all_entrypoints(config):
    driver._bind(config)
    alternate = config.path.with_name("alternate.json")
    alternate.write_text(config.path.read_text())
    with pytest.raises(DriverError, match="bound to another"):
        Config(alternate)


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
