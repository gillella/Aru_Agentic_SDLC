from __future__ import annotations

import subprocess

import pytest

import merge_state

HEAD = "a" * 40


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
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: {
        "project_id": "PVT_1", "item_id": "PVTI_7", "status_field_id": "FIELD_1", "status": "In Review",
    })
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


@pytest.mark.parametrize("missing", ["mergeQueue", "mergeQueueEntry", "autoMergeRequest"])
def test_missing_queue_field_is_not_proof_of_direct_merge(monkeypatch, missing):
    record = dict(number=10, headRefOid=HEAD, baseRefOid="b" * 40,
                  mergeQueue=None, mergeQueueEntry=None, autoMergeRequest=None)
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(merge_state, "gh_json", lambda *_a, **_k: {
        "data": {"repository": {"pullRequest": record}},
    })
    assert merge_state.merge_queue_snapshot(10, HEAD)["configured"] is False
    del record[missing]
    with pytest.raises(merge_state.KernelError, match="incomplete or stale"):
        merge_state.merge_queue_snapshot(10, HEAD)


@pytest.mark.parametrize("mutation", ["head", "merge", "state", "missing", "bool", "wrong-node", "errors"])
def test_finalize_rejects_unproven_queue_history(monkeypatch, mutation):
    record = dict(number=10, headRefOid=HEAD, state="MERGED", mergeCommit={"oid": "c" * 40},
                  timelineItems={"nodes": [], "pageInfo": {"hasNextPage": False}})
    data = {"data": {"repository": {"pullRequest": record}}}
    if mutation == "head":
        record["headRefOid"] = "d" * 40
    elif mutation == "merge":
        record["mergeCommit"] = {"oid": "d" * 40}
    elif mutation == "state":
        record["state"] = "OPEN"
    elif mutation == "errors":
        data["errors"] = [{"message": "partial response"}]
    else:
        record["timelineItems"] = {
            "nodes": [{"__typename": "UnrelatedEvent"}] if mutation == "wrong-node" else [],
            "pageInfo": None if mutation == "missing" else {"hasNextPage": 0 if mutation == "bool" else False},
        }
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")
    def query(argv, **kwargs):
        assert "ADDED_TO_MERGE_QUEUE_EVENT" in argv[3] and "first:1" in argv[3]
        assert kwargs["auth"] == merge_state.REPOSITORY_AUTH
        return data
    monkeypatch.setattr(merge_state, "gh_json", query)
    with pytest.raises(merge_state.KernelError, match="incomplete or stale|history is incomplete"):
        merge_state.require_direct_merge_history(10, HEAD, "c" * 40)
