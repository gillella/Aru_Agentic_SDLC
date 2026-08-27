from __future__ import annotations

import pytest

import merge_pr


def base_pr(**overrides):
    pr = {
        "number": 10,
        "body": "Closes #7",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "headRefName": "feat/issue-7-change",
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "labels": [{"name": "review:coderabbit"}],
        "statusCheckRollup": [{"context": "CodeRabbit", "state": "SUCCESS"}],
    }
    pr.update(overrides)
    return pr


def install_happy_gate(monkeypatch, pr=None):
    pr = pr or base_pr()
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda _numbers: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": "a" * 40, "state": "success", "checks": ["Verify"]},
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: True)
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))
    return pr


def test_evaluate_accepts_exact_head_only(monkeypatch):
    install_happy_gate(monkeypatch)
    gates = merge_pr.evaluate(10, "a" * 40)
    assert gates["reviewer"] == "coderabbit"
    assert gates["base_sha"] == "b" * 40
    with pytest.raises(merge_pr.KernelError, match="expected head"):
        merge_pr.evaluate(10, "c" * 40)


def test_evaluate_blocks_unresolved_feedback(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"body": "fix"}])
    with pytest.raises(merge_pr.KernelError, match="unresolved"):
        merge_pr.evaluate(10, "a" * 40)


def test_evaluate_blocks_missing_review(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: False)
    with pytest.raises(merge_pr.KernelError, match="exact-head verdict"):
        merge_pr.evaluate(10, "a" * 40)


def test_merge_rechecks_head_and_base(monkeypatch):
    pr = install_happy_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: {
            "head": "a" * 40,
            "base_sha": "b" * 40,
            "issues": [{"issue": 7}],
        },
    )
    calls = []
    monkeypatch.setattr(merge_pr, "run", lambda argv: calls.append(argv))
    monkeypatch.setattr(merge_pr, "close_out", lambda numbers: calls.append(["close", *numbers]))
    snapshots = iter([pr, {**pr, "mergedAt": "now", "mergeCommit": {"oid": "c" * 40}}])
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: next(snapshots))
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))
    result = merge_pr.merge(10, "a" * 40)
    assert result["merged"] is True
    assert "--match-head-commit" in calls[0]
    assert calls[-1] == ["close", 7]


def test_review_label_must_be_unique():
    with pytest.raises(merge_pr.KernelError, match="exactly one"):
        merge_pr.assigned_service(
            base_pr(labels=[{"name": "review:coderabbit"}, {"name": "review:sourcery"}])
        )
