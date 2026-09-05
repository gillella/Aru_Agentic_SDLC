from __future__ import annotations

import subprocess

import pytest

import merge_state


@pytest.fixture
def semantic_issue(monkeypatch):
    record = {
        "state": "OPEN",
        "labels": [{"name": "status:in-review"}, {"name": "agent:writer"}],
        "body": "## Acceptance Criteria\n- [x] Reject unauthorized writes\n\n"
                "## Scope\ntouches: src/app.py\n",
    }
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    monkeypatch.setattr(merge_state, "issue", lambda _number: dict(record))
    return record


def test_same_count_acceptance_semantic_edit_changes_evidence(semantic_issue):
    before = merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] = semantic_issue["body"].replace("Reject", "Permit")
    after = merge_state.issue_gate([7], ["src/app.py"])
    assert before[0]["criteria"] == after[0]["criteria"] == 1
    assert before != after
    assert before[0]["acceptance"] == [{"done": True, "text": "Reject unauthorized writes"}]
    assert after[0]["acceptance"] == [{"done": True, "text": "Permit unauthorized writes"}]


def test_acceptance_completion_drift_fails_closed(semantic_issue):
    merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] = semantic_issue["body"].replace("[x]", "[ ]")
    with pytest.raises(merge_state.KernelError, match="incomplete Acceptance Criteria"):
        merge_state.issue_gate([7], ["src/app.py"])


def test_issue_evidence_ignores_prose_outside_acceptance(semantic_issue):
    before = merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] += "\n## Evidence\nAdditional successful verification.\n"
    assert merge_state.issue_gate([7], ["src/app.py"]) == before
