from __future__ import annotations

import json
import sys

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


@pytest.mark.parametrize(
    "agents",
    [
        ["agent-a", "agent-a"],
        ["agent-a", "INVALID"],
    ],
)
def test_batch_rejects_duplicate_or_invalid_agents_before_inventory(
    monkeypatch, agents
):
    calls = []
    monkeypatch.setattr(
        fetch_next_work, "open_prs", lambda: calls.append("prs") or []
    )
    monkeypatch.setattr(
        fetch_next_work, "ready_issues", lambda: calls.append("issues") or []
    )

    with pytest.raises(common.KernelError):
        fetch_next_work.select_batch(agents)

    assert calls == []


def test_batch_requires_json_before_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch_next_work, "open_prs", lambda: calls.append("prs") or []
    )
    monkeypatch.setattr(
        fetch_next_work, "ready_issues", lambda: calls.append("issues") or []
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_next_work.py", "--agent", "agent-a", "--agent", "agent-b"],
    )

    with pytest.raises(SystemExit, match="2"):
        fetch_next_work.main()

    assert calls == []


def test_batch_reads_each_inventory_once_and_preserves_lane_pr_precedence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: calls.append("prs")
        or [
            {
                "number": 500,
                "labels": [
                    {"name": "author:agent-a"},
                    {"name": "review:sourcery"},
                ],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("issues")
        or [ready_issue(10, "priority:p0", body="touches: src/a.py")],
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: True)
    monkeypatch.setattr(
        fetch_next_work,
        "fetch_feedback",
        lambda _number: [{"kind": "review", "id": 7}],
    )

    def unexpected_ci(_number):
        raise AssertionError("feedback must retain precedence over CI")

    monkeypatch.setattr(fetch_next_work, "ci_verdict", unexpected_ci)

    assert fetch_next_work.select_batch(["agent-a", "agent-b"]) == {
        "schema": "aru.fetch-next-work.batch/v1",
        "lanes": [
            {
                "agent": "agent-a",
                "work": {
                    "type": "feedback",
                    "pr": 500,
                    "items": [{"kind": "review", "id": 7}],
                },
            },
            {
                "agent": "agent-b",
                "work": {"type": "issue", "issue": 10, "title": "issue 10"},
            },
        ],
        "diagnostics": [],
        "claim_status": "not-requested",
    }
    assert calls == ["prs", "issues"]


def test_batch_rejects_ambiguous_pr_authors_before_pr_evaluation(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "labels": [
                    {"name": "author:agent-a"},
                    {"name": "author:agent-b"},
                ],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])

    def unexpected_pr_evaluation(_pr):
        raise AssertionError("ambiguous PR authorship must fail before evaluation")

    monkeypatch.setattr(fetch_next_work, "_open_pr_work", unexpected_pr_evaluation)

    with pytest.raises(common.KernelError, match="contradictory author labels"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_deterministically_fills_lanes_with_path_disjoint_issues(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(30, "priority:p1", body="touches: later.py"),
            ready_issue(13, "priority:p0", body="touches: docs2/**"),
            ready_issue(11, "priority:p0", body="touches: docs/api/index.md"),
            ready_issue(12, "priority:p0", body="touches: src/a.py"),
            ready_issue(10, "priority:p0", body="touches: docs/**"),
        ],
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b", "agent-c"])

    assert [lane["work"].get("issue") for lane in result["lanes"]] == [10, 12, 13]


def test_batch_exact_paths_only_conflict_when_equal(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(1, body="touches: src/pkg/one.py"),
            ready_issue(2, body="touches: src/pkg/two.py"),
        ],
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert [lane["work"].get("issue") for lane in result["lanes"]] == [1, 2]


def test_batch_skips_malformed_touches_with_diagnostics(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(1, "priority:p0", body=""),
            ready_issue(2, "priority:p0", body="touches: ../secret"),
            ready_issue(3, "priority:p1", body="touches: safe.py"),
        ],
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {"type": "issue", "issue": 3, "title": "issue 3"},
        },
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready issue #1 has invalid touches: issue must contain exactly one "
        "touches: declaration; skipped",
        "Ready issue #2 has invalid touches: touches: contains an unsafe path; skipped",
    ]


def test_batch_claim_stops_after_first_failure_and_prints_partial_json(
    monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        fetch_next_work, "open_prs", lambda: calls.append("prs") or []
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("issues")
        or [ready_issue(number) for number in (1, 2, 3, 4)],
    )

    def fake_claim(number, agent):
        calls.append((number, agent))
        if number == 2:
            raise common.KernelError("claim race detected")
        return {"issue": number, "agent": agent, "status": "In Progress"}

    monkeypatch.setattr(fetch_next_work, "claim", fake_claim)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_next_work.py",
            "--agent",
            "agent-a",
            "--agent",
            "agent-b",
            "--agent",
            "agent-c",
            "--claim",
            "--json",
        ],
    )

    assert fetch_next_work.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["claim_status"] == "partial"
    assert result["lanes"][0]["work"]["claim"] == {
        "issue": 1,
        "agent": "agent-a",
        "status": "In Progress",
    }
    assert result["lanes"][1]["work"]["claim"] == {
        "status": "failed",
        "error": "claim race detected",
    }
    assert result["lanes"][2]["work"]["claim"] == {
        "status": "skipped",
        "reason": "claim stopped after earlier failure",
    }
    assert calls == ["prs", "issues", (1, "agent-a"), (2, "agent-b")]


@pytest.mark.parametrize("as_json", [False, True])
def test_single_agent_cli_output_is_exactly_legacy_compatible(
    monkeypatch, capsys, as_json
):
    work = {"type": "issue", "issue": 7, "title": "issue 7"}
    monkeypatch.setattr(fetch_next_work, "select", lambda agent: work)
    argv = ["fetch_next_work.py", "--agent", "agent-a"]
    if as_json:
        argv.append("--json")
    monkeypatch.setattr(sys, "argv", argv)

    assert fetch_next_work.main() == 0

    expected = (
        '{"agent": "agent-a", "work": {"issue": 7, "title": "issue 7", '
        '"type": "issue"}}\n'
        if as_json
        else "{'type': 'issue', 'issue': 7, 'title': 'issue 7'}\n"
    )
    assert capsys.readouterr().out == expected
