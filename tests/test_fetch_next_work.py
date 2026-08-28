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
    monkeypatch.setattr(
        fetch_next_work,
        "has_review_comments",
        lambda _number: bool(feedback),
    )
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


def test_select_treats_missing_priority_as_p2(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(21), ready_issue(22, "priority:p3")],
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "issue",
        "issue": 21,
        "title": "issue 21",
    }


def test_select_skips_bad_priority_and_returns_scoped_diagnostic(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(22, "priority:p0", "priority:p1"),
            ready_issue(23, "priority:p1"),
        ],
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "issue",
        "issue": 23,
        "title": "issue 23",
        "diagnostics": [
            "Ready issue #22 has contradictory or unsupported priority labels; skipped"
        ],
    }


def test_select_returns_idle_diagnostic_when_only_priority_is_bad(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(22, "priority:urgent")],
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "idle",
        "diagnostics": [
            "Ready issue #22 has contradictory or unsupported priority labels; skipped"
        ],
    }


def test_select_wait_path_does_not_call_review_thread_graphql(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [{"number": 500, "labels": []}],
    )

    def fake_gh_json(args, **_kwargs):
        calls.append(list(args))
        joined = " ".join(str(part) for part in args)
        if "reviewThreads" in joined:
            raise AssertionError("wait path must not query reviewThreads")
        if args[:2] == ["api", "repos/owner/repo/pulls/500/comments?per_page=1"]:
            return []
        raise AssertionError(f"unexpected GitHub call: {args}")

    monkeypatch.setattr(fetch_next_work, "gh_json", fake_gh_json)

    def unexpected_feedback(_number):
        raise AssertionError("wait path must not dump reviewThreads")

    monkeypatch.setattr(fetch_next_work, "fetch_feedback", unexpected_feedback)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "a" * 40, "checks": []},
    )

    def unexpected_ready_inventory():
        raise AssertionError("Ready inventory loaded before authored PR work")

    monkeypatch.setattr(
        fetch_next_work, "ready_issues", unexpected_ready_inventory, raising=False
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "wait",
        "pr": 500,
        "head": "a" * 40,
        "ci": "pending",
    }
    assert calls == [["api", "repos/owner/repo/pulls/500/comments?per_page=1"]]


def test_select_feedback_still_uses_full_review_threads(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [{"number": 500, "labels": []}],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: True)
    monkeypatch.setattr(
        fetch_next_work,
        "fetch_feedback",
        lambda _number: [{"kind": "review", "id": 7}],
    )

    def unexpected_ci(_number):
        raise AssertionError("CI must not run before returning feedback")

    monkeypatch.setattr(fetch_next_work, "ci_verdict", unexpected_ci)

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "feedback",
        "pr": 500,
        "items": [{"kind": "review", "id": 7}],
    }
