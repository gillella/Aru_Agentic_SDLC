from __future__ import annotations

import pytest

import common
import fetch_next_work


@pytest.fixture(autouse=True)
def authoritative_backlog_project_status(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "project_item_status",
        lambda _number: "Backlog",
        raising=False,
    )


def backlog_issue(number: int, *labels: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": (
            "## Acceptance Criteria\n"
            "- [ ] Recover valid work\n\n"
            f"touches: src/{number}.py"
        ),
        "state": "OPEN",
        "labels": [{"name": "status:backlog"}, *({"name": label} for label in labels)],
    }


@pytest.mark.parametrize("status", [None, "Ready", "In Progress"])
def test_single_recovery_skips_non_backlog_project_status_for_later_candidate(
    monkeypatch, status
):
    promoted = []
    records = [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p1")]
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: records)
    monkeypatch.setattr(
        fetch_next_work,
        "project_item_status",
        lambda number: status if number == 12 else "Backlog",
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    result = fetch_next_work._recover_single_issue()

    assert result["issue"] == 13
    assert promoted == [13]
    expected = "is unset" if status is None else f"is {status!r}, expected 'Backlog'"
    assert f"Backlog issue #12 Project card status {expected}; skipped" in result["diagnostics"]


def test_single_recovery_requires_exactly_one_backlog_status_label(monkeypatch):
    promoted = []
    records = [
        backlog_issue(12, "priority:p0", "status:ready"),
        backlog_issue(13, "priority:p1"),
    ]
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: records)
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    result = fetch_next_work._recover_single_issue()

    assert result["issue"] == 13
    assert promoted == [13]
    assert (
        "Backlog issue #12 issue must have exactly one status:backlog label; skipped"
        in result["diagnostics"]
    )


def test_single_recovery_skips_off_board_issue_for_later_candidate(monkeypatch):
    promoted = []
    records = [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p1")]
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: records)

    def project_status(number):
        if number == 12:
            raise common.KernelError(
                "issue #12 is not a member of the linked Project Board"
            )
        return "Backlog"

    monkeypatch.setattr(fetch_next_work, "project_item_status", project_status)
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    result = fetch_next_work._recover_single_issue()

    assert result["issue"] == 13
    assert promoted == [13]
    assert (
        "Backlog issue #12 issue is not a member of the linked Project Board; skipped"
        in result["diagnostics"]
    )


def test_single_recovery_skips_pre_mutation_drift_for_later_candidate(monkeypatch):
    promoted = []
    records = [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p1")]
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: records)

    def promote(number, pre_mutation_check=None, **_kwargs):
        if number == 12:
            raise fetch_next_work._RecoveryDriftError("issue is already claimed")
        promoted.append(number)

    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", promote)

    result = fetch_next_work._recover_single_issue()

    assert result["issue"] == 13
    assert promoted == [13]
    assert "Backlog issue #12 issue is already claimed; skipped" in result["diagnostics"]


def test_batch_recovery_skips_conflict_for_later_candidate(monkeypatch):
    promoted = []
    records = [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p1")]
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: records)
    records[0]["body"] = records[0]["body"].replace("src/12.py", "docs/active.md")
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    candidates, diagnostics, _classification = fetch_next_work._recover_batch_candidates(
        1, [["docs/**"]]
    )

    assert candidates[0][1] == 13
    assert promoted == [13]
    assert (
        "Backlog issue #12 touches conflict with active lane work; skipped"
        in diagnostics
    )


def test_batch_idle_recovery_rechecks_live_touches_against_reserved_paths(monkeypatch):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [{
            "number": 12,
            "title": "issue 12",
            "body": "## Acceptance Criteria\n- [ ] Snapshot\n\ntouches: src/a.py",
            "state": "OPEN",
            "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
        }],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: {
            "number": 12,
            "title": "issue 12",
            "body": "## Acceptance Criteria\n- [ ] Live\n\ntouches: docs/guide.md",
            "state": "OPEN",
            "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
        } if args == ["api", "repos/owner/repository/issues/12"] else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: promoted.append(number)
        or pre_mutation_check(),
    )

    candidates, diagnostics, classification = fetch_next_work._recover_batch_candidates(
        1, [["docs/**"]]
    )

    assert candidates == []
    assert diagnostics == [
        "Ready idle; evaluated Backlog once",
        "Backlog issue #12 touches conflict with active lane work; skipped",
    ]
    assert classification == {
        "total_ready": 0,
        "executable_ready": 0,
        "human_gated": 0,
        "epics": 0,
        "dependency_blocked": 0,
        "malformed": 0,
    }
    assert promoted == [12]
