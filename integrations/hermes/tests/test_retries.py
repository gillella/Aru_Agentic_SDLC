"""Failure/restart probes use synthetic workers; no provider or GitHub calls."""
import os
import json

import pytest

from test_controller import Harness, REPO, HEAD, pr
from test_controller import harness as harness
from test_permissions import scoped as scoped
from aru_project_driver import retries, permissions, quota
from aru_project_driver.config import Config, DriverError
from aru_project_driver.controller import Controller
from aru_project_driver.state import State, write_json


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path)
    value.one_lane()
    value.kernel.issues = value.kernel.issues[:1]
    yield value
    value.close()


def finish(h, code=1, *, lost=False):
    h.clock += .01
    os.close(h.descriptors.pop())
    record = h.state.worker(h.launched[-1]["id"])
    if not lost:
        record.update(state="exited", exit_code=code, finished_at=h.clock, child_pid=123)
    write_json(h.state.worker_path(record["id"]), record)
    return record


@pytest.mark.parametrize("code,lost", [(1, False), (0, False), (124, False), (None, True)])
def test_unchanged_attempts_back_off_then_block_durably(h, code, lost):
    assert len(h.controller.reconcile(REPO)["launched"]) == 1
    for attempt in (1, 2):
        finish(h, code, lost=lost)
        result = h.controller.reconcile(REPO)
        assert result["launched"] == []
        action = result["actions"][0]
        assert action["type"] == "worker_retry_wait"
        assert action["retry_at"] == h.clock + 60 * 2 ** (attempt - 1)
        for _ in range(3):
            assert h.controller.reconcile(REPO)["launched"] == []
            assert not h.controller.tick(REPO)["wakeAgent"]
        h.clock = action["retry_at"]
        assert h.controller.tick(REPO)["wakeAgent"]
        assert len(h.controller.reconcile(REPO)["launched"]) == 1
    finish(h, code, lost=lost)
    assert h.controller.tick(REPO)["wakeAgent"]  # First terminal blocker notifies its owner.
    blocked = h.controller.reconcile(REPO)
    assert blocked["status"] == "degraded"
    assert blocked["actions"][0]["owner"] == "operator"
    # New Config and repeated activations cannot erase receipt-owned limits.
    Config(h.config.path)
    h.clock += 100000
    for _ in range(3):
        assert h.controller.reconcile(REPO)["launched"] == []
        assert not h.controller.tick(REPO)["wakeAgent"]
    assert len(h.launched) == 3
    assert h.kernel.record(1)["agents"] == ["codex-one"]
    assert len([c for c in h.kernel.calls if c[0] == "claim"]) == 1
    assert all(os.path.isdir(r["worktree"]) for r in h.launched)


@pytest.mark.parametrize("progress", ["new-pr", "new-head", "new-action", "operator-epoch"])
def test_authoritative_progress_or_explicit_recovery_renews_attempts(h, progress):
    if progress != "new-pr":
        h.controller.reconcile(REPO)
        finish(h)
        h.kernel.record(1)["status"] = "In Review"
        h.kernel.prs = [pr(9, "codex-one", 1)]
        h.kernel.work["codex-one"] = {"type": "feedback", "issue": 1, "pr": 9, "head": HEAD}
    h.controller.reconcile(REPO)
    finish(h)
    assert h.controller.reconcile(REPO)["launched"] == []
    if progress == "new-pr":
        h.kernel.record(1)["status"] = "In Review"
        h.kernel.prs = [pr(9, "codex-one", 1)]
        h.kernel.work["codex-one"] = {"type": "feedback", "issue": 1, "pr": 9, "head": HEAD}
    elif progress == "new-head":
        h.kernel.prs[0]["head"] = "b" * 40
        h.kernel.work["codex-one"]["head"] = "b" * 40
    elif progress == "new-action":
        h.kernel.work["codex-one"]["type"] = "conflict"
    else:
        h.config.project(REPO)["worker_retry_epoch"] = "operator-fixed-root-cause"
    assert len(h.controller.reconcile(REPO)["launched"]) == 1
    finish(h)
    assert h.controller.reconcile(REPO)["actions"][0]["type"] == "worker_retry_wait"
    assert h.state.worker(h.launched[-1]["id"])["retry_observation"]["attempt"] == 1


def test_stop_start_and_duplicate_events_do_not_renew_executed_attempt(h, monkeypatch):
    from aru_project_driver import driver, scheduler
    h.controller.reconcile(REPO)
    finish(h)
    assert h.controller.reconcile(REPO)["launched"] == []
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    h.state.request_stop(REPO)
    assert h.controller.reconcile(REPO)["status"] == "stopped"
    driver.start(h.config, REPO)
    for _ in range(2):
        h.controller.event(REPO, "duplicate", "event", inline=True)
        assert h.controller.reconcile(REPO)["launched"] == []
    assert len(h.launched) == 1


def test_stopped_project_does_not_inspect_or_refill(harness):
    harness.stop()
    assert harness.controller.reconcile(REPO) == {"status": "stopped", "launched": []}
    assert harness.controller.tick(REPO)["wakeAgent"] is False
    assert harness.kernel.calls == [] and harness.probes == []


@pytest.mark.parametrize("field,value", [("attempt", 0), ("attempt", True), ("retry_at", float("inf")),
                                       ("context", "invalid"), ("observed_at", -1)])
def test_malformed_retry_receipts_fail_closed(h, field, value):
    h.controller.reconcile(REPO)
    receipt = finish(h)
    h.controller.reconcile(REPO)
    receipt = h.state.worker(receipt["id"])
    receipt["retry_observation"][field] = value
    write_json(h.state.worker_path(receipt["id"]), receipt)
    with pytest.raises(DriverError, match="retry observation"):
        h.state.workers(REPO)


def test_retry_context_ignores_prose_exit_status_and_clock(h):
    task = {"type": "claimed_issue", "issue": 1}
    assert retries.context(h.config, REPO, task) == retries.context(h.config, REPO, {
        **task, "reason": "new excuse", "exit_code": 0, "updated_at": 99999,
    })


def test_retained_false_quota_result_gets_generic_bound_after_quota_opt_out(scoped, tmp_path):
    config, record, *_ = scoped
    repo, agent = record["repo"], record["agent"]
    assert permissions.enabled(config, repo, agent) and not quota.enabled(config, repo)
    # A validated result retained from an earlier opted-in run can outlive its
    # quota configuration. A false permission blocker supplies no retry policy.
    result = tmp_path / "retained-result.json"
    result.write_text(json.dumps({"type": "result", "is_error": True,
        "subtype": "error_during_execution", "permission_denials": [],
        "result": "You've hit your session limit"}))
    retained = permissions.observe_result(result, 1, quota_errors=True)
    assert retained["outcome"] == "quota_exhausted" and retained["retry_blocked"] is False
    state = State(config.state_dir)
    clock = [1000.0]
    controller = Controller(config, now=lambda: clock[0])
    work = {"type": "claimed_issue", "issue": record["issue"], "agent": agent}
    current = {**record, **retained, "started_at": clock[0] - 1, "finished_at": clock[0],
               "policy_fingerprint": permissions.fingerprint(config, repo, agent)}
    write_json(state.worker_path(current["id"]), current)
    for _ in range(3):
        actions, resumes = [], []
        controller._resume_action(repo, work, state.workers(repo), [agent], actions, resumes)
        assert not resumes
        assert actions[0]["type"] == "worker_retry_wait"
        assert state.worker(current["id"])["retry_observation"]["attempt"] == 1
    clock[0] = actions[0]["retry_at"]
    actions, resumes = [], []
    controller._resume_action(repo, work, state.workers(repo), [agent], actions, resumes)
    assert len(resumes) == 1
