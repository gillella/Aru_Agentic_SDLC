from __future__ import annotations

import pytest

import common
import fetch_next_work


def ready_issue(number: int, *labels: str, body: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body if body is not None else f"touches: issue-{number}.txt",
        "labels": [{"name": "status:ready"}, *({"name": label} for label in labels)],
    }


def test_dependency_states_use_one_bounded_bulk_query(monkeypatch):
    calls = []
    records = [ready_issue(1, body="depends-on: #90\ndepends-on: #91")]
    valid = {"data": {"repository": {
        "issue_90": {"number": 90, "state": "OPEN"},
        "issue_91": {"number": 91, "state": "CLOSED"},
    }}}
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or valid,
    )

    assert fetch_next_work.dependency_states(records) == {90: "open", 91: "closed"}
    assert len(calls) == 1
    for response in ({"errors": ["partial"], **valid}, {"data": {"repository": {}}}):
        monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_a, **_k: response)
        with pytest.raises(common.KernelError, match="incomplete"):
            fetch_next_work.dependency_states(records)
    body = "\n".join(f"depends-on: #{number}" for number in range(1, 102))
    assert fetch_next_work.dependency_states([ready_issue(1, "needs-human", body=body)]) == {}
    with pytest.raises(common.KernelError, match="exceeds 100"):
        fetch_next_work.dependency_states([ready_issue(1, body=body)])


def test_dependency_states_accepts_exactly_100_references(monkeypatch):
    numbers = range(1, 101)
    body = "\n".join(f"depends-on: #{number}" for number in numbers)
    response = {"data": {"repository": {
        f"issue_{number}": {"number": number, "state": "CLOSED"}
        for number in numbers
    }}}
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or response,
    )

    assert fetch_next_work.dependency_states([ready_issue(101, body=body)]) == {
        number: "closed" for number in numbers
    }
    assert len(calls) == 1


def test_null_dependency_alias_leaves_missing_state_and_classifies_blocked(monkeypatch):
    record = ready_issue(1, body="depends-on: #90\ntouches: src/a.py")
    response = {"data": {"repository": {"issue_90": None}}}
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_args, **_kwargs: response)

    states = fetch_next_work.dependency_states([record])
    candidates, diagnostics, classification = fetch_next_work._ready_candidates(
        [record], states
    )

    assert 90 not in states
    assert candidates == []
    assert diagnostics == []
    assert classification == {
        "total_ready": 1,
        "executable_ready": 0,
        "human_gated": 0,
        "epics": 0,
        "dependency_blocked": 1,
        "malformed": 0,
    }
