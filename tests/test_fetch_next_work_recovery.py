from __future__ import annotations

import json
import sys
import pytest

import common
import fetch_next_work
from test_fetch_next_work import ready_counts, ready_issue


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
        lambda: [
            {
                "number": 12,
                "title": "issue 12",
                "body": "## Acceptance Criteria\n- [ ] Snapshot\n\ntouches: src/a.py",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: (
            {
                "number": 12,
                "title": "issue 12",
                "body": "## Acceptance Criteria\n- [ ] Live\n\ntouches: docs/guide.md",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
            }
            if args == ["api", "repos/owner/repository/issues/12"]
            else (_ for _ in ()).throw(AssertionError(args))
        ),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: (
            promoted.append(number) or pre_mutation_check()
        ),
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


def test_batch_idle_recovery_rechecks_live_touches_against_selected_candidates(
    monkeypatch,
):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            {
                "number": 12,
                "title": "issue 12",
                "body": "## Acceptance Criteria\n- [ ] First\n\ntouches: src/a.py",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
            },
            {
                "number": 13,
                "title": "issue 13",
                "body": "## Acceptance Criteria\n- [ ] Second\n\ntouches: docs/b.md",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p1"}],
            },
        ],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: (
            {
                "number": 12,
                "title": "issue 12",
                "body": "## Acceptance Criteria\n- [ ] First\n\ntouches: src/a.py",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
            }
            if args == ["api", "repos/owner/repository/issues/12"]
            else {
                "number": 13,
                "title": "issue 13",
                "body": "## Acceptance Criteria\n- [ ] Drifted\n\ntouches: src/a.py",
                "state": "OPEN",
                "labels": [{"name": "status:backlog"}, {"name": "priority:p1"}],
            }
            if args == ["api", "repos/owner/repository/issues/13"]
            else (_ for _ in ()).throw(AssertionError(args))
        ),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: (
            promoted.append(number) or pre_mutation_check()
        ),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Backlog issue #13 touches conflict with earlier selected recovery candidate; skipped",
        "Ready snapshot empty; recovered 1 Backlog candidate",
        "Promoted Backlog issue #12 to Ready",
    ]
    assert promoted == [12, 13]


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


def test_batch_diagnostics_are_ordered_by_numeric_issue_number(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(40, body=""),
            ready_issue(7, "priority:urgent"),
            ready_issue(12, body="touches: ../secret"),
        ],
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["ready_classification"] == ready_counts(3, 0, malformed=3)
    assert result["diagnostics"] == [
        "Ready classification: total=3, executable=0, human-gated=0, "
        "epics=0, dependency-blocked=0, malformed=3",
        "Ready issue #7 has contradictory or unsupported priority labels; skipped",
        "Ready issue #12 has invalid touches: touches: contains an unsafe path; skipped",
        "Ready issue #40 has invalid touches: issue must contain exactly one "
        "touches: declaration; skipped",
    ]


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
    assert result["ready_classification"] == ready_counts(3, 1, malformed=2)
    assert result["diagnostics"] == [
        "Ready classification: total=3, executable=1, human-gated=0, "
        "epics=0, dependency-blocked=0, malformed=2",
        "Ready issue #1 has invalid touches: issue must contain exactly one "
        "touches: declaration; skipped",
        "Ready issue #2 has invalid touches: touches: contains an unsafe path; skipped",
    ]


def test_batch_reports_one_aggregate_for_seven_ready_with_zero_executable(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: calls.append("prs") or [])
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

    result = fetch_next_work.select_batch(["agent-a", "agent-b", "agent-c", "agent-d"])

    assert [lane["work"]["type"] for lane in result["lanes"]] == ["idle"] * 4
    assert result["ready_classification"] == ready_counts(7, 0, human=5, epics=2)
    assert result["diagnostics"] == [
        "Ready classification: total=7, executable=0, human-gated=5, "
        "epics=2, dependency-blocked=0, malformed=0"
    ]
    assert calls == ["prs", "issues"]


def test_batch_classification_is_deterministic_for_mixed_ready_cards(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(5, "priority:urgent"),
            ready_issue(4, body="depends-on: #90\ntouches: ../unsafe"),
            ready_issue(3, "type:epic", body="depends-on: #90"),
            ready_issue(2, "needs-human", "type:epic", body="depends-on: #90"),
            ready_issue(1, "priority:p0", body="depends-on: #91\ntouches: safe.py"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "dependency_states",
        lambda _records: {90: "open", 91: "closed"},
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {"type": "issue", "issue": 1, "title": "issue 1"},
        },
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    classification = result["ready_classification"]
    assert classification == ready_counts(5, 1, 1, 1, 1, 1)
    assert sum(classification.values()) == 2 * classification["total_ready"]


def test_batch_claim_stops_after_first_failure_and_prints_partial_json(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: calls.append("prs") or [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("issues") or [ready_issue(number) for number in (1, 2, 3, 4)],
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
def test_single_agent_cli_output_is_exactly_legacy_compatible(monkeypatch, capsys, as_json):
    work = {"type": "issue", "issue": 7, "title": "issue 7"}
    monkeypatch.setattr(fetch_next_work, "select", lambda agent: work)
    argv = ["fetch_next_work.py", "--agent", "agent-a"]
    if as_json:
        argv.append("--json")
    monkeypatch.setattr(sys, "argv", argv)

    assert fetch_next_work.main() == 0

    expected = (
        '{"agent": "agent-a", "work": {"issue": 7, "title": "issue 7", "type": "issue"}}\n'
        if as_json
        else "{'type': 'issue', 'issue': 7, 'title': 'issue 7'}\n"
    )
    assert capsys.readouterr().out == expected


def test_batch_reserves_touches_from_unrequested_open_pr_author(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:unrequested-agent"}],
            }
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: [
            ready_issue(10, "priority:p0", body="touches: src/conflict.py"),
            ready_issue(11, "priority:p0", body="touches: src/free.py"),
            ready_issue(12, "priority:p0", body="touches: src/other.py"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: {
            "number": number,
            "title": f"issue {number}",
            "body": "touches: src/conflict.py",
            "labels": [{"name": "status:in-progress"}],
        },
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 11, "title": "issue 11"}},
        {"agent": "agent-b", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
    ]


def test_batch_rejects_open_pr_without_linked_issue(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "No linked issue here",
                "labels": [{"name": "author:agent-c"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])

    with pytest.raises(common.KernelError, match="Open PR #500 has no linked issue"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_rejects_open_pr_with_multiple_linked_issues(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80\nCloses #81",
                "labels": [{"name": "author:agent-c"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])

    with pytest.raises(common.KernelError, match="Open PR #500 has multiple linked issues"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_rejects_open_pr_with_unreadable_linked_issue(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:agent-c"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: (_ for _ in ()).throw(common.KernelError(f"issue #{number} is unavailable")),
    )

    with pytest.raises(common.KernelError, match="issue #80 is unavailable"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_rejects_open_pr_with_malformed_issue_touches(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:agent-c"}],
            }
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "issue",
        lambda number: {
            "number": number,
            "title": f"issue {number}",
            "body": "no touches declaration here",
            "labels": [{"name": "status:in-progress"}],
        },
    )

    with pytest.raises(
        common.KernelError, match="issue must contain exactly one touches: declaration"
    ):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_rejects_multiple_open_prs_for_same_author(monkeypatch):
    monkeypatch.setattr(
        fetch_next_work,
        "open_prs",
        lambda: [
            {
                "number": 500,
                "body": "Closes #80",
                "labels": [{"name": "author:agent-c"}],
            },
            {
                "number": 501,
                "body": "Closes #81",
                "labels": [{"name": "author:agent-c"}],
            },
        ],
    )
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])

    with pytest.raises(common.KernelError, match="Open PRs for author 'agent-c' are ambiguous"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])
