from __future__ import annotations

import fetch_next_work


def test_batch_idle_recovery_rechecks_live_touches_against_reserved_paths(monkeypatch):
    promoted = []
    monkeypatch.setattr(fetch_next_work, "open_prs", lambda: [])
    monkeypatch.setattr(fetch_next_work, "ready_issues", lambda: [])
    monkeypatch.setattr(
        fetch_next_work,
        "backlog_issues",
        lambda: [{
            "number": 12,
            "title": "issue 12",
            "body": "## Acceptance Criteria\n- [ ] Snapshot\n\ntouches: src/a.py",
            "state": "OPEN",
            "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
        }],
    )
    monkeypatch.setattr(fetch_next_work, "repo_slug", lambda: "owner/repository")
    monkeypatch.setattr(
        fetch_next_work,
        "gh_json",
        lambda args, **_kwargs: {
            "number": 12,
            "title": "issue 12",
            "body": "## Acceptance Criteria\n- [ ] Live\n\ntouches: docs/guide.md",
            "state": "OPEN",
            "labels": [{"name": "status:backlog"}, {"name": "priority:p0"}],
        } if args == ["api", "repos/owner/repository/issues/12"] else (_ for _ in ()).throw(AssertionError(args)),
    )
    monkeypatch.setattr(
        fetch_next_work.triage_backlog,
        "promote_issue",
        lambda number, pre_mutation_check=None, **_kwargs: promoted.append(number)
        or pre_mutation_check(),
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
        lambda number, pre_mutation_check=None, **_kwargs: promoted.append(number)
        or pre_mutation_check(),
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
