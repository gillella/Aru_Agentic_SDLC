from __future__ import annotations

import copy
import sys

import pytest

import triage_backlog
from common import KernelError


@pytest.fixture(autouse=True)
def no_ready_pin_writes(monkeypatch):
    """These tests cover the status transaction; tests/test_ready_pin.py covers pinning."""


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


def scoped_context(monkeypatch, records):
    live = {record["number"]: copy.deepcopy(record) for record in records}
    reads, writes = [], []

    def read(number):
        reads.append(number)
        return copy.deepcopy(live[number])

    def transition(number, status, *, expected_current=None, pre_mutation_check=None):
        assert expected_current == "Backlog"
        pre_mutation_check()
        writes.append((number, status))

    monkeypatch.setattr(triage_backlog, "issue", read)
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: pytest.fail("unscoped read"))
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _: [])
    monkeypatch.setattr(triage_backlog, "set_status", transition)
    return live, reads, writes


def test_scoped_triage_selects_one_by_priority_without_unrelated_candidates(monkeypatch):
    _, reads, writes = scoped_context(monkeypatch, [
        backlog_issue(1, "priority:p0"),
        backlog_issue(7, "priority:p1"),
        backlog_issue(8, "priority:p0"),
    ])
    assert triage_backlog.triage(issue_numbers=[7, 8]) == {"promoted": [8], "rejected": {}}
    assert reads == [7, 8, 8]
    assert writes == [(8, "Ready")]


def test_scoped_all_stays_inside_deduplicated_authorized_subset(monkeypatch):
    _, reads, writes = scoped_context(monkeypatch, [
        backlog_issue(1, "priority:p0"), backlog_issue(7), backlog_issue(8, "priority:p0"),
    ])
    result = triage_backlog.triage(issue_numbers=[7, 8, 7], promote_all=True)
    assert result == {"promoted": [8, 7], "rejected": {}}
    assert reads == [7, 8, 8, 7]
    assert writes == [(8, "Ready"), (7, "Ready")]


@pytest.mark.parametrize(("record", "message"), [
    ({**backlog_issue(7), "state": "CLOSED"}, "issue is not open"),
    ({**backlog_issue(7), "labels": [{"name": "status:ready"}]}, "must be Backlog"),
    (backlog_issue(7, "agent:other"), "claimed issues"),
    (backlog_issue(7, "needs-human"), "needs-human"),
    (backlog_issue(7, "type:epic"), "type:epic"),
    (backlog_issue(7, body="## Acceptance Criteria\n- [ ] incomplete"), "touches:"),
])
def test_invalid_scoped_item_never_falls_back(monkeypatch, record, message):
    _, reads, writes = scoped_context(monkeypatch, [record, backlog_issue(1, "priority:p0")])
    result = triage_backlog.triage(issue_numbers=[7])
    assert result["promoted"] == []
    assert any(message in error for error in result["rejected"][7])
    assert reads == [7]
    assert writes == []


def test_scoped_dependency_blocker_is_reported_without_promotion(monkeypatch):
    _, _, writes = scoped_context(monkeypatch, [backlog_issue(7)])
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _: [9])
    assert triage_backlog.triage(issue_numbers=[7]) == {
        "promoted": [], "rejected": {7: ["open dependencies: #9"]},
    }
    assert writes == []


@pytest.mark.parametrize("numbers", [[], [0], [-1], [True], ["7"], [7, 0]])
def test_invalid_scope_fails_before_authority_reads(monkeypatch, numbers):
    monkeypatch.setattr(triage_backlog, "issue", lambda _: pytest.fail("invalid scope read"))
    with pytest.raises(KernelError, match="positive issue IDs"):
        triage_backlog.triage(issue_numbers=numbers)


@pytest.mark.parametrize("change", ["body", "labels", "state", "claim"])
def test_promotion_observed_drift_refuses_before_writes(monkeypatch, change):
    live, _, writes = scoped_context(monkeypatch, [backlog_issue(7)])
    transition = triage_backlog.set_status

    def race(*args, **kwargs):
        if change == "body":
            live[7]["body"] += "\nnew scope"
        elif change == "labels":
            live[7]["labels"].append({"name": "needs-human"})
        elif change == "state":
            live[7]["state"] = "CLOSED"
        else:
            live[7]["labels"].append({"name": "agent:other"})
        transition(*args, **kwargs)

    monkeypatch.setattr(triage_backlog, "set_status", race)
    with pytest.raises(KernelError, match="changed after evaluation"):
        triage_backlog.triage(issue_numbers=[7])
    assert writes == []


def test_dependency_reopening_after_evaluation_refuses_before_writes(monkeypatch):
    record = backlog_issue(7)
    record["body"] += "\ndepends-on: #9"
    _, _, writes = scoped_context(monkeypatch, [record])
    states = iter([[], [9]])
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _: next(states))
    with pytest.raises(KernelError, match="open dependencies: #9"):
        triage_backlog.triage(issue_numbers=[7])
    assert writes == []


def test_unreadable_scoped_authority_is_an_error_not_empty_work(monkeypatch):
    def unavailable(_):
        raise KernelError("GitHub unavailable")
    monkeypatch.setattr(triage_backlog, "issue", unavailable)
    with pytest.raises(KernelError, match="GitHub unavailable"):
        triage_backlog.triage(issue_numbers=[7])


def test_cli_forwards_repeated_scope_and_explicit_batch(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys, "argv", ["triage_backlog.py", "--issue", "7", "--issue", "8", "--all", "--json"])
    def triage(**kwargs):
        calls.append(kwargs)
        return {"promoted": [], "rejected": {}}
    monkeypatch.setattr(triage_backlog, "triage", triage)
    assert triage_backlog.main() == 0
    assert calls == [{"promote_all": True, "issue_numbers": [7, 8]}]
    assert '"promoted": []' in capsys.readouterr().out
