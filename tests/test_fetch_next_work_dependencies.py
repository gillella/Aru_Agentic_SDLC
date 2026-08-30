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


def test_select_ready_fast_path_never_reads_or_mutates_backlog(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "authored_prs", lambda _agent: [])
    monkeypatch.setattr(
        fetch_next_work,
        "ready_issues",
        lambda: calls.append("ready") or [ready_issue(7, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: (_ for _ in ()).throw(AssertionError("Backlog inventory should not load")),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Backlog mutation should not run")
        ),
    )

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


def test_batch_idle_recovery_uses_one_backlog_snapshot_and_single_safe_promotion(
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
        {"agent": "agent-b", "work": {"type": "idle"}},
        {"agent": "agent-c", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Promoted Backlog issue #12 to Ready",
    ]
    assert calls == ["prs", "ready", "backlog", ("promote", 12)]


def test_batch_idle_recovery_promotes_only_one_issue_to_avoid_partial_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [backlog_issue(12, "priority:p0"), backlog_issue(13, "priority:p0")],
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, **_kwargs: calls.append(number),
    )

    result = fetch_next_work.select_batch(["agent-a", "agent-b"])

    assert result["lanes"] == [
        {"agent": "agent-a", "work": {"type": "issue", "issue": 12, "title": "issue 12"}},
        {"agent": "agent-b", "work": {"type": "idle"}},
    ]
    assert result["diagnostics"] == [
        "Ready idle; evaluated Backlog once",
        "Promoted Backlog issue #12 to Ready",
    ]
    assert calls == [12]


def test_batch_idle_recovery_does_not_promote_overlapping_backlog_candidates(monkeypatch):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [
            backlog_issue(12, "priority:p0", body=(
                "## Acceptance Criteria\n"
                "- [ ] First lane\n\n"
                "touches: src/a.py"
            )),
            backlog_issue(13, "priority:p0", body=(
                "## Acceptance Criteria\n"
                "- [ ] Conflicts with first\n\n"
                "touches: src/a.py"
            )),
            backlog_issue(14, "priority:p1", body=(
                "## Acceptance Criteria\n"
                "- [ ] Different path\n\n"
                "touches: docs/b.md"
            )),
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
        {"agent": "agent-b", "work": {"type": "idle"}},
        {"agent": "agent-c", "work": {"type": "idle"}},
    ]
    assert promoted == [12]
