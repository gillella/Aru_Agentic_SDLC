from __future__ import annotations

import json
import sys

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


def ready_issue(number: int, *labels: str, body: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body if body is not None else f"touches: issue-{number}.txt",
        "labels": [{"name": "status:ready"}, *({"name": label} for label in labels)],
    }


def backlog_issue(number: int, *labels: str, body: str | None = None) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": body if body is not None else (
            "## Acceptance Criteria\n"
            "- [ ] Promote idle backlog work\n\n"
            f"touches: src/{number}.py"
        ),
        "state": "OPEN",
        "labels": [{"name": "status:backlog"}, *({"name": label} for label in labels)],
    }


def test_dependency_states_use_one_bounded_bulk_query(monkeypatch):
    calls = []
    records = [ready_issue(1, body="depends-on: #90\ndepends-on: #91")]
    valid = {"data": {"repository": {
        "issue_90": {"number": 90, "state": "OPEN"},
        "issue_91": {"number": 91, "state": "CLOSED"},
    }}}
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or valid,
    )

    assert fetch_next_work.dependency_states(records) == {90: "open", 91: "closed"}
    assert len(calls) == 1
    for response in ({"errors": ["partial"], **valid}, {"data": {"repository": {}}}):
        monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_a, **_k: response)
        with pytest.raises(common.KernelError, match="incomplete"):
            fetch_next_work.dependency_states(records)
    body = "\n".join(f"depends-on: #{number}" for number in range(1, 102))
    assert fetch_next_work.dependency_states([ready_issue(1, "needs-human", body=body)]) == {}
    assert fetch_next_work.dependency_states([ready_issue(1, body="depends-on: none")]) == {}
    with pytest.raises(common.KernelError, match="exceeds 100"):
        fetch_next_work.dependency_states([ready_issue(1, body=body)])


def test_dependency_states_accepts_exactly_100_references(monkeypatch):
    numbers = range(1, 101)
    body = "\n".join(f"depends-on: #{number}" for number in numbers)
    response = {"data": {"repository": {
        f"issue_{number}": {"number": number, "state": "CLOSED"}
        for number in numbers
    }}}
    calls = []
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or response,
    )

    assert fetch_next_work.dependency_states([ready_issue(101, body=body)]) == {
        number: "closed" for number in numbers
    }
    assert len(calls) == 1


def test_null_dependency_alias_leaves_missing_state_and_classifies_blocked(monkeypatch):
    record = ready_issue(1, body="depends-on: #90\ntouches: src/a.py")
    response = {"data": {"repository": {"issue_90": None}}}
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_args, **_kwargs: response)

    states = fetch_next_work.dependency_states([record])
    candidates, diagnostics, classification = fetch_next_work._ready_candidates(
        [record], states
    )

    assert 90 not in states
    assert candidates == []
    assert diagnostics == []
    assert classification == {
        "total_ready": 1,
        "executable_ready": 0,
        "human_gated": 0,
        "epics": 0,
        "dependency_blocked": 1,
        "malformed": 0,
    }


def test_backlog_dependency_states_use_one_bounded_bulk_query(monkeypatch):
    calls = []
    records = [backlog_issue(1, body=(
        "## Acceptance Criteria\n"
        "- [ ] Wait for dependencies\n\n"
        "depends-on: #90\n"
        "depends-on: #91\n"
        "touches: src/a.py"
    ))]
    valid = {"data": {"repository": {
        "issue_90": {"number": 90, "state": "OPEN"},
        "issue_91": {"number": 91, "state": "CLOSED"},
    }}}
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or valid,
    )

    assert fetch_next_work.backlog_dependency_states(records) == {90: "open", 91: "closed"}
    assert len(calls) == 1
    for response in ({"errors": ["partial"], **valid}, {"data": {"repository": {}}}):
        monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_a, **_k: response)
        with pytest.raises(common.KernelError, match="Backlog dependency inventory is incomplete"):
            fetch_next_work.backlog_dependency_states(records)


def test_select_idle_recovery_uses_shared_backlog_dependency_snapshot(monkeypatch):
    ready_snapshots = [[], [ready_issue(11, body="touches: src/a.py")]]
    backlog_records = [
        backlog_issue(
            11,
            "priority:p0",
            body=(
                "## Acceptance Criteria\n"
                "- [ ] Promote only closed dependencies\n\n"
                "depends-on: #90\n"
                "touches: src/a.py"
            ),
        )
    ]
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: ready_snapshots.pop(0))
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: backlog_records)
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_dependency_states",
        lambda records: calls.append(records) or {90: "closed"},
    )
    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", lambda *_a, **_k: None)

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result["type"] == "issue"
    assert len(calls) == 1


def test_select_idle_recovery_stops_on_partial_backlog_dependency_inventory(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: [backlog_issue(11)])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_dependency_states",
        lambda _records: (_ for _ in ()).throw(
            common.KernelError("Backlog dependency inventory is incomplete")
        ),
    )

    with pytest.raises(common.KernelError, match="Backlog dependency inventory is incomplete"):
        fetch_next_work.select("codex-sol56-issue535")


def test_select_idle_recovery_keeps_project_evidence_failures_terminal(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: [backlog_issue(11)])
    monkeypatch.setattr(
        fetch_next_work,
        "project_item_status",
        lambda _number: (_ for _ in ()).throw(
            common.KernelError("Project Board item inventory is truncated")
        ),
    )

    with pytest.raises(common.KernelError, match="Project Board item inventory is truncated"):
        fetch_next_work.select("codex-sol56-issue537")


def test_select_ready_fast_path_never_reads_or_mutates_backlog(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: calls.append("ready") or [ready_issue(7, "priority:p0")])
    monkeypatch.setattr(fetch_next_work, "backlog_issues", lambda: (_ for _ in ()).throw(AssertionError("Backlog inventory should not load")))
    monkeypatch.setattr(fetch_next_work, "project_item_status", lambda _n: (_ for _ in ()).throw(AssertionError("Project recovery evidence should not load")))
    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Backlog mutation should not run")))

    assert fetch_next_work.select("codex-sol56-issue535") == {
        "type": "issue",
        "issue": 7,
        "title": "issue 7",
    }
    assert calls == ["ready"]


def test_select_idle_recovery_prefilters_claimed_backlog_before_dependency_inventory(
    monkeypatch,
):
    claimed_body = (
        "## Acceptance Criteria\n"
        "- [ ] Already claimed elsewhere\n\n"
        + "\n".join(f"depends-on: #{number}" for number in range(1, 102))
        + "\n"
        + "touches: src/40.py"
    )
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(40, "agent:other-worker", body=claimed_body),
            backlog_issue(41, "priority:p0"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prefiltered claimed issue should not trigger dependency query")
        ),
    )
    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", lambda *_a, **_k: None)

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result["type"] == "issue"
    assert result["issue"] == 41
    assert result["title"] == "issue 41"
    assert result["diagnostics"][0] == "Ready idle; evaluated Backlog once"
    assert "Backlog issue #40 issue is already claimed; skipped" in result["diagnostics"]
    assert result["diagnostics"][-1] == "Promoted Backlog issue #41 to Ready"


def test_select_idle_recovery_runs_one_backlog_pass_without_ready_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("ready") or [],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: calls.append("backlog") or [backlog_issue(41, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: calls.append(("promote", number)),
    )

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result == {
        "type": "issue",
        "issue": 41,
        "title": "issue 41",
        "diagnostics": [
            "Ready idle; evaluated Backlog once",
            "Promoted Backlog issue #41 to Ready",
        ],
    }
    assert calls == ["ready", "backlog", ("promote", 41)]


def test_select_idle_recovery_rereads_chosen_issue_and_rejects_new_claim(monkeypatch):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(41, "priority:p0")],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: backlog_issue(41, "priority:p0", "agent:other-worker")
        if args == ["api", "repos/owner/repository/issues/41"]
        else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: (
            pre_mutation_check() if pre_mutation_check is not None else None,
            promoted.append(number),
        )[-1],
    )

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result == {
        "type": "idle",
        "diagnostics": [
            "Ready idle; evaluated Backlog once",
            "Backlog issue #41 issue is already claimed; skipped",
        ],
    }
    assert promoted == []


@pytest.mark.parametrize("payload", [None, [], {}, {"number": 41, "title": "", "body": "", "state": "OPEN"}])
def test_select_idle_recovery_treats_malformed_chosen_issue_reread_as_terminal(
    monkeypatch, payload
):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(41, "priority:p0")],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: payload
        if args == ["api", "repos/owner/repository/issues/41"]
        else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda _number, pre_mutation_check=None, **_kwargs: pre_mutation_check(),
    )

    with pytest.raises(common.KernelError, match="malformed recovery issue reread"):
        fetch_next_work.select("codex-sol56-issue535")


def test_select_idle_recovery_rereads_dependency_state_before_promotion(monkeypatch):
    dependency_states = iter([{90: "closed"}, {90: "open"}])
    promoted = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(
                41,
                "priority:p0",
                body=(
                    "## Acceptance Criteria\n"
                    "- [ ] Dependency can race\n\n"
                    "depends-on: #90\n"
                    "touches: src/41.py"
                ),
            )
        ],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_dependency_states",
        lambda _records: next(dependency_states),
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: backlog_issue(
            41, "priority:p0",
            body="## Acceptance Criteria\n- [ ] Dependency can race\n\ndepends-on: #90\ntouches: src/41.py",
        )
        if args == ["api", "repos/owner/repository/issues/41"]
        else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: (
            pre_mutation_check() if pre_mutation_check is not None else None,
            promoted.append(number),
        )[-1],
    )

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result == {
        "type": "idle",
        "diagnostics": [
            "Ready idle; evaluated Backlog once",
            "Backlog issue #41 open dependencies: #90; skipped",
        ],
    }
    assert promoted == []


@pytest.mark.parametrize("payload", [{}, [None], [{"number": "7"}], [{"number": 7}]])
def test_backlog_issues_rejects_malformed_inventory(monkeypatch, payload):
    monkeypatch.setattr(fetch_next_work, "gh_json", lambda *_args, **_kwargs: payload)

    with pytest.raises(common.KernelError, match="GitHub returned malformed Backlog issue inventory"):
        fetch_next_work.backlog_issues()


def test_select_idle_recovery_returns_promoted_issue_without_ready_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: calls.append("ready") or [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: calls.append("backlog") or [backlog_issue(41, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: calls.append(("promote", number)),
    )

    result = fetch_next_work.select("codex-sol56-issue535")

    assert result == {
        "type": "issue",
        "issue": 41,
        "title": "issue 41",
        "diagnostics": [
            "Ready idle; evaluated Backlog once",
            "Promoted Backlog issue #41 to Ready",
        ],
    }
    assert calls == ["ready", "backlog", ("promote", 41)]


def test_select_claim_after_idle_recovery_uses_same_invocation(monkeypatch, capsys):
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(41, "priority:p0")],
    )
    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", lambda *_a, **_k: None)
    monkeypatch.setattr(
        fetch_next_work,
        "claim",
        lambda number, agent: {"issue": number, "agent": agent, "status": "In Progress"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_next_work.py", "--agent", "codex-sol56-issue535", "--claim", "--json"],
    )

    assert fetch_next_work.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "agent": "codex-sol56-issue535",
        "work": {
            "type": "issue",
            "issue": 41,
            "title": "issue 41",
            "diagnostics": [
                "Ready idle; evaluated Backlog once",
                "Promoted Backlog issue #41 to Ready",
            ],
            "claim": {
                "issue": 41,
                "agent": "codex-sol56-issue535",
                "status": "In Progress",
            },
        },
    }


def test_batch_idle_recovery_uses_one_backlog_snapshot_and_recovers_up_to_free_capacity(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: calls.append("prs") or [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("ready") or [],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: calls.append("backlog")
        or [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: calls.append(("promote", number)),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b", "agent-c"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
        {"agent": "agent-b", "work": {"type": "issue", "issue": 13, "title": "issue 13"}},
        {"agent": "agent-c", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Ready snapshot empty; recovered 2 Backlog candidates",
        "Promoted Backlog issue #12 to Ready",
        "Promoted Backlog issue #13 to Ready",
    ]
    assert "ready_classification" not in result
    assert calls == ["prs", "ready", "backlog", ("promote", 12), ("promote", 13)]


def test_batch_idle_recovery_stops_at_free_capacity(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(12, "priority:p0"),
            backlog_issue(13, "priority:p0"),
            backlog_issue(14, "priority:p0"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: calls.append(number),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
        {"agent": "agent-b", "work": {"type": "issue", "issue": 13, "title": "issue 13"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Ready snapshot empty; recovered 2 Backlog candidates",
        "Promoted Backlog issue #12 to Ready",
        "Promoted Backlog issue #13 to Ready",
    ]
    assert "ready_classification" not in result
    assert calls == [12, 13]


def test_batch_idle_recovery_does_not_promote_overlapping_backlog_candidates(monkeypatch):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(12, "priority:p0", body="## Acceptance Criteria\n- [ ] First lane\n\ntouches: src/a.py"),
            backlog_issue(13, "priority:p0", body="## Acceptance Criteria\n- [ ] Conflicts with first\n\ntouches: src/a.py"),
            backlog_issue(14, "priority:p1", body="## Acceptance Criteria\n- [ ] Different path\n\ntouches: docs/b.md"),
        ],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b", "agent-c"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
        {"agent": "agent-b", "work": {"type": "issue", "issue": 14, "title": "issue 14"}},
        {"agent": "agent-c", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Backlog issue #13 touches conflict with earlier selected recovery candidate; skipped",
        "Ready snapshot empty; recovered 2 Backlog candidates",
        "Promoted Backlog issue #12 to Ready",
        "Promoted Backlog issue #14 to Ready",
    ]
    assert promoted == [12, 14]


def test_batch_idle_recovery_rejects_candidate_that_conflicts_with_open_lane_work(
    monkeypatch,
):
    promoted = []
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
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(
                12, "priority:p0",
                body="## Acceptance Criteria\n- [ ] Conflicts with active lane\n\ntouches: src/a.py",
            )
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
            "body": "touches: src/a.py",
            "labels": [{"name": "status:in-progress"}],
        },
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: promoted.append(number),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {
            "agent": "agent-a",
            "work": {"type": "wait", "pr": 500, "head": "head", "ci": "pending"},
        },
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Backlog issue #12 touches conflict with active lane work; skipped",
    ]
    assert promoted == []


def test_batch_idle_recovery_leaves_lane_idle_when_chosen_issue_drifted_before_promotion(
    monkeypatch,
):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p1")],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: (
            backlog_issue(12, "priority:p0", "agent:other-worker")
            if args == ["api", "repos/owner/repository/issues/12"]
            else backlog_issue(13, "priority:p1")
            if args == ["api", "repos/owner/repository/issues/13"]
            else (_ for _ in ()).throw(AssertionError(args))
        ),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda _number, pre_mutation_check=None, **_kwargs: pre_mutation_check(),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 13, "title": "issue 13"}},
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Backlog issue #12 issue is already claimed; skipped",
        "Ready snapshot empty; recovered 1 Backlog candidate",
        "Promoted Backlog issue #13 to Ready",
    ]
    assert "ready_classification" not in result


def test_batch_idle_recovery_keeps_terminal_reread_failures_terminal(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(12, "priority:p0")],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: None
        if args == ["api", "repos/owner/repository/issues/12"]
        else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda _number, pre_mutation_check=None, **_kwargs: pre_mutation_check(),
    )

    with pytest.raises(common.KernelError, match="malformed recovery issue reread"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_idle_recovery_keeps_terminal_promotion_failures_terminal(monkeypatch):
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(12, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda _number, **_kwargs: (_ for _ in ()).throw(common.KernelError("GitHub API rate limit exceeded")),
    )

    with pytest.raises(common.KernelError, match="GitHub API rate limit exceeded"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])


def test_batch_idle_recovery_stops_on_terminal_failure_after_partial_promotion(
    monkeypatch,
):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p0")],
    )

    def fake_promote(number, **_kwargs):
        promoted.append(number)
        if number == 13:
            raise common.KernelError("GitHub API rate limit exceeded")

    monkeypatch.setattr(fetch_next_work.triage_backlog, "promote_issue", fake_promote)

    with pytest.raises(common.KernelError, match="GitHub API rate limit exceeded"):
        fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert promoted == [12, 13]
