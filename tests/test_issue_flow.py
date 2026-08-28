from __future__ import annotations

import sys

import pytest

import claim_issue
import triage_backlog


def backlog_issue(*labels: str) -> dict:
    return {
        "number": 3,
        "title": "ready",
        "body": "## Acceptance Criteria\n\n- [ ] Complete the fix.\n\ntouches: scripts/example.py",
        "state": "OPEN",
        "labels": [{"name": label} for label in labels],
    }


def test_triage_accepts_zero_or_one_supported_priority():
    assert triage_backlog.evaluate(backlog_issue()) == []
    assert triage_backlog.evaluate(backlog_issue("priority:p2")) == []


@pytest.mark.parametrize(
    ("labels", "diagnostic"),
    [
        (
            ("priority:p0", "priority:p1"),
            "issue may have at most one supported priority:p0..p3 label",
        ),
        (
            ("priority:urgent",),
            "issue may have at most one supported priority:p0..p3 label",
        ),
    ],
)
def test_triage_rejects_ambiguous_or_unsupported_priority(labels, diagnostic):
    assert triage_backlog.evaluate(backlog_issue(*labels)) == [diagnostic]


def test_triage_promotes_one_complete_issue(monkeypatch):
    records = [
        {"number": 2, "title": "blocked"},
        {"number": 3, "title": "ready"},
        {"number": 4, "title": "also ready"},
    ]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: records)
    monkeypatch.setattr(
        triage_backlog,
        "evaluate",
        lambda item: ["missing"] if item["number"] == 2 else [],
    )
    promoted = []
    monkeypatch.setattr(triage_backlog, "set_status", lambda number, status: promoted.append((number, status)))
    result = triage_backlog.triage()
    assert result["promoted"] == [3]
    assert promoted == [(3, "Ready")]
    assert result["rejected"] == {2: ["missing"]}


def test_claim_settles_one_writer(monkeypatch):
    snapshots = iter(
        [
            {"number": 7, "labels": [], "state": "OPEN"},
            {"number": 7, "labels": [{"name": "agent:codex-1"}], "state": "OPEN"},
            {"number": 7, "labels": [{"name": "agent:codex-1"}], "state": "OPEN"},
        ]
    )
    monkeypatch.setattr(claim_issue, "issue", lambda _number: next(snapshots))
    statuses_seen = iter(["Ready", "In Progress"])
    monkeypatch.setattr(claim_issue, "status_of", lambda _record: next(statuses_seen))
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(claim_issue, "run", lambda argv, **_kwargs: commands.append(argv))
    statuses = []
    monkeypatch.setattr(claim_issue, "set_status", lambda number, status: statuses.append((number, status)))

    result = claim_issue.claim(7, "codex-1")
    assert result["status"] == "In Progress"
    assert statuses == [(7, "In Progress")]
    assert "--add-label" in commands[0]


def test_claim_race_rolls_back(monkeypatch):
    snapshots = iter(
        [
            {"number": 7, "labels": [], "state": "OPEN"},
            {
                "number": 7,
                "labels": [{"name": "agent:codex-1"}, {"name": "agent:codex-2"}],
                "state": "OPEN",
            },
        ]
    )
    monkeypatch.setattr(claim_issue, "issue", lambda _number: next(snapshots))
    monkeypatch.setattr(claim_issue, "status_of", lambda _record: "Ready")
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(claim_issue, "run", lambda argv, **_kwargs: commands.append(argv))
    with pytest.raises(claim_issue.KernelError, match="race"):
        claim_issue.claim(7, "codex-1")
    assert "--remove-label" in commands[-1]


def test_claim_rollback_quota_surfaces_original_failure(monkeypatch, capsys):
    snapshots = iter(
        [
            {"number": 7, "labels": [], "state": "OPEN"},
            {
                "number": 7,
                "labels": [{"name": "agent:codex-1"}, {"name": "agent:codex-2"}],
                "state": "OPEN",
            },
        ]
    )
    monkeypatch.setattr(claim_issue, "issue", lambda _number: next(snapshots))
    monkeypatch.setattr(claim_issue, "status_of", lambda _record: "Ready")
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []

    def quota_on_rollback(argv, **_kwargs):
        commands.append(argv)
        if "--remove-label" in argv:
            raise claim_issue.KernelError(
                "GitHub GraphQL quota exhausted; stop and wait for the budget to reset"
            )

    monkeypatch.setattr(claim_issue, "run", quota_on_rollback)
    monkeypatch.setattr(
        sys,
        "argv",
        ["claim_issue.py", "--issue", "7", "--agent", "codex-1"],
    )

    with pytest.raises(SystemExit, match="2"):
        claim_issue.main()

    assert commands == [
        ["gh", "issue", "edit", "7", "--add-label", "agent:codex-1", "--add-assignee", "@me"],
        [
            "gh",
            "issue",
            "edit",
            "7",
            "--remove-label",
            "agent:codex-1",
            "--remove-assignee",
            "@me",
        ],
    ]
    assert capsys.readouterr().err.endswith(
        "claim_issue.py: error: GitHub GraphQL quota exhausted; stop and wait for the budget "
        "to reset; original claim failure: claim race detected; no exclusive winner\n"
    )


@pytest.mark.parametrize("agent", ["A", "contains space", "x", "../agent"])
def test_agent_ids_are_bounded(agent):
    with pytest.raises(claim_issue.KernelError):
        claim_issue.safe_agent(agent)
