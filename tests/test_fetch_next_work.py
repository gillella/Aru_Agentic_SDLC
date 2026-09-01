from __future__ import annotations

import sys

import pytest

import common
import fetch_next_work


@pytest.fixture(autouse=True)
def no_existing_claim(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "claimed_issue", lambda _agent: None)


def ready_issue(number: int, *labels: str, body: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body
        if body is not None
        else (
            "## Acceptance Criteria\n- [ ] Complete the change\n\n"
            f"touches: issue-{number}.txt"
        ),
        "state": "OPEN",
        "labels": [{"name": "status:ready"}, *({"name": label} for label in labels)],
    }


def ready_counts(total, executable, human=0, epics=0, blocked=0, malformed=0):
    return dict(
        total_ready=total,
        executable_ready=executable,
        human_gated=human,
        epics=epics,
        dependency_blocked=blocked,
        malformed=malformed,
    )


def test_ready_inventory_is_paginated_and_excludes_pull_requests(monkeypatch):
    inventory = [ready_issue(number, "priority:p1") for number in range(1, 202)]
    pull_request = {**ready_issue(202, "priority:p0"), "pull_request": {}}
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, **_kwargs: (
            calls.append(args) or [inventory[:100], [*inventory[100:], pull_request]]
        ),
    )

    assert fetch_next_work.ready_issues() == inventory
    assert calls == [[
        "api",
        "--paginate",
        "--slurp",
        "repos/owner/repository/issues?state=open&labels=status%3Aready&per_page=100",
    ]]


@pytest.mark.parametrize(
    ("feedback", "verification", "merge_state", "expected"),
    [
        (
            [{"kind": "review", "id": 7}],
            {"state": "success", "head": "feedback-head", "checks": []},
            "CLEAN",
            {"type": "feedback", "pr": 500, "items": [{"kind": "review", "id": 7}]},
        ),
        (
            [],
            {"state": "failure", "head": "verify-head", "checks": ["tests"]},
            "CLEAN",
            {
                "type": "verification",
                "pr": 500,
                "head": "verify-head",
                "checks": ["tests"],
            },
        ),
        (
            [],
            {"state": "success", "head": "merge-head", "checks": []},
            "CLEAN",
            {"type": "merge", "pr": 500, "head": "merge-head"},
        ),
        (
            [],
            {"state": "pending", "head": "wait-head", "checks": []},
            "CLEAN",
            {
                "type": "wait",
                "pr": 500,
                "head": "wait-head",
                "verification": "pending",
            },
        ),
    ],
)
def test_authored_pr_always_precedes_ready_inventory(
    monkeypatch, feedback, verification, merge_state, expected
):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [{"number": 500, "mergeStateStatus": merge_state, "labels": []}],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: bool(feedback))
    monkeypatch.setattr(fetch_next_work, "fetch_feedback", lambda _number: feedback)
    monkeypatch.setattr(fetch_next_work, "ci_verdict", lambda _number: verification)
    monkeypatch.setattr(fetch_next_work, "evaluate", lambda _number, _head: None)
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: (_ for _ in ()).throw(AssertionError("Ready inventory must not be read")),
    )

    assert fetch_next_work.select("codex-sol56") == expected


def test_dirty_authored_pr_routes_conflict_before_failed_verification(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "authored_prs",
        lambda _agent: [{"number": 500, "mergeStateStatus": "DIRTY", "labels": []}],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "failure", "head": "dirty-head", "checks": ["tests"]},
    )

    assert fetch_next_work.select("codex-sol56") == {
        "type": "conflict",
        "pr": 500,
        "head": "dirty-head",
        "reason": "PR merge state is DIRTY",
    }


def test_live_merge_state_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda *_args, **_kwargs: {
            "number": 17,
            "headRefOid": "a" * 40,
            "state": "OPEN",
            "mergeStateStatus": "BROKEN",
        },
    )
    with pytest.raises(common.KernelError, match="malformed pull request reread"):
        fetch_next_work._pr_live_merge_state(17, "a" * 40)


def test_risk_escalation_without_authority_routes_one_explicit_refresh(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: False)
    monkeypatch.setattr(
        fetch_next_work,
        "ci_verdict",
        lambda _number: {"state": "success", "head": "a" * 40, "checks": []},
    )
    monkeypatch.setattr(
        fetch_next_work,
        "evaluate",
        lambda *_args: (_ for _ in ()).throw(
            common.KernelError("PR must have exactly one review:<authority> label")
        ),
    )
    monkeypatch.setattr(
        fetch_next_work,
        "reviewer_continuation",
        lambda _number: {
            "authority": None,
            "next_action": "refresh-reviewer",
            "retry_at": "2026-09-01T12:00:00+00:00",
        },
    )

    result = fetch_next_work._open_pr_work(
        {"number": 500, "mergeStateStatus": "CLEAN"}
    )

    assert result["type"] == "wait"
    assert result["next_action"] == "refresh-reviewer"
    assert result["authority"] is None


def test_ready_selection_skips_human_and_epic_and_orders_by_priority(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(16, "needs-human", "priority:p0"),
            ready_issue(45, "type:epic", "priority:p0"),
            ready_issue(12, "priority:p1"),
            ready_issue(99, "priority:p0"),
            ready_issue(70, "priority:p0"),
        ],
    )
    monkeypatch.setattr(fetch_next_work, "dependency_states", lambda _records: {})

    assert fetch_next_work.select("codex-sol56") == {
        "type": "issue",
        "issue": 70,
        "title": "issue 70",
    }


def test_ready_selection_requires_complete_contract(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(1, "priority:p0", body="touches: src/a.py"),
            ready_issue(2, "priority:p1"),
        ],
    )
    monkeypatch.setattr(fetch_next_work, "dependency_states", lambda _records: {})

    result = fetch_next_work.select("codex-sol56")

    assert result["issue"] == 2
    assert result["diagnostics"] == [
        "Ready issue #1 Acceptance Criteria must contain an unchecked item; skipped"
    ]


def test_ready_selection_reports_the_live_bottleneck(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            *(ready_issue(number, "needs-human") for number in range(1, 4)),
            ready_issue(4, "type:epic"),
            ready_issue(5, body="## Acceptance Criteria\n- [ ] Missing scope"),
        ],
    )
    monkeypatch.setattr(fetch_next_work, "dependency_states", lambda _records: {})

    result = fetch_next_work.select("codex-sol56")

    assert result["type"] == "idle"
    assert result["ready_classification"] == ready_counts(
        5, 0, human=3, epics=1, malformed=1
    )
    assert result["diagnostics"][0].startswith("Ready classification: total=5")


def test_open_dependency_blocks_and_closed_dependency_selects(monkeypatch):
    record = ready_issue(
        10,
        body=(
            "## Acceptance Criteria\n- [ ] Complete after dependency\n\n"
            "depends-on: #90\n"
            "touches: src/a.py"
        ),
    )
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [record])
    monkeypatch.setattr(fetch_next_work, "dependency_states", lambda _records: {90: "open"})
    assert fetch_next_work.select("codex-sol56")["type"] == "idle"

    monkeypatch.setattr(fetch_next_work, "dependency_states", lambda _records: {90: "closed"})
    assert fetch_next_work.select("codex-sol56") == {
        "type": "issue",
        "issue": 10,
        "title": "issue 10",
    }


def test_claimed_issue_precedes_ready_and_resumes_without_reclaim(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "claimed_issue",
        lambda _agent: {
            "number": 33,
            "title": "resume me",
            "state": "OPEN",
            "labels": [
                {"name": "agent:codex-sol56"},
                {"name": "status:in-progress"},
            ],
        },
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: pytest.fail("Ready must not be read while a claim is active"),
    )
    assert fetch_next_work.select("codex-sol56") == {
        "type": "claimed_issue",
        "issue": 33,
        "title": "resume me",
        "next_action": "resume-implementation",
    }


def test_merged_queued_claim_routes_explicit_finalization(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "claimed_issue",
        lambda _agent: {
            "number": 33,
            "title": "merged",
            "state": "CLOSED",
            "labels": [
                {"name": "agent:codex-sol56"},
                {"name": "status:in-review"},
            ],
        },
    )
    monkeypatch.setattr(
        fetch_next_work,
        "merged_closing_pr",
        lambda _number: {"pr": 44, "head": "a" * 40},
    )
    assert fetch_next_work.select("codex-sol56") == {
        "type": "finalize",
        "issue": 33,
        "pr": 44,
        "head": "a" * 40,
        "next_action": "finalize-queued-merge",
    }


def test_cli_is_single_lane_and_read_only(monkeypatch, capsys):
    monkeypatch.setattr(fetch_next_work, "select", lambda agent: {"type": "idle", "agent": agent})
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_next_work.py", "--agent", "agent-a", "--json"],
    )

    assert fetch_next_work.main() == 0
    assert '"agent": "agent-a"' in capsys.readouterr().out

    parser_argv = [
        "fetch_next_work.py",
        "--agent",
        "agent-a",
        "--agent",
        "agent-b",
    ]
    monkeypatch.setattr(sys, "argv", parser_argv)
    with pytest.raises(SystemExit):
        fetch_next_work.main()
