from __future__ import annotations

import pytest

import common
import fetch_next_work


def ready_issue(number: int, *labels: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "labels": [{"name": "status:ready"}, *({"name": label} for label in labels)],
    }


def test_ready_inventory_uses_complete_pagination_and_excludes_pull_requests(
    monkeypatch,
):
    inventory = [ready_issue(number, "priority:p1") for number in range(1, 202)]
    pull_request = {**ready_issue(202, "priority:p0"), "pull_request": {}}
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, **_kwargs: calls.append(args)
        or [inventory[:100], [*inventory[100:], pull_request]],
    )

    assert fetch_next_work.ready_issues() == inventory
    assert calls == [
        [
            "api",
            "--paginate",
            "--slurp",
            "repos/owner/repository/issues?state=open&labels=status%3Aready&per_page=100",
        ]
    ]


@pytest.mark.parametrize(
    ("feedback", "ci", "labels", "expected"),
    [
        (
            [{"kind": "review", "id": 7}],
            {"state": "success", "head": "feedback-head", "checks": []},
            [],
            {
                "type": "feedback",
                "pr": 500,
                "items": [{"kind": "review", "id": 7}],
            },
        ),
        (
            [],
            {"state": "failure", "head": "ci-head", "checks": ["tests"]},
            [],
            {"type": "ci", "pr": 500, "head": "ci-head", "checks": ["tests"]},
        ),
        (
            [],
            {"state": "success", "head": "merge-head", "checks": []},
            ["review:sourcery"],
            {"type": "merge", "pr": 500, "head": "merge-head"},
        ),
    ],
)
def test_select_preserves_pr_precedence_over_ready_inventory(
    monkeypatch, feedback, ci, labels, expected
):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [
            {
                "number": 500,
                "labels": [{"name": label} for label in labels],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "fetch_feedback", lambda _number: feedback)
    monkeypatch.setattr(fetch_next_work, "ci_verdict", lambda _number: ci)
    monkeypatch.setattr(fetch_next_work, "evaluate", lambda _number, _head: None)

    def unexpected_ready_inventory():
        raise AssertionError("Ready inventory loaded before authored PR work")

    monkeypatch.setattr(
        fetch_next_work, "ready_issues", unexpected_ready_inventory, raising=False
    )

    assert fetch_next_work.select("codex-sol56-issue499") == expected


def test_select_excludes_human_and_epic_work_then_orders_by_priority(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
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
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [record])

    with pytest.raises(fetch_next_work.KernelError) as exc_info:
        fetch_next_work.select("codex-sol56-issue499")
    assert str(exc_info.value) == diagnostic
