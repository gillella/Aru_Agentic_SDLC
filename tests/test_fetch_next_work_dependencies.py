from __future__ import annotations

import pytest

import common
import fetch_next_work
from test_fetch_next_work import ready_issue


def test_dependency_states_use_one_bounded_bulk_query(monkeypatch):
    records = [
        ready_issue(
            1,
            body=(
                "## Acceptance Criteria\n- [ ] Wait\n\n"
                "depends-on: #90\n"
                "depends-on: #91\n"
                "touches: src/a.py"
            ),
        )
    ]
    response = {
        "data": {
            "repository": {
                "issue_90": {"number": 90, "state": "OPEN"},
                "issue_91": {"number": 91, "state": "CLOSED"},
            }
        }
    }
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or response,
    )

    assert fetch_next_work.dependency_states(records) == {90: "open", 91: "closed"}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"errors": ["partial"], "data": {"repository": {}}},
        {"data": {"repository": {}}},
        {"data": None},
    ],
)
def test_dependency_states_fail_closed_on_partial_inventory(monkeypatch, response):
    record = ready_issue(
        1,
        body=(
            "## Acceptance Criteria\n- [ ] Wait\n\n"
            "depends-on: #90\n"
            "touches: src/a.py"
        ),
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_args, **_kwargs: response)

    with pytest.raises(common.KernelError, match="incomplete"):
        fetch_next_work.dependency_states([record])


def test_dependency_states_accept_exactly_one_hundred_references(monkeypatch):
    numbers = range(1, 101)
    body = (
        "## Acceptance Criteria\n- [ ] Wait\n\n"
        + "\n".join(f"depends-on: #{number}" for number in numbers)
        + "\ntouches: src/a.py"
    )
    response = {
        "data": {
            "repository": {
                f"issue_{number}": {"number": number, "state": "CLOSED"}
                for number in numbers
            }
        }
    }
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_args, **_kwargs: response)

    assert len(fetch_next_work.dependency_states([ready_issue(101, body=body)])) == 100


def test_dependency_states_reject_more_than_one_hundred_references():
    body = (
        "## Acceptance Criteria\n- [ ] Wait\n\n"
        + "\n".join(f"depends-on: #{number}" for number in range(1, 102))
        + "\ntouches: src/a.py"
    )
    with pytest.raises(common.KernelError, match="exceeds 100"):
        fetch_next_work.dependency_states([ready_issue(102, body=body)])


def test_null_dependency_is_not_misclassified_as_closed(monkeypatch):
    record = ready_issue(
        1,
        body=(
            "## Acceptance Criteria\n- [ ] Wait\n\n"
            "depends-on: #90\n"
            "touches: src/a.py"
        ),
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda *_args, **_kwargs: {"data": {"repository": {"issue_90": None}}},
    )

    states = fetch_next_work.dependency_states([record])
    candidates, _diagnostics, classification = fetch_next_work._ready_candidates(
        [record], states
    )

    assert candidates == []
    assert classification["dependency_blocked"] == 1
