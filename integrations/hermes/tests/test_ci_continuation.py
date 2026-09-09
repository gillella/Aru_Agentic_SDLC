"""CI inspection failures must not starve independently guarded review work."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_controller import HEAD, REPO, assigned_review, harness as controller_harness, mutations


harness = controller_harness


def ci_blocked_picker(harness, monkeypatch):
    """Exercise the real picker error path inside the Driver's bounded plan."""
    import fetch_next_work
    from common import KernelError
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _n: False)
    def unreadable(_n):
        raise KernelError("required CI inventory is incomplete")
    monkeypatch.setattr(fetch_next_work, "ci_verdict", unreadable)
    monkeypatch.setattr(fetch_next_work, "evaluate", lambda *_a: pytest.fail("uncertain CI cannot merge"))
    opened = harness.kernel.prs[0]
    harness.kernel.work["codex-one"] = fetch_next_work._open_pr_work({
        "number": opened["number"], "headRefOid": opened["head"],
        "labels": [{"name": label} for label in opened["labels"]],
    })


@pytest.mark.parametrize("due", [False, True])
def test_ci_error_discovers_and_executes_review_continuation(harness, monkeypatch, due):
    assigned_review(harness)
    harness.kernel.recovery_target = deepcopy(harness.kernel.binding)
    harness.kernel.prs[0]["labels"] = ["review:coderabbit"]
    deadline = datetime.fromtimestamp(harness.clock + (0 if due else 120), timezone.utc).isoformat()
    harness.kernel.continuations[9] = {
        "authority": "coderabbit", "next_action": "refresh-reviewer", "retry_at": deadline,
    }
    ci_blocked_picker(harness, monkeypatch)
    result = harness.controller.tick(REPO)
    assert result["wakeAgent"] is due
    assert len(harness.synced) == 1
    assert harness.synced[0][1][0]["ci_error"] == "required CI inventory is incomplete"
    assert harness.state.project(REPO).get("cooldown_until", 0) == 0
    assert mutations(harness) == []
    if due:
        result = harness.controller.reconcile(REPO)
        assert len(result["launched"]) == 1
        assert result["launched"][0]["kind"] == "review"
        assert len([c for c in harness.kernel.calls if c[0] == "refresh_reviewer"]) == 1
        assert harness.controller.reconcile(REPO)["launched"] == []
    else:
        assert harness.probes == []


@pytest.mark.parametrize("failure", ["head", "authority", "unavailable", "family", "cap", "stop"])
def test_ci_error_review_preserves_dispatch_guards(harness, monkeypatch, failure):
    assigned_review(harness)
    harness.kernel.continuations[9] = {k: harness.kernel.work["codex-one"][k]
                                      for k in ("authority", "next_action", "retry_at")}
    ci_blocked_picker(harness, monkeypatch)
    if failure in {"head", "authority"}:
        harness.on_probe = lambda _id: harness.kernel.binding.update({failure: "b" * 40})
    elif failure == "unavailable":
        harness.available["claude-one"] = False
    elif failure == "family":
        harness.config.lanes["claude-one"]["family"] = "openai-codex"
    elif failure == "cap":
        harness.controller._worker_count = lambda _repo: 2
    else:
        harness.on_probe = lambda _id: harness.stop()
    result = harness.controller.reconcile(REPO)
    assert result["launched"] == []
    assert result["actions"][0]["execution"] == "blocked"
    assert result["actions"][0]["verification"] == "unreadable"
    assert mutations(harness) == []


def test_blocked_ci_action_without_picker_continuation_is_still_discovered(harness):
    assigned_review(harness)
    harness.kernel.continuations[9] = {k: harness.kernel.work["codex-one"][k]
                                      for k in ("authority", "next_action", "retry_at")}
    harness.kernel.work["codex-one"] = {"type": "blocked", "pr": 9, "head": HEAD,
                                       "verification": "unreadable", "ci_error": "inventory incomplete"}
    result = harness.controller.reconcile(REPO)
    assert ("reviewer_continuation", 9) in harness.kernel.calls
    assert len(result["launched"]) == 1 and result["launched"][0]["kind"] == "review"
    assert mutations(harness) == []


@pytest.mark.parametrize("error", ["head changed", "unreadable review inventory"])
def test_ci_and_review_errors_remain_explicit_actions(harness, monkeypatch, error):
    assigned_review(harness)
    ci_blocked_picker(harness, monkeypatch)
    from aru_project_driver.kernel import KernelAdapterError
    def unavailable(number, *, expected_head):
        assert (number, expected_head) == (9, HEAD)
        raise KernelAdapterError(error)
    harness.kernel.reviewer_continuation = unavailable
    harness.controller.tick(REPO)
    action = harness.synced[-1][1][0]
    assert action["ci_error"] == "required CI inventory is incomplete"
    assert action["review_error"] == error and "next_action" not in action
    assert harness.probes == [] and mutations(harness) == []


def test_kernel_bridge_preserves_expected_review_head(monkeypatch):
    from types import SimpleNamespace
    from aru_project_driver.kernel import KernelAdapter, _Bridge
    calls = []
    adapter = object.__new__(KernelAdapter)
    bridge = object.__new__(_Bridge)
    def continuation(number, *, expected_head):
        calls.append((number, expected_head))
        return {"authority": "coderabbit", "next_action": "refresh-reviewer"}
    bridge.review = SimpleNamespace(reviewer_continuation=continuation)
    monkeypatch.setattr(adapter, "_invoke", lambda operation, **payload: bridge.dispatch(operation, payload))
    assert adapter.reviewer_continuation(9, expected_head=HEAD)["next_action"] == "refresh-reviewer"
    assert calls == [(9, HEAD)]
