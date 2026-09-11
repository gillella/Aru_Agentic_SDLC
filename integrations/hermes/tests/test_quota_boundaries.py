"""Exercise real controller/launch/child boundaries with isolated synthetic providers."""
from copy import deepcopy
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from test_controller import REPO, issue
from test_quota import qh as qh, observation
from aru_project_driver import execution, permissions, quota, quota_admission as admission, quota_collect, quota_worker
from aru_project_driver.state import key, read_json, write_json


def terminal(qh, agent="codex-one", outcome="quota_exhausted"):
    qh.kernel.issues[0].update(status="In Progress", agents=[agent])
    path = qh.kernel.branch(1, agent)
    receipt = {"id": "failed-owner", "repo": REPO, "agent": agent, "issue": 1, "state": "exited",
               "capacity_key": qh.config.lanes[agent]["capacity_key"], "worktree": path, "started_at": time.time(),
               "kind": "implementation", "outcome": outcome, "retry_blocked": False,
               "policy_fingerprint": permissions.fingerprint(qh.config, REPO, agent), "reason": "quota-exhausted"}
    write_json(qh.state.worker_path(receipt["id"]), receipt)
    return receipt


def alias(qh, identity="codex-two", *, shared=False):
    lane = deepcopy(qh.config.lanes["codex-one"])
    if not shared:
        lane["quota"]["account_sha256"] = quota.digest(identity)
        lane["capacity_key"] = "account-" + identity
    qh.config.lanes[identity] = lane
    qh.config.project(REPO)["lanes"].append(identity)
    qh.available[identity] = True
    return lane


def test_ranked_author_selection_sees_all_lanes_before_worker_cap(qh, monkeypatch):
    alias(qh)
    qh.config.project(REPO)["max_workers"] = 1
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 95 if i == "codex-two" else 60))
    result = qh.controller.reconcile(REPO)
    assert [(r["agent"], r["issue"]) for r in result["launched"]] == [("codex-two", 1)]
    assert ("claim", 1, "codex-one") not in qh.kernel.calls


def test_no_budget_means_no_actual_claim_or_launch_despite_ok_probe(qh, monkeypatch):
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 35))
    result = qh.controller.reconcile(REPO)
    assert not result["launched"] and not any(c[0] == "claim" for c in qh.kernel.calls)
    decisions = read_json(qh.state.root / "quota-decisions" / (key(REPO) + ".json"))["decisions"]
    assert decisions and all(not d["accepted"] for d in decisions)
    assert "insufficient" in decisions[0]["reason"]


def test_resume_rechecks_quota_after_probe(qh, monkeypatch):
    terminal(qh)
    qh.kernel.issues = qh.kernel.issues[:1]
    qh.on_probe = lambda identity: monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 20))
    assert not qh.controller.reconcile(REPO)["launched"]
    assert qh.kernel.record(1)["agents"] == ["codex-one"]


def test_child_recheck_updates_risk_without_reserving_another_worker(qh):
    record = terminal(qh)
    qh.kernel.record(1)["quota_risk"] = 0
    quota_worker.prepare(qh.config, qh.state, record, qh.kernel)
    assert len(record["quota_decision"]["reservations"]) == 1
    qh.kernel.record(1)["quota_risk"] = 2
    quota_worker.recheck(qh.config, qh.state, record, qh.kernel)
    assert record["quota_decision"]["demand"]["risk"] == 3
    assert [r["role"] for r in record["quota_decision"]["reservations"]] == ["worker"]


def test_quota_fallback_uses_canonical_transition_and_preserves_checkpoint(qh):
    old = terminal(qh)
    alias(qh)
    qh.available["codex-one"] = False
    qh.kernel.issues = qh.kernel.issues[:1]
    def release(number, agent):
        qh.kernel.calls.append(("release", number, agent))
        qh.kernel.revalidate(number, agent)
        qh.kernel.record(number).update(status="Ready", agents=[])
    qh.kernel.release = release
    result = qh.controller.reconcile(REPO)
    assert [(r["agent"], r["issue"]) for r in result["launched"]] == [("codex-two", 1)]
    assert result["launched"][0]["worktree"] == old["worktree"]
    assert ("release", 1, "codex-one") in qh.kernel.calls and ("claim", 1, "codex-two") in qh.kernel.calls
    preserved = qh.state.worker(old["id"])
    assert preserved["agent"] == "codex-one" and preserved["quota_transfer_completed"]


@pytest.mark.parametrize("gate", ["live-owner", "stop", "permission", "bound", "all-exhausted", "open-pr"])
def test_quota_recovery_preserves_fences(qh, gate):
    old = terminal(qh)
    if gate == "live-owner":
        qh.launch(qh.config, REPO, "codex-one", 1, old["worktree"])
    elif gate == "stop":
        qh.state.request_stop(REPO)
    elif gate == "permission":
        old.update(outcome="permission_denied", retry_blocked=True, reason="real permission denial")
        write_json(qh.state.worker_path(old["id"]), old)
    elif gate == "bound":
        qh.config.project(REPO)["quota_admission"]["max_recoveries"] = 0
    else:
        qh.available["codex-one"] = False
        if gate == "open-pr":
            alias(qh)
            qh.kernel.work["codex-one"] = {"type": "feedback", "issue": 1, "pr": 9}
    qh.kernel.issues = qh.kernel.issues[:1]
    result = qh.controller.reconcile(REPO)
    assert not result["launched"]
    assert qh.kernel.record(1)["agents"] == ["codex-one"]
    assert not any(c[0] in {"claim", "release"} for c in qh.kernel.calls)


def test_account_aliases_share_reservations_across_projects(qh):
    alias(qh, shared=True)
    qh.config.lanes["codex-one"]["max_sessions"] = 2
    qh.config.lanes["codex-two"]["max_sessions"] = 2
    d = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    old = terminal(qh)
    receipt = qh.launch(qh.config, REPO, "codex-one", 1, old["worktree"], quota_decision=d)
    receipt["repo"] = "other/project"
    qh.state.save("other/project", {**qh.state.project("other/project"), "enabled": True})
    write_json(qh.state.worker_path(receipt["id"]), receipt)
    held = admission.reservations(qh.config, qh.state, qh.config.lanes["codex-two"]["quota"]["account_sha256"], "codex")
    assert held == {"primary": 30, "secondary": 30}
    assert admission.reservations(qh.config, qh.state, qh.config.lanes["claude-one"]["quota"]["account_sha256"], "codex") == {"primary": 0, "secondary": 0}


@pytest.mark.parametrize("after", ["healthy", "exhausted", "stop"])
def test_actual_launch_and_child_revalidate_under_inherited_lock(qh, monkeypatch, tmp_path, after):
    old = terminal(qh, outcome="checkpoint-prepared")
    script = tmp_path / "native.py"
    script.write_text('print(\'{"type":"turn.completed","usage":{"input_tokens":3,"cached_input_tokens":0,"output_tokens":1}}\')\n')
    lane = qh.config.lanes["codex-one"]
    lane["command"] = [sys.executable, str(script), "exec", "--model", lane["quota"]["model"], "{prompt}"]
    monkeypatch.setattr(execution, "KernelAdapter", lambda *args: qh.kernel)
    inherited = []
    def supervisor(*args, **kwargs):
        inherited.append(os.dup(kwargs["pass_fds"][0]))
        return SimpleNamespace(pid=os.getpid())
    with monkeypatch.context() as local:
        local.setattr(execution.subprocess, "Popen", supervisor)
        with qh.state.lock():
            launched = execution.launch(qh.config, REPO, "codex-one", 1, old["worktree"])
    receipt = qh.state.worker(launched["id"])
    assert receipt["quota_decision"]["reservations"] and qh.state.capacity_busy(lane["capacity_key"])
    if after == "exhausted":
        monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 0, "exhausted"))
    elif after == "stop":
        qh.state.request_stop(REPO)
    monkeypatch.setattr(execution, "scoped_environment", lambda *args: {})
    monkeypatch.setattr("aru_project_driver.scheduler.schedule_wake", lambda *args, **kwargs: None)
    code = execution.worker_main(qh.config, launched["id"], inherited[0])
    final = qh.state.worker(launched["id"])
    assert final["state"] == "exited" and not qh.state.capacity_busy(lane["capacity_key"])
    if after == "healthy":
        assert code == 0 and final["child_pid"] and final["outcome"] == "reported_success"
    else:
        assert code != 0 and "child_pid" not in final


def test_claude_637_quota_envelope_is_not_permission_policy_failure(qh, tmp_path, monkeypatch):
    payload = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "result": "You've hit your session limit · resets 11:30am (America/New_York)", "permission_denials": []}
    result = tmp_path / "result.json"
    result.write_text(json.dumps(payload))
    assert permissions.observe_result(result, 1)["outcome"] == "worker_error"  # Non-opted compatibility.
    observed = permissions.observe_result(result, 1, quota_errors=True)
    assert observed["outcome"] == "quota_exhausted" and observed["retry_blocked"] is False
    assert observed["quota_reset_at"] is None and "11:30am" not in json.dumps(observed)
    record = terminal(qh, "claude-one")
    qh.config.project(REPO)["quota_admission"]["unknown_checkpoint_seconds"] = 60
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), state="unknown"))
    record["quota_decision"] = admission.evaluate(qh.config, qh.state, REPO, "claude-one", issue(1), "implementation", qh.kernel)
    record.update(observed, child_pid=123)
    quota_worker.finish(qh.config, qh.state, record)
    cooldown = read_json(qh.state.root / "cooldowns" / (key(record["capacity_key"]) + ".json"))
    assert cooldown["reset_at"] is None and time.time() + 590 < cooldown["until"] < time.time() + 610
    assert record["quota_continuation"]["worktree"] == record["worktree"]
    payload["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "denied"}}]
    result.write_text(json.dumps(payload))
    assert permissions.observe_result(result, 1, quota_errors=True)["outcome"] == "permission_denied"
    payload["permission_denials"] = ["malformed"]
    result.write_text(json.dumps(payload))
    assert permissions.observe_result(result, 1, quota_errors=True)["outcome"] == "result_unavailable"


def test_legacy_author_review_allocation_never_reserves_quota(qh):
    record = terminal(qh)
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    other = qh.config.lanes["claude-one"]["quota"]
    decision["reservations"].append({"role": "review", "account": other["account_sha256"],
                                      "pool": other["pool"], "percent": {"primary": 90, "secondary": 90}})
    qh.launch(qh.config, REPO, "codex-one", 1, record["worktree"], quota_decision=decision)
    assert admission.reservations(qh.config, qh.state, other["account_sha256"], other["pool"]) == {"primary": 0, "secondary": 0}
