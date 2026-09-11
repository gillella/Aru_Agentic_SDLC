"""Fail-closed guards the 2026-09-10 audit found could be deleted without a test failing."""

from __future__ import annotations

import pytest

import claim_issue
import merge_pr
from test_merge_gate import base_pr, install_happy_gate

HEAD = "a" * 40


def install_claim(monkeypatch, snapshots, *, active=()):
    records = iter(snapshots)
    monkeypatch.setattr(claim_issue, "issue", lambda _n: next(records))
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "other_active_claims", lambda *_args: list(active))
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claim_issue, "set_status", lambda *_args, **_kwargs: None)
    writes, rollbacks = [], []
    monkeypatch.setattr(claim_issue, "run", lambda argv, **_kwargs: writes.append(argv))
    monkeypatch.setattr(claim_issue, "rollback_claim", lambda *args: rollbacks.append(args))
    return writes, rollbacks


def ready(*extra):
    return {"number": 5, "labels": [{"name": "status:ready"}, *({"name": name} for name in extra)]}


def test_claim_refuses_an_issue_someone_else_holds(monkeypatch):
    writes, _ = install_claim(monkeypatch, [ready("agent:other")])
    with pytest.raises(claim_issue.KernelError, match="already claimed by other"):
        claim_issue.claim(5, "codex-1")
    assert writes == []


def test_claim_refuses_an_agent_that_already_holds_work(monkeypatch):
    writes, _ = install_claim(monkeypatch, [ready()], active=[9])
    with pytest.raises(claim_issue.KernelError, match="agent already has active issue claims"):
        claim_issue.claim(5, "codex-1")
    assert writes == []


def test_claim_that_does_not_settle_is_rolled_back(monkeypatch):
    claimed = ready("agent:codex-1")
    _, rollbacks = install_claim(monkeypatch, [ready(), claimed, claimed])  # status never reaches In Progress
    with pytest.raises(claim_issue.KernelError, match="claim did not settle"):
        claim_issue.claim(5, "codex-1")
    assert rollbacks == [(5, "agent:codex-1")]


def test_merge_refuses_a_pr_github_does_not_call_mergeable():
    queue = {"configured": False, "entry": None, "auto_merge": None}
    with pytest.raises(merge_pr.KernelError, match="not currently mergeable"):
        merge_pr.require_mergeable(base_pr(mergeable="CONFLICTING"), queue)


@pytest.mark.parametrize("merged, reason", [
    ({"headRefOid": "e" * 40, "mergedAt": "now"}, "head changed during merge submission"),
    ({"mergedAt": None}, "did not confirm the expected-head merge"),
])
def test_merge_does_not_close_out_what_github_did_not_confirm(monkeypatch, merged, reason):
    pr = install_happy_gate(monkeypatch)
    gates = merge_pr.evaluate(10, HEAD)
    monkeypatch.setattr(merge_pr, "evaluate", lambda *_args: gates)
    snapshots = iter([pr, {**pr, **merged}])
    monkeypatch.setattr(merge_pr, "pull_request", lambda _n: next(snapshots))
    monkeypatch.setattr(merge_pr, "run", lambda _argv: None)
    monkeypatch.setattr(merge_pr, "finalize_queued", lambda *_args: pytest.fail("must not close out"))
    with pytest.raises(merge_pr.KernelError, match=reason):
        merge_pr.merge(10, HEAD)
