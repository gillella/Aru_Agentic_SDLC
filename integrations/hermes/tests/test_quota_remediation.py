"""PR646 regressions: synthetic providers, real controller locks and child processes."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from test_controller import REPO, issue
from test_quota import qh as qh, observation
from test_quota_boundaries import terminal
from aru_project_driver import execution, permissions, quota, quota_admission as admission
from aru_project_driver import quota_checkpoint, quota_collect, quota_worker
from aru_project_driver.config import DriverError
from aru_project_driver.state import key, read_json, write_json


def unknown_mode(qh, monkeypatch, seconds=1800):
    qh.config.project(REPO)["quota_admission"].update(unknown_checkpoint_seconds=seconds,
        unknown_max_attempts=2, unknown_total_seconds=seconds * 2)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: quota.unknown(c.lane(r, i), time.time(), "fixture-unknown"))


@pytest.mark.parametrize("field,value", [("unknown_max_attempts", 33), ("unknown_total_seconds", 0)])
def test_new_operator_bounds_are_validated(qh, field, value):
    qh.config.project(REPO)["quota_admission"][field] = value
    with pytest.raises(DriverError):
        quota.validate_config(qh.config)


def test_known_author_precedes_unknown_author(qh, monkeypatch):
    unknown_mode(qh, monkeypatch)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 95)
                        if i == "codex-one" else quota.unknown(c.lane(r, i), time.time(), "fixture-unknown"))
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    assert decision["checkpoint_seconds"] == 0 and decision["observation"]["state"] == "known"
    assert len(decision["reservations"]) == 1
    assert qh.controller.reconcile(REPO)["launched"][0]["agent"] == "codex-one"


def supervised(qh, monkeypatch):
    descriptors = []
    monkeypatch.setattr(execution, "KernelAdapter", lambda *a: qh.kernel)
    monkeypatch.setattr(execution, "scoped_environment", lambda *a: {})
    monkeypatch.setattr("aru_project_driver.scheduler.schedule_wake", lambda *a, **k: None)
    def supervisor(*args, **kwargs):
        descriptors.append(os.dup(kwargs["pass_fds"][0]))
        return SimpleNamespace(pid=os.getpid())
    def launch(*args, **kwargs):
        with monkeypatch.context() as local:
            local.setattr(execution.subprocess, "Popen", supervisor)
            return execution.launch(*args, **kwargs)
    qh.controller.launch = launch
    return descriptors


@pytest.mark.parametrize("identity", ["codex-one", "claude-one"])
def test_real_children_preserve_progress_continue_and_stop_at_owned_limit(qh, monkeypatch, tmp_path, identity):
    unknown_mode(qh, monkeypatch, seconds=1)
    prepared = terminal(qh, identity, "prepared")
    prepared["state"] = "prepared"  # Claim/branch recovery before any child attempt.
    write_json(qh.state.worker_path(prepared["id"]), prepared)
    qh.kernel.issues = qh.kernel.issues[:1]
    lane = qh.config.lanes[identity]
    code = tmp_path / "bounded_native.py"
    completed = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [], "result": "continued useful step"}
    if identity == "codex-one":
        completed = {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}
    # The author persists permitted source
    # before an actual sleep exceeds the supervisor's one-second test bound.
    code.write_text("import sys,time\nfrom pathlib import Path\n" + (
        "p=Path('src/issue_1.py')\np.parent.mkdir(exist_ok=True)\n"
        "if not p.exists():\n p.write_text('step = 1\\n')\n"
        + (" print('{\"type\":\"thread.started\",\"thread_id\":\"fixture\"}', flush=True)\n" if identity == "codex-one" else "") +
        " time.sleep(20)\n"
        "assert p.read_text() == 'step = 1\\n'\np.write_text('step = 2\\n')\n") +
        f"print({json.dumps(json.dumps(completed))})\n")
    lane["command"] = [sys.executable, str(code), "exec", "--model", lane["quota"]["model"], "{prompt}"]
    descriptors = supervised(qh, monkeypatch)
    receipts = []
    for attempt in range(2):
        result = qh.controller.reconcile(REPO)
        assert len(result["launched"]) == 1, result
        launched = result["launched"][0]
        code_result = execution.worker_main(qh.config, launched["id"], descriptors.pop())
        receipt = qh.state.worker(launched["id"])
        receipts.append(receipt)
        assert code_result == (124 if attempt == 0 else 0)
        assert receipt["outcome"] == "quota_checkpoint" and not receipt["retry_blocked"]
        assert permissions.retry_blocker(qh.config, receipt, {"pr": receipt["pr"], "head": receipt["head"]}) is None
        assert Path(receipt["quota_checkpoint"]).is_file()
        assert not qh.state.capacity_busy(lane["capacity_key"])
    assert receipts[0]["id"] != receipts[1]["id"] and receipts[0]["quota_checkpoint"] != receipts[1]["quota_checkpoint"]
    assert receipts[0]["policy_fingerprint"] == receipts[1]["policy_fingerprint"]
    assert "prior untrusted worker notes:" in receipts[1]["prompt"]
    assert (Path(receipts[1]["worktree"]) / "src/issue_1.py").read_text() == "step = 2\n"
    limited = qh.controller.reconcile(REPO)
    assert not limited["launched"] and "unknown work limit" in json.dumps(limited)
    assert not any(c[0] == "release" for c in qh.kernel.calls)


@pytest.mark.parametrize("provider,payload", [("claude-one", "bad-json"),
    ("claude-one", json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "permission_denials": [{"tool_name": "Bash", "tool_input": {}}]})),
    ("codex-one", '{"type":"item.completed","item":{"status":"declined"}}'),
    ("codex-one", '{"type":"turn.failed","error":{"message":"permission denied"}}'),
    ("codex-one", '{"type":"item.completed"}'),
    ("codex-one", '{"type":"turn.failed","type":"turn.started"}')])
def test_timeout_does_not_override_denial_or_invalid_evidence(qh, tmp_path, provider, payload):
    path = tmp_path / "invalid.json"
    path.write_text(payload)
    observed = quota_worker.observe(path, qh.config.lanes[provider], 124, timed_out=True)
    assert observed["retry_blocked"] and observed["outcome"] != "quota_checkpoint"


def test_generic_exhaustion_preserves_longer_genuine_alias_reset(qh, monkeypatch):
    unknown_mode(qh, monkeypatch)
    record = terminal(qh)
    lane = qh.config.lanes["codex-one"]
    path = qh.state.root / "cooldowns" / (key(lane["capacity_key"]) + ".json")
    genuine = {"until": time.time() + 500000, "reset_at": time.time() + 499999, "pool": "other-pool", "reason": "quota-exhausted"}
    write_json(path, genuine)
    record.update(quota_decision={"checkpoint_seconds": 0, "observation": {"state": "unknown"}}, child_pid=123)
    quota_worker.finish(qh.config, qh.state, record)
    assert read_json(path) == genuine


def test_checkpoint_identity_separates_projects_issues_and_attempts(qh):
    records = [{"repo": r, "issue": n, "kind": k, "id": a} for r, n, k, a in [
        ("owner/proj1", 23, "implementation", "a"), ("owner/proj", 123, "implementation", "a"),
        ("owner/proj1", 23, "implementation", "b")]]
    assert len({quota_checkpoint.note_path(qh.state, r) for r in records}) == len(records)
