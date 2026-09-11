"""Unreadable CI stays an explicit blocked action and cannot launch or merge."""
import pytest

from test_controller import HEAD, REPO, harness as harness, issue, pr, mutations


@pytest.mark.parametrize("operation", ["tick", "reconcile"])
def test_unreadable_ci_remains_blocked_without_launch_or_merge(harness, monkeypatch, operation):
    import fetch_next_work
    from common import KernelError
    harness.one_lane()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _n: False)
    def unreadable(_n):
        raise KernelError("required CI inventory is incomplete")
    monkeypatch.setattr(fetch_next_work, "ci_verdict", unreadable)
    monkeypatch.setattr(fetch_next_work, "evaluate", lambda *_a: pytest.fail("uncertain CI cannot merge"))
    harness.kernel.work["codex-one"] = fetch_next_work._open_pr_work({"number": 9, "headRefOid": HEAD})
    result = getattr(harness.controller, operation)(REPO)
    actions = result["plan"]["actions"] if operation == "tick" else result["actions"]
    assert actions == [{"type": "blocked", "pr": 9, "head": HEAD, "agent": "codex-one", "issue": 1,
                        "verification": "unreadable", "ci_error": "required CI inventory is incomplete",
                        "reason": "CI inspection is unreadable; merge is blocked"}]
    assert not harness.launched and not harness.probes and mutations(harness) == []
