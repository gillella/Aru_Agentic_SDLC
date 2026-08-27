from __future__ import annotations

import pytest

import fetch_next_work


def ready_issue(number: int, *labels: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "labels": [{"name": "status:ready"}, *({"name": label} for label in labels)],
    }


def test_select_excludes_human_and_epic_work_then_orders_by_priority(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "list_issues",
        lambda **_kwargs: [
            ready_issue(16, "needs-human", "priority:p0"),
            ready_issue(45, "type:epic", "priority:p1"),
            ready_issue(12, "priority:p1"),
            ready_issue(99, "priority:p0"),
            ready_issue(70, "priority:p0"),
        ],
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "issue",
        "issue": 70,
        "title": "issue 70",
    }


@pytest.mark.parametrize(
    ("record", "diagnostic"),
    [
        (ready_issue(21), "Ready issue #21 must have exactly one priority:p0..p3 label; found 0"),
        (
            ready_issue(22, "priority:p0", "priority:p1"),
            "Ready issue #22 must have exactly one priority:p0..p3 label; found 2",
        ),
    ],
)
def test_select_fails_closed_when_claimable_issue_priority_is_not_unique(
    monkeypatch, record, diagnostic
):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "list_issues", lambda **_kwargs: [record])

    with pytest.raises(fetch_next_work.KernelError) as exc_info:
        fetch_next_work.select("codex-sol56-issue499")
    assert str(exc_info.value) == diagnostic
