"""Real lock contention cannot acknowledge or silently drop a delivery."""

import json
import threading
import time

import pytest

from test_driver import _lock_holder, config as driver_config
from aru_project_driver import driver, scheduler
from aru_project_driver.config import DriverBusy, DriverError
from aru_project_driver.state import State

REPO = "owner/repo"
config = driver_config


def enable(config):
    state = State(config.state_dir)
    data = state.project(REPO)
    data["enabled"] = True
    state.save(REPO, data)
    return state


def event_args(config, *, inline=True):
    return ["--config", str(config.path), "event", "--project", REPO,
            "--event-id", "contended-delivery", *(["--inline"] if inline else [])]


def test_short_contention_accepts_event_once_after_real_lock_release(config, capsys):
    state = enable(config)
    holder = _lock_holder(state.root / "coordination.lock")
    release = threading.Timer(.1, lambda: holder.communicate(timeout=3))
    release.start()
    try:
        assert driver.main(event_args(config)) == 0
        assert json.loads(capsys.readouterr().out)["accepted"] is True
        assert driver.main(event_args(config)) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "duplicate"
        assert len(state.project(REPO)["events"]) == 1
    finally:
        release.join(timeout=3)
        if holder.poll() is None:
            holder.communicate(timeout=3)


@pytest.mark.parametrize("inline", [True, False])
def test_long_contention_is_retryable_and_delivery_is_not_consumed(config, monkeypatch, capsys, inline):
    state = enable(config)
    wakes = []
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **kw: wakes.append(kw) or {})
    original = State.lock
    monkeypatch.setattr(State, "lock", lambda self, **kw: original(self, timeout_seconds=.05))
    holder = _lock_holder(state.root / "coordination.lock")
    try:
        assert driver.main(event_args(config, inline=inline)) == 75
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "busy" and result["retryable"] is True
        assert result["accepted"] is False and result["wakeAgent"] is False
        assert not state.has_event(REPO, "contended-delivery") and not wakes
    finally:
        holder.communicate(timeout=3)
    assert driver.main(event_args(config, inline=inline)) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    assert len(wakes) == (0 if inline else 1)
    assert state.has_event(REPO, "contended-delivery")


@pytest.mark.parametrize("operation", ["tick", "reconcile"])
def test_busy_activation_never_reports_success(config, capsys, operation):
    state = enable(config)
    holder = _lock_holder(state.root / "coordination.lock")
    try:
        assert driver.main(["--config", str(config.path), operation, "--project", REPO]) == 75
        result = json.loads(capsys.readouterr().out)
        assert result["retryable"] and not result["wakeAgent"]
    finally:
        holder.communicate(timeout=3)


def test_stop_during_contention_prevents_delivery_after_lock_release(config, monkeypatch, capsys):
    state = enable(config)
    holder = _lock_holder(state.root / "coordination.lock")
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **kw: pytest.fail("stopped event scheduled"))

    def stop_and_release():
        state.request_stop(REPO)
        holder.communicate(timeout=3)

    release = threading.Timer(.1, stop_and_release)
    release.start()
    try:
        assert driver.main(event_args(config, inline=False)) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "stopped" and not result["accepted"]
        assert not state.has_event(REPO, "contended-delivery")
    finally:
        release.join(timeout=3)
        if holder.poll() is None:
            holder.communicate(timeout=3)


def test_lock_retry_is_bounded_and_does_not_release_the_holder(config):
    state = State(config.state_dir)
    holder = _lock_holder(state.root / "coordination.lock")
    began = time.monotonic()
    try:
        with pytest.raises(DriverBusy):
            with state.lock(timeout_seconds=.05):
                pytest.fail("must not enter another coordinator's critical section")
        assert time.monotonic() - began < 1
        assert holder.poll() is None
    finally:
        holder.communicate(timeout=3)


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), True, "20"])
def test_invalid_lock_retry_bound_is_refused(config, timeout):
    with pytest.raises(DriverError, match="coordination timeout"):
        with State(config.state_dir).lock(timeout_seconds=timeout):
            pytest.fail("invalid timeout entered coordination")
