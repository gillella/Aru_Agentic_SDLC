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
