"""PR646 regressions: synthetic providers, real controller locks and child processes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from test_controller import REPO, assigned_review, issue
from test_quota import qh as qh, observation
from test_quota_boundaries import terminal
from aru_project_driver import execution, permissions, quota, quota_admission as admission, quota_boundary
from aru_project_driver import quota_checkpoint, quota_collect, quota_worker
from aru_project_driver.config import DriverError
from aru_project_driver.state import key, read_json, write_json


def unknown_mode(qh, monkeypatch, seconds=1800):
    qh.config.project(REPO)["quota_admission"].update(unknown_checkpoint_seconds=seconds,
        unknown_review_seconds=seconds, unknown_max_attempts=2, unknown_total_seconds=seconds * 2)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: quota.unknown(c.lane(r, i), time.time(), "fixture-unknown"))


@pytest.mark.parametrize("field,value", [("unknown_review_seconds", True), ("unknown_review_seconds", 86401),
    ("unknown_max_attempts", 33), ("unknown_total_seconds", 0), ("review_escrow_seconds", 3601)])
def test_new_operator_bounds_are_validated(qh, field, value):
    qh.config.project(REPO)["quota_admission"][field] = value
    with pytest.raises(DriverError):
        quota.validate_config(qh.config)


@pytest.mark.parametrize("state", ["unknown", "insufficient", "invalid"])
def test_review_refusal_never_rotates_actual_mutable_binding(qh, monkeypatch, state):
    assigned_review(qh)
    original = deepcopy(qh.kernel.binding)
    lane = deepcopy(qh.config.lanes["claude-one"])
    lane["capacity_key"] = "different-account"
    lane["quota"]["account_sha256"] = quota.digest("different-account")
    qh.config.lanes["claude-two"] = lane
    qh.config.project(REPO)["lanes"].append("claude-two")
    qh.available["claude-two"] = True
    qh.kernel.recovery_target = {**original, "reviewer": "claude-two"}
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: {} if state == "invalid" else
                        observation(c.lane(r, i), 1, "unknown" if state == "unknown" else "known"))
    for _ in range(4):
        assert not qh.controller.reconcile(REPO)["launched"]
    assert qh.kernel.binding == original
    assert not any(c[0] == "refresh_reviewer" for c in qh.kernel.calls)


def test_known_author_precedes_unknown_with_unknown_independent_review(qh, monkeypatch):
    unknown_mode(qh, monkeypatch)
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i), 95)
                        if i == "codex-one" else quota.unknown(c.lane(r, i), time.time(), "fixture-unknown"))
    decision = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    assert decision["checkpoint_seconds"] == 0 and decision["observation"]["state"] == "known"
    assert decision["review_budget"]["observation"]["state"] == "unknown"
    assert decision["review_budget"]["checkpoint_seconds"] == 1800
    assert len(decision["reservations"]) == 2
    assert qh.controller.reconcile(REPO)["launched"][0]["agent"] == "codex-one"


def test_genuine_exhaustion_still_uses_canonical_recovery_to_distinct_account(qh, monkeypatch):
    assigned_review(qh)
    lane = deepcopy(qh.config.lanes["claude-one"])
    lane["capacity_key"] = "different-account"
    lane["quota"]["account_sha256"] = quota.digest("different-account")
    qh.config.lanes["claude-two"] = lane
    qh.config.project(REPO)["lanes"].append("claude-two")
    qh.available["claude-two"] = True
    qh.kernel.recovery_target = {**qh.kernel.binding, "reviewer": "claude-two"}
    monkeypatch.setattr(quota_collect, "collect", lambda c, r, i: observation(c.lane(r, i),
                        0 if i == "claude-one" else 90, "exhausted" if i == "claude-one" else "known"))
    result = qh.controller.reconcile(REPO)
    assert result["launched"][0]["agent"] == qh.kernel.binding["reviewer"] == "claude-two"
    qh.controller.reconcile(REPO)
    assert sum(c[0] == "refresh_reviewer" for c in qh.kernel.calls) == 1


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


@pytest.mark.parametrize("identity,reviewing", [("codex-one", False), ("claude-one", False), ("claude-one", True)])
def test_real_children_preserve_progress_continue_and_stop_at_owned_limit(qh, monkeypatch, tmp_path, identity, reviewing):
    unknown_mode(qh, monkeypatch, seconds=1)
    if reviewing:
        assigned_review(qh)
    else:
        terminal(qh, identity, "prepared")
        qh.kernel.issues = qh.kernel.issues[:1]
    lane = qh.config.lanes[identity]
    code = tmp_path / "bounded_native.py"
    completed = {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [], "result": "continued useful step"}
    if identity == "codex-one":
        completed = {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}
    # Review emits progress without source writes; author persists permitted source
    # before an actual sleep exceeds the supervisor's one-second test bound.
    code.write_text("import sys,time\nfrom pathlib import Path\n" + (
        "if '\"outcome\": \"quota_checkpoint\"' not in sys.argv[-1]:\n time.sleep(20)\n" if reviewing else
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
    if not reviewing:
        assert (Path(receipts[1]["worktree"]) / "src/issue_1.py").read_text() == "step = 2\n"
    else:
        assert receipts[0]["review"] == receipts[1]["review"] == qh.kernel.binding
    limited = qh.controller.reconcile(REPO)
    assert not limited["launched"] and "unknown work limit" in json.dumps(limited)
    assert not any(c[0] in {"refresh_reviewer", "release"} for c in qh.kernel.calls)


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


@pytest.mark.parametrize("end", ["closed", "blocked", "stopped", "expired"])
def test_review_lease_releases_without_other_project_reconcile(qh, end):
    record = terminal(qh, outcome="reported_success")
    record["quota_decision"] = admission.evaluate(qh.config, qh.state, REPO, "codex-one", issue(1), "implementation", qh.kernel)
    if end == "expired":
        record["quota_decision"]["review_expires_at"] = time.time() - 1
    if end == "blocked":
        record["retry_blocked"] = True
    write_json(qh.state.worker_path(record["id"]), record)
    if end == "closed":
        qh.kernel.record(1).update(state="CLOSED", status="Backlog")
    if end in {"closed", "blocked"}:
        quota_boundary.settle(qh.controller, REPO, qh.kernel)
    if end == "stopped":
        qh.state.request_stop(REPO)
    account = qh.config.lanes["claude-one"]["quota"]["account_sha256"]
    assert admission.reservations(qh.config, qh.state, account, "other-pool") == {"primary": 0, "secondary": 0}
    assert qh.state.worker(record["id"])["quota_review_released"]


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


def test_checkpoint_identity_separates_projects_issues_roles_and_attempts(qh):
    records = [{"repo": r, "issue": n, "kind": k, "id": a} for r, n, k, a in [
        ("owner/proj1", 23, "implementation", "a"), ("owner/proj", 123, "implementation", "a"),
        ("owner/proj1", 23, "review", "a"), ("owner/proj1", 23, "implementation", "b")]]
    assert len({quota_checkpoint.note_path(qh.state, r) for r in records}) == len(records)
