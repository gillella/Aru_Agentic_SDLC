from __future__ import annotations

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


def ready_counts(total, executable, human=0, epics=0, blocked=0, malformed=0):
    return dict(
        total_ready=total,
        executable_ready=executable,
        human_gated=human,
        epics=epics,
        dependency_blocked=blocked,
        malformed=malformed,
    )


def test_ready_inventory_uses_complete_pagination_and_excludes_pull_requests(monkeypatch):
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
    url = "repos/owner/repository/issues?state=open&labels=status%3Aready&per_page=100"
    assert calls == [["api", "--paginate", "--slurp", url]]


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

    monkeypatch.setattr(fetch_next_work, "ready_issues", unexpected_ready_inventory, raising=False)

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


def test_select_preserves_legacy_malformed_touches_behavior(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(21, "priority:p0", body="")],
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
        "ready_classification": ready_counts(1, 0, malformed=1),
        "diagnostics": [
            "Ready classification: total=1, executable=0, human-gated=0, "
            "epics=0, dependency-blocked=0, malformed=1",
            "Ready issue #22 has contradictory or unsupported priority labels; skipped",
        ],
    }


def test_select_explains_seven_ready_with_zero_executable_from_one_snapshot(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: (
            calls.append("issues")
            or [
                *(ready_issue(number, "needs-human") for number in range(1, 6)),
                ready_issue(6, "type:epic"),
                ready_issue(7, "type:epic"),
            ]
        ),
    )

    result = fetch_next_work.select("codex-sol56-issue531")

    assert result["type"] == "idle"
    assert result["ready_classification"] == ready_counts(7, 0, human=5, epics=2)
    assert calls == ["issues"]


@pytest.mark.parametrize(("state", "work_type"), [("open", "idle"), ("closed", "issue")])
def test_select_classifies_open_dependency_as_blocked_and_closed_as_executable(
    monkeypatch, state, work_type
):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: (
            calls.append("issues") or [ready_issue(10, body="depends-on: #90\ntouches: src/a.py")]
        ),
    )
    monkeypatch.setattr(
        fetch_next_work,
        "dependency_states",
        lambda _records: calls.append("dependencies") or {90: state},
    )

    result = fetch_next_work.select("codex-sol56-issue531")

    assert result["type"] == work_type
    if state == "open":
        assert result["ready_classification"] == ready_counts(1, 0, blocked=1)
    else:
        assert result == {"type": "issue", "issue": 10, "title": "issue 10"}
    assert calls == ["issues", "dependencies"]


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

    monkeypatch.setattr(fetch_next_work, "ready_issues", unexpected_ready_inventory, raising=False)

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
def test_batch_rejects_duplicate_or_invalid_agents_before_inventory(monkeypatch, agents):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: calls.append("prs") or [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: calls.append("issues") or [])

    with pytest.raises(common.KernelError):
        fetch_next_work.select_batch(agents)

    assert calls == []


def test_batch_requires_json_before_inventory(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: calls.append("prs") or [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: calls.append("issues") or [])
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
        lambda: (
            calls.append("prs")
            or [
                {
                    "number": 500,
                    "body": "Closes #80",
                    "labels": [
                        {"name": "author:agent-a"},
                        {"name": "review:sourcery"},
                    ],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: (
            calls.append("issues") or [ready_issue(10, "priority:p0", body="touches: src/a.py")]
        ),
    )
    monkeypatch.setattr(fetch_next_work, "has_review_comments", lambda _number: True)
    monkeypatch.setattr(
        fetch_next_work,
        "fetch_feedback",
        lambda _number: [{"kind": "review", "id": 7}],
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


def test_batch_waiting_review_lane_does_not_block_free_lane_ready_work(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:agent-a"}],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [ready_issue(10, "priority:p0", body="touches: src/free.py")],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "_open_pr_work",
        lambda pr: {"type": "wait", "pr": pr["number"], "head": "head", "ci": "pending"},
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

    assert fetch_next_work.select_batch(["agent-a", "agent-b"]) == {
        "schema": "aru.fetch-next-work.batch/v1",
        "lanes": [
            {
                "agent": "agent-a",
                "work": {"type": "wait", "pr": 500, "head": "head", "ci": "pending"},
            },
            {
                "agent": "agent-b",
                "work": {"type": "issue", "issue": 10, "title": "issue 10"},
            },
        ],
        "diagnostics": [],
        "claim_status": "not-requested",
    }


def test_batch_ignores_unrelated_pr_without_author_label(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [{"number": 500, "labels": [{"name": "review:sourcery"}]}],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(10, body="touches: src/a.py"),
            ready_issue(11, body="touches: src/b.py"),
        ],
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert [lane["work"].get("issue") for lane in result["lanes"]] == [10, 11]


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


def test_batch_rejects_ambiguous_unrequested_pr_authors(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "labels": [
                    {"name": "author:agent-c"},
                    {"name": "author:agent-d"},
                ],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])

    with pytest.raises(common.KernelError, match="contradictory author labels"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"labels": [{"name": "author:agent-a"}]}, id="missing"),
        pytest.param({"number": None, "labels": [{"name": "author:agent-a"}]}, id="null"),
        pytest.param(
            {"number": "500", "labels": [{"name": "author:agent-a"}]},
            id="string",
        ),
        pytest.param(
            {"number": 1.5, "labels": [{"name": "author:agent-a"}]},
            id="float",
        ),
        pytest.param({"number": 0, "labels": [{"name": "author:agent-a"}]}, id="zero"),
        pytest.param(
            {"number": -1, "labels": [{"name": "author:agent-a"}]},
            id="negative",
        ),
        pytest.param(
            {"number": True, "labels": [{"name": "author:agent-a"}]},
            id="boolean",
        ),
    ],
)
def test_batch_rejects_malformed_open_pr_numbers(record):
    with pytest.raises(common.KernelError, match="Open PR number must be a positive integer"):
        fetch_next_work._prs_by_batch_author([record], ["agent-a", "agent-b"])


@pytest.mark.parametrize(
    "number",
    [
        pytest.param(None, id="missing"),
        pytest.param("10", id="string"),
        pytest.param(1.5, id="float"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="boolean"),
    ],
)
def test_batch_rejects_malformed_ready_issue_numbers(number):
    record = ready_issue(10)
    if number is None:
        del record["number"]
    else:
        record["number"] = number

    with pytest.raises(common.KernelError, match="Ready issue number must be a positive integer"):
        fetch_next_work._batch_ready_candidates([record])


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


def test_batch_ready_candidates_respect_active_lane_reserved_paths(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:agent-a"}],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(10, "priority:p0", body="touches: docs/**"),
            ready_issue(11, "priority:p0", body="touches: src/free.py"),
            ready_issue(12, "priority:p0", body="touches: src/other.py"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "_open_pr_work",
        lambda pr: {"type": "wait", "pr": pr["number"], "head": "head", "ci": "pending"},
    )
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: {
            "number": number,
            "title": f"issue {number}",
            "body": "touches: docs/guide.md",
            "labels": [{"name": "status:in-progress"}],
        },
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b", "agent-c"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {"type": "wait", "pr": 500, "head": "head", "ci": "pending"},
        },
        {"agent": "agent-b", "work": {"type": "issue", "issue": 11, "title": "issue 11"}},
        {"agent": "agent-c", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
    ]
