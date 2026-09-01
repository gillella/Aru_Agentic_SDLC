from __future__ import annotations

import pytest

import common
import fetch_next_work
import fetch_pr_feedback
from test_fetch_next_work import ready_issue


def test_review_thread_graphql_uses_repository_authority(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    calls = []

    def fake_gh_json(args, *, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(fetch_pr_feedback, "gh_json", fake_gh_json)

    assert fetch_pr_feedback.fetch_feedback(17) == []
    assert calls[0][1] == common.REPOSITORY_AUTH


def test_select_authored_pr_dirty_merge_state_returns_conflict_remediation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [
            {"number": 500, "mergeStateStatus": "DIRTY", "labels": [{"name": "review:sourcery"}]}
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "dirty-head-sha", "checks": []},
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "conflict",
        "pr": 500,
        "head": "dirty-head-sha",
        "reason": "PR merge state is DIRTY",
    }


def test_select_authored_pr_evaluate_dirty_merge_state_returns_conflict_remediation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [
            {"number": 500, "mergeStateStatus": "CLEAN", "labels": [{"name": "review:sourcery"}]}
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "success", "head": "merge-head-sha", "checks": []},
    )

    def failing_evaluate(number, head):
        raise common.KernelError("PR merge state is DIRTY")

    monkeypatch.setattr(fetch_next_work, "evaluate", failing_evaluate)

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "conflict",
        "pr": 500,
        "head": "merge-head-sha",
        "reason": "PR merge state is DIRTY",
    }


@pytest.mark.parametrize("bad_status", ["dirty", "BROKEN"])
def test_select_authored_pr_malformed_snapshot_merge_state_fails_closed(monkeypatch, bad_status):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [
            {"number": 500, "mergeStateStatus": bad_status, "labels": [{"name": "review:sourcery"}]}
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "a" * 40, "checks": []},
    )

    with pytest.raises(
        common.KernelError, match=r"GitHub returned malformed pull request snapshot for #500"
    ):
        fetch_next_work.select("codex-sol56-issue499")


@pytest.mark.parametrize("bad_status", ["dirty", "BROKEN"])
def test_batch_authored_pr_malformed_snapshot_merge_state_fails_closed(monkeypatch, bad_status):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "mergeStateStatus": bad_status,
                "labels": [{"name": "author:agent-a"}, {"name": "review:sourcery"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "a" * 40, "checks": []},
    )

    with pytest.raises(
        common.KernelError, match=r"GitHub returned malformed pull request snapshot for #500"
    ):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


@pytest.mark.parametrize(
    "valid_status",
    ["BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"],
)
def test_open_pr_work_accepts_valid_snapshot_statuses(monkeypatch, valid_status):
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "a" * 40, "checks": []},
    )
    work = fetch_next_work._open_pr_work(
        {"number": 500, "mergeStateStatus": valid_status, "labels": []}
    )
    expected_type = "conflict" if valid_status == "DIRTY" else "wait"
    assert work["type"] == expected_type


def test_batch_authored_pr_dirty_merge_state_returns_conflict_remediation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "mergeStateStatus": "DIRTY",
                "labels": [{"name": "author:agent-a"}, {"name": "review:sourcery"}],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(10, "priority:p0", body="touches: src/free.py")],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "pending", "head": "dirty-head-sha", "checks": []},
    )
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: {
            "number": number,
            "title": f"issue {number}",
            "body": "touches: docs/reserved.md",
            "labels": [{"name": "status:in-progress"}],
        },
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {
                "type": "conflict",
                "pr": 500,
                "head": "dirty-head-sha",
                "reason": "PR merge state is DIRTY",
            },
        },
        {"agent": "agent-b", "work": {"type": "issue", "issue": 10, "title": "issue 10"}},
    ]


def test_select_authored_pr_dirty_merge_state_overrides_ci_failure(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [
            {"number": 500, "mergeStateStatus": "DIRTY", "labels": [{"name": "review:sourcery"}]}
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "failure", "head": "dirty-head-sha", "checks": ["tests"]},
    )

    assert fetch_next_work.select("codex-sol56-issue499") == {
        "type": "conflict",
        "pr": 500,
        "head": "dirty-head-sha",
        "reason": "PR merge state is DIRTY",
    }


def test_batch_authored_pr_dirty_merge_state_overrides_ci_failure(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "mergeStateStatus": "DIRTY",
                "labels": [{"name": "author:agent-a"}, {"name": "review:sourcery"}],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(10, "priority:p0", body="touches: src/free.py")],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "failure", "head": "dirty-head-sha", "checks": ["tests"]},
    )
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: {
            "number": number,
            "title": f"issue {number}",
            "body": "touches: docs/reserved.md",
            "labels": [{"name": "status:in-progress"}],
        },
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {
                "type": "conflict",
                "pr": 500,
                "head": "dirty-head-sha",
                "reason": "PR merge state is DIRTY",
            },
        },
        {"agent": "agent-b", "work": {"type": "issue", "issue": 10, "title": "issue 10"}},
    ]


def test_batch_occupied_only_not_blocked_by_unrelated_malformed_reservation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "mergeStateStatus": "CLEAN",
                "labels": [{"name": "author:agent-a"}, {"name": "review:sourcery"}],
            },
            {
                "number": 501,
                "body": "Closes #81",
                "mergeStateStatus": "CLEAN",
                "labels": [{"name": "author:agent-b"}, {"name": "review:sourcery"}],
            },
            {
                "number": 502,
                "body": "Malformed: no closes directive",
                "labels": [{"name": "author:agent-c"}],
            },
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda number: {"state": "pending", "head": f"head-{number}", "checks": []},
    )

    def unexpected_ready_inventory():
        raise AssertionError("Ready inventory loaded when all batch lanes are occupied")

    monkeypatch.setattr(fetch_next_work, "ready_issues", unexpected_ready_inventory, raising=False)

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result == {
        "schema": "aru.fetch-next-work.batch/v1",
        "lanes": [
            {
                "agent": "agent-a",
                "work": {"type": "wait", "pr": 500, "head": "head-500", "ci": "pending"},
            },
            {
                "agent": "agent-b",
                "work": {"type": "wait", "pr": 501, "head": "head-501", "ci": "pending"},
            },
        ],
        "diagnostics": [],
        "claim_status": "not-requested",
    }


def test_batch_free_lane_assignment_blocked_by_malformed_global_reservation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "mergeStateStatus": "CLEAN",
                "labels": [{"name": "author:agent-a"}, {"name": "review:sourcery"}],
            },
            {
                "number": 502,
                "body": "Malformed: no closes directive",
                "labels": [{"name": "author:agent-c"}],
            },
        ],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda number: {"state": "pending", "head": f"head-{number}", "checks": []},
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(10, "priority:p0", body="touches: src/free.py")],
    )

    with pytest.raises(common.KernelError, match="Open PR #502 has no linked issue"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])
