from __future__ import annotations

import pytest

import merge_pr
from review_risk import review_risk_tier
from test_governed_merge import HEAD, install_low_risk_gate, ready_pr


@pytest.mark.parametrize("path", [
    "scripts/review_policy.py",
    "scripts/install_hooks.sh",
    "scripts/cleanup_worktrees.py",
    "scripts/legacy_recovery.py",
    "scripts/new_gate.py",
    "scripts/subdirectory/future_gate.sh",
    "integrations/hermes/aru_project_driver/controller.py",
    "integrations/hermes/aru_project_driver/execution.py",
    "integrations/hermes/aru_project_driver/scheduler.py",
    "integrations/hermes/aru_project_driver/handoff_evidence.py",
    "integrations/hermes/install.py",
    "integrations/hermes/skill/SKILL.md",
])
def test_control_only_pr_requires_current_head_authoritative_review(monkeypatch, path):
    install_low_risk_gate(monkeypatch, paths=[path])
    monkeypatch.setattr(merge_pr, "review_risk_tier", review_risk_tier)

    with pytest.raises(merge_pr.KernelError, match="exactly one review:"):
        merge_pr.evaluate(10, HEAD)

    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: ready_pr(
        labels=[{"name": "review:coderabbit"}],
    ))
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: False)
    with pytest.raises(merge_pr.KernelError, match="no successful exact-head verdict"):
        merge_pr.evaluate(10, HEAD)

    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: True)
    gates = merge_pr.evaluate(10, HEAD)
    assert gates["risk_tier"] == 2
    assert gates["reviewer"] == "coderabbit"


@pytest.mark.parametrize("path", [
    "scripts/review_policy.py",
    "integrations/hermes/aru_project_driver/controller.py",
    "integrations/hermes/install.py",
    "integrations/hermes/skill/SKILL.md",
])
@pytest.mark.parametrize("withdrawn", [False, True])
def test_control_review_is_revalidated_before_merge_submission(monkeypatch, path, withdrawn):
    install_low_risk_gate(monkeypatch, paths=[path])
    monkeypatch.setattr(merge_pr, "review_risk_tier", review_risk_tier)
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: ready_pr(
        labels=[{"name": "review:coderabbit"}],
    ))
    reviews, commands = [], []

    def review(*_args):
        reviews.append(True)
        # Withdraw approval only after both initial evaluations have passed.
        return not (withdrawn and len(reviews) == 3)

    def submit(argv):
        commands.append(argv)
        raise RuntimeError("command spy reached")

    monkeypatch.setattr(merge_pr, "exact_head_review", review)
    monkeypatch.setattr(merge_pr, "run", submit)
    if withdrawn:
        with pytest.raises(merge_pr.KernelError, match="verdict before merge submission"):
            merge_pr.merge(10, HEAD)
        assert commands == []
    else:
        with pytest.raises(RuntimeError, match="command spy reached"):
            merge_pr.merge(10, HEAD)
        assert commands == [[
            "gh", "pr", "merge", "10", "--merge",
            "--match-head-commit", HEAD,
        ]]
    assert len(reviews) == 3


@pytest.mark.parametrize("path,tier", [
    ("src/app.py", 1),
    ("integrations/hermes/tests/test_controller.py", 1),
    ("integrations/hermes/README.md", 0),
])
def test_ordinary_changes_keep_proportional_merge_routing(monkeypatch, path, tier):
    install_low_risk_gate(monkeypatch, paths=[path])
    monkeypatch.setattr(merge_pr, "review_risk_tier", review_risk_tier)
    monkeypatch.setattr(merge_pr, "assigned_service", lambda _pr: pytest.fail(
        "ordinary changes must not require authoritative review",
    ))
    gates = merge_pr.evaluate(10, HEAD)
    assert gates["risk_tier"] == tier
    assert gates["reviewer"] == "not-required"
