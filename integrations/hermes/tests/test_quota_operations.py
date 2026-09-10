"""Operational receipts, contention and installer tests use private temporary homes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from test_install import installer
from test_permissions import scoped as scoped
from test_quota import qh as qh, observation
from test_quota_boundaries import terminal, alias
from test_controller import REPO, issue
from aru_project_driver import driver, execution, permissions, quota, quota_admission as admission, quota_collect, quota_worker
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import key, read_json, write_json


def test_second_slot_rechecks_shared_alias_budget_after_first_reservation(qh, monkeypatch):
    old = terminal(qh)
    alias(qh, shared=True)
    for i in ("codex-one", "codex-two"):
        qh.config.lanes[i]["max_sessions"] = 2
    qh.kernel.record(2).update(status="In Progress", agents=["codex-two"])
    path = qh.kernel.branch(2, "codex-two")
    monkeypatch.setattr(execution, "KernelAdapter", lambda *args: qh.kernel)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 65))
    descriptors = []
    def launch(*args, **kwargs):
        descriptors.append(os.dup(kwargs["pass_fds"][0]))
        return SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(execution.subprocess, "Popen", launch)
    try:
        with qh.state.lock():
            first = execution.launch(qh.config, REPO, "codex-one", 1, old["worktree"])
        assert qh.state.worker(first["id"])["quota_decision"]["margin"] == 25
        with qh.state.lock(), pytest.raises(DriverError, match="preserved"):
            execution.launch(qh.config, REPO, "codex-two", 2, path)
        assert len(descriptors) == 1
        assert any("insufficient" in r.get("reason", "") for r in qh.state.workers(REPO))
    finally:
        for fd in descriptors:
            os.close(fd)


def test_completed_quota_delta_is_bounded_private_history_not_token_conversion(qh, monkeypatch):
    record = terminal(qh)
    before = observation(qh.config.lanes["codex-one"], 90)
    after = deepcopy(before)
    for window in after["windows"].values():
        window["remaining"] = 80
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: before if i == "codex-one" else observation(c.lane(r, i)))
    record["quota_decision"] = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    record.update(outcome="reported_success", child_pid=123, exit_code=0)
    # Review has its own account binding; only the post-run collector uses this fixed observation.
    monkeypatch.setattr(quota_collect, "collect", lambda *args: after)
    path = qh.state.root / "quota-history" / (key(qh.config.lanes["codex-one"]["quota"]["account_sha256"]) + ".json")
    sample = {"signature": {}, "pool": "codex", "durations": qh.config.lanes["codex-one"]["quota"]["windows"],
              "unit": "percent", "percent": {"primary": 1, "secondary": 1}}
    write_json(path, {"samples": [sample for _ in range(128)]})
    quota_worker.finish(qh.config, qh.state, record)
    samples = read_json(path)["samples"]
    assert len(samples) == 128 and samples[-1]["percent"] == {"primary": 10, "secondary": 10}
    assert samples[-1]["unit"] == "percent" and path.stat().st_mode & 0o777 == 0o600
    assert "tokens" not in json.dumps(samples[-1]) and not record.get("governed_completion")


def test_installer_copies_quota_runbook_and_complete_modules_without_activation(qh):
    qh.config.kernel_root.mkdir()
    qh.config.path.write_text(json.dumps(qh.config.raw))
    source = Path(__file__).resolve().parents[1]
    result = installer.install(source, qh.config.path)
    names = {Path(item["source"]).name for item in result["files"]}
    assert {"quota.py", "quota_collect.py", "quota_admission.py", "quota_boundary.py", "quota_worker.py", "quota_checkpoint.py", "QUOTA.md"} <= names
    assert result["activated"] is False and result["applied"] is False
    assert not (qh.config.hermes_home / "scripts").exists()
    example = Config(source / "config.example.json", bind=False)
    assert example.project("example/project")["quota_admission"]["unknown_review_seconds"] == 1800


def test_status_exposes_bounded_decisions_cooldown_and_owner(qh, monkeypatch):
    monkeypatch.setattr(driver.scheduler, "scheduler_status", lambda *a, **k: {"verified": True})
    qh.controller.reconcile(REPO)
    response = driver.status(qh.config, REPO)
    assert response["quota"]["decisions"] and len(response["quota"]["decisions"]) <= 32
    assert set(response["quota"]["cooldowns"]) == {"codex-one", "claude-one"}
    assert all(d["continuation_owner"] == "Hermes Driver completion/heartbeat" for d in response["quota"]["decisions"])


@pytest.mark.parametrize("mutation", ["denial", "auth", "malformed", "ordinary"])
def test_claude_nonquota_results_stay_fail_closed(qh, tmp_path, mutation):
    payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "permission_denials": [], "result": "You've hit your session limit · resets 11:30am (America/New_York)"}
    if mutation == "denial":
        payload["permission_denials"] = [{"tool_name": "Bash", "tool_input": {}}]
    elif mutation == "auth":
        payload["api_error_status"] = 401
    elif mutation == "malformed":
        payload.pop("type")
    else:
        payload["result"] = "provider temporarily unavailable"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    outcome = permissions.observe_result(path, 1, quota_errors=True)
    assert outcome["retry_blocked"] and outcome["outcome"] != "quota_exhausted"


def test_codex_native_error_is_quota_but_model_prose_and_invalid_usage_are_not(qh, tmp_path):
    path = tmp_path / "result.jsonl"
    lane = qh.config.lanes["codex-one"]
    message = "You've hit your usage limit. Try again later."
    path.write_text(json.dumps({"type": "turn.failed", "error": {"message": message}}))
    assert quota_worker.observe(path, lane, 1)["outcome"] == "quota_exhausted"
    for payload in ({"type": "item.completed", "item": {"type": "agent_message", "text": message}},
                    {"type": "turn.completed", "usage": {"input_tokens": float("nan")}},
                    {"type": "item.completed", "item": "invalid"}):
        path.write_text(json.dumps(payload))
        assert quota_worker.observe(path, lane, 1)["retry_blocked"]


def test_reset_cannot_be_fabricated_far_outside_its_window(qh):
    lane = qh.config.lanes["codex-one"]
    data = observation(lane)
    data["windows"]["primary"]["reset_at"] = time.time() + 10 ** 9
    with pytest.raises(DriverError, match="reset"):
        quota.validate(data, lane, time.time(), 120)


@pytest.mark.parametrize("path,bucket", [("README.md", 0), ("AGENTS.md", 3), ("docs/KERNEL-CONTRACT.md", 3), ("src/main.py", 3)])
def test_demand_risk_comes_from_canonical_scope_classification(qh, tmp_path, path, bucket):
    from test_kernel import Backend, make_bridge, issue as kernel_issue
    raw = kernel_issue(1, touches=path)
    task = make_bridge(Backend([raw]), tmp_path)._issue(raw, {})
    estimate = admission.demand(qh.config, qh.state, REPO, "codex-one", task, "implementation")
    assert estimate["risk"] == bucket


def test_checkpoint_notes_do_not_add_sandbox_grants(scoped):
    before = permissions.compile_policy(*scoped)
    scoped[1]["quota_checkpoint"] = "/private/checkpoint.json"
    assert permissions.compile_policy(*scoped) == before


def test_alias_cooldown_suppresses_real_availability_without_inventing_reset(qh, monkeypatch):
    lane = alias(qh, shared=True)
    deadline = time.time() + 600
    write_json(qh.state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json"),
               {"until": deadline, "reset_at": None, "reason": "quota-exhausted"})
    monkeypatch.setattr(execution, "run_bounded", lambda *a, **k: pytest.fail("cooldown must precede CLI calls"))
    for identity in ("codex-one", "codex-two"):
        result = execution.availability(qh.config, REPO, identity, qh.state)
        assert result["available"] is False and result["retry_at"] == deadline and result["reset_at"] is None


@pytest.mark.parametrize("result_kind", ["quota", "denied", "missing"])
def test_existing_misclassification_requires_retained_native_result(qh, result_kind):
    record = terminal(qh, "claude-one", "worker_error")
    qh.state.worker_path(record["id"]).unlink()
    record.update(id="c" * 32, finished_at=time.time() - 20, child_pid=123, retry_blocked=True,
                  permission_snapshot={"result_format": "claude-json"}, exit_code=1)
    path = qh.state.root / "logs" / (record["id"] + ".result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    record["result_path"] = str(path)
    payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "permission_denials": [], "result": "You've hit your session limit · resets 11:30am (America/New_York)"}
    if result_kind == "denied":
        payload["permission_denials"] = [{"tool_name": "Bash", "tool_input": {}}]
    if result_kind != "missing":
        path.write_text(json.dumps(payload))
    write_json(qh.state.worker_path(record["id"]), record)
    qh.kernel.issues = qh.kernel.issues[:1]
    qh.controller.reconcile(REPO)
    observed = qh.state.worker(record["id"])
    if result_kind == "quota":
        assert observed["outcome"] == "quota_exhausted" and not observed["retry_blocked"]
        cooldown = qh.state.root / "cooldowns" / (key(record["capacity_key"]) + ".json")
        first = read_json(cooldown)
        qh.controller.reconcile(REPO)
        assert read_json(cooldown) == first and first["reset_at"] is None
        assert first["until"] == record["finished_at"] + 600
    else:
        assert observed["outcome"] == "worker_error" and observed["retry_blocked"]
    assert observed["worktree"] == record["worktree"] and observed["agent"] == record["agent"]
