from __future__ import annotations

import pytest

import triage_backlog
from common import KernelError


def backlog_issue(number: int, *labels: str, body: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body if body is not None else (
            "## Acceptance Criteria\n"
            "- [ ] Promote only mechanically eligible backlog work\n\n"
            f"touches: src/{number}.py"
        ),
        "state": "OPEN",
        "labels": [{"name": "status:backlog"}, *({"name": label} for label in labels)],
    }


def test_backlog_candidates_sort_by_priority_then_issue_number(monkeypatch):
    monkeypatch.setattr(
        triage_backlog,
        "list_issues",
        lambda **_kwargs: [
            backlog_issue(50, "priority:p1"),
            backlog_issue(7, "priority:p0"),
            backlog_issue(8),
            backlog_issue(6, "priority:p0"),
        ],
    )
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _record: [])

    candidates, rejected = triage_backlog.backlog_candidates()

    assert [int(record["number"]) for _priority, record in candidates] == [6, 7, 50, 8]
    assert rejected == {}


@pytest.mark.parametrize(
    ("record", "message"),
        [
            (backlog_issue(1, "needs-human"), "needs-human issues cannot enter Ready"),
            (backlog_issue(2, "type:epic"), "type:epic issues cannot enter Ready"),
            (
                backlog_issue(
                    3,
                    body="## Acceptance Criteria\n- [ ] Valid AC only\n",
                ),
                "issue must contain exactly one touches: declaration",
            ),
            (backlog_issue(4, "priority:urgent"), "issue may have at most one supported priority:p0..p3 label"),
        ],
)
def test_backlog_candidates_reject_mechanically_ineligible_records(
    monkeypatch, record, message
):
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_kwargs: [record])
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _record: [])

    candidates, rejected = triage_backlog.backlog_candidates()

    assert candidates == []
    assert rejected == {int(record["number"]): [message]}


def test_backlog_candidates_reject_open_dependencies(monkeypatch):
    record = backlog_issue(
        9,
        body=(
            "## Acceptance Criteria\n"
            "- [ ] Dependency still open\n\n"
            "depends-on: #90\n"
            "touches: src/9.py"
        ),
    )
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_kwargs: [record])
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _record: [90])

    candidates, rejected = triage_backlog.backlog_candidates()

    assert candidates == []
    assert rejected == {9: ["open dependencies: #90"]}


def test_backlog_candidates_reject_malformed_dependencies_and_keep_later_candidate(
    monkeypatch,
):
    malformed = backlog_issue(
        9,
        "priority:p0",
        body=(
            "## Acceptance Criteria\n"
            "- [ ] Reject malformed dependency syntax\n\n"
            "depends-on: #90, #91\n"
            "touches: src/9.py"
        ),
    )
    valid = backlog_issue(10, "priority:p1")

    candidates, rejected = triage_backlog.backlog_candidates(
        [malformed, valid], issue_states={}
    )

    assert [record["number"] for _priority, record in candidates] == [10]
    assert rejected == {
        9: ["depends-on declarations must each match 'depends-on: #N'"]
    }


def test_backlog_candidates_preserve_legacy_evaluate_seam(monkeypatch):
    records = [
        {"number": 2, "title": "blocked"},
        {"number": 3, "title": "ready"},
        {"number": 4, "title": "also ready"},
    ]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_kwargs: records)
    monkeypatch.setattr(
        triage_backlog,
        "evaluate",
        lambda item: ["missing"] if item["number"] == 2 else [],
    )

    candidates, rejected = triage_backlog.backlog_candidates()

    assert [int(record["number"]) for _priority, record in candidates] == [3, 4]
    assert rejected == {2: ["missing"]}


def test_promote_issue_uses_backlog_precondition(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage_backlog,
        "set_status",
        lambda number, status, expected_current=None, pre_mutation_check=None: calls.append(
            (number, status, expected_current, pre_mutation_check)
        ),
    )

    triage_backlog.promote_issue(15)

    assert calls == [(15, "Ready", "Backlog", None)]


def test_promote_issue_forwards_pre_mutation_check(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setattr(
        triage_backlog,
        "set_status",
        lambda number, status, expected_current=None, pre_mutation_check=None: calls.append(
            (number, status, expected_current, pre_mutation_check)
        ),
    )

    triage_backlog.promote_issue(15, pre_mutation_check=lambda: sentinel)

    assert calls[0][:3] == (15, "Ready", "Backlog")
    assert callable(calls[0][3])


def test_promote_issue_requires_transactional_set_status_api(monkeypatch):
    def legacy_set_status(number, status):
        raise AssertionError((number, status))

    monkeypatch.setattr(triage_backlog, "set_status", legacy_set_status)

    with pytest.raises(KernelError, match="transactional set_status API"):
        triage_backlog.promote_issue(15)


def test_promote_issue_propagates_runtime_typeerror_from_transactional_setter(monkeypatch):
    def transactional_set_status(
        number,
        status,
        *,
        expected_current=None,
        pre_mutation_check=None,
    ):
        raise TypeError("internal callback failure")

    monkeypatch.setattr(triage_backlog, "set_status", transactional_set_status)

    with pytest.raises(TypeError, match="internal callback failure"):
        triage_backlog.promote_issue(15)


def test_promote_issue_avoids_signature_introspection_on_setter(monkeypatch):
    calls = []

    class SignatureTrap:
        @property
        def __signature__(self):
            raise AssertionError("promote_issue must not inspect runtime signatures")

        def __call__(
            self,
            number,
            status,
            *,
            expected_current=None,
            pre_mutation_check=None,
        ):
            calls.append((number, status, expected_current, pre_mutation_check))

    monkeypatch.setattr(triage_backlog, "set_status", SignatureTrap())

    triage_backlog.promote_issue(15)

    assert calls == [(15, "Ready", "Backlog", None)]
