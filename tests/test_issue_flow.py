from __future__ import annotations

import sys
from typing import Any

import pytest

import claim_issue
import triage_backlog


def backlog_issue(*labels: str, number: int = 3) -> dict[str, Any]:
    return {
        "number": number, "title": "ready", "state": "OPEN",
        "body": "## Acceptance Criteria\n\n- [ ] Complete the fix.\n\ntouches: scripts/example.py",
        "labels": [{"name": label} for label in labels],
    }


@pytest.mark.parametrize(
    ("labels", "diagnostics"),
    [
        ((), []),
        (("priority:p2",), []),
        (("priority:p0", "priority:p1"), ["issue may have at most one supported priority:p0..p3 label"]),
        (("priority:urgent",), ["issue may have at most one supported priority:p0..p3 label"]),
        (("needs-human",), ["needs-human issues cannot enter Ready"]),
        (("type:epic",), ["type:epic issues cannot enter Ready"]),
    ],
)
def test_triage_evaluates_issue_labels(labels, diagnostics):
    assert triage_backlog.evaluate(backlog_issue(*labels)) == diagnostics


def test_triage_does_not_promote_human_gated_or_epic_issues(monkeypatch):
    records = [{**backlog_issue("type:epic"), "number": 4}, {**backlog_issue("needs-human"), "number": 3}]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: records)
    monkeypatch.setattr(
        triage_backlog,
        "set_status",
        lambda *_: (_ for _ in ()).throw(AssertionError("non-executable work must not enter Ready")),
    )
    assert triage_backlog.triage(promote_all=True) == {
        "promoted": [],
        "rejected": {3: ["needs-human issues cannot enter Ready"], 4: ["type:epic issues cannot enter Ready"]},
    }


def test_triage_promotes_one_complete_issue(monkeypatch):
    records = [{"number": 2, "title": "blocked"}, {"number": 3, "title": "ready"}, {"number": 4, "title": "also ready"}]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: records)
    monkeypatch.setattr(triage_backlog, "evaluate", lambda item: ["missing"] if item["number"] == 2 else [])
    promoted = []

    def transactional_set_status(
        number,
        status,
        *,
        expected_current=None,
        pre_mutation_check=None,
    ):
        assert expected_current == "Backlog"
        assert pre_mutation_check is None
        promoted.append((number, status, expected_current))

    monkeypatch.setattr(triage_backlog, "set_status", transactional_set_status)
    result = triage_backlog.triage()
    assert result["promoted"] == [3]
    assert promoted == [(3, "Ready", "Backlog")]
    assert result["rejected"] == {2: ["missing"]}


def _mock_claim_context(monkeypatch, snapshots, status="Ready"):
    snap_iter = iter(snapshots) if isinstance(snapshots, (list, tuple)) else snapshots
    status_iter = iter(status) if isinstance(status, (list, tuple)) else None
    monkeypatch.setattr(claim_issue, "issue", lambda _num: next(snap_iter))
    monkeypatch.setattr(claim_issue, "status_of", lambda _rec: next(status_iter) if status_iter else status)
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _rec: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _rec: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_a, **_kw: None)
    monkeypatch.setattr(claim_issue, "other_active_claims", lambda *_args: [])


@pytest.mark.parametrize("race", [False, True])
def test_claim_settlement_and_race_rollback(monkeypatch, race):
    owners = ["agent:codex-1"] + (["agent:codex-2"] if race else [])
    owned = backlog_issue(*owners, number=7)
    snapshots = [backlog_issue(number=7), owned, owned, backlog_issue("agent:codex-2" if race else "agent:codex-1", number=7)]
    statuses_live = ["Ready", "Ready"] if race else ["Ready", "Ready", "In Progress"]
    _mock_claim_context(monkeypatch, snapshots, status=statuses_live)
    commands, statuses = [], []
    monkeypatch.setattr(claim_issue, "run", lambda argv, **_kwargs: commands.append(argv))

    def transition(number, status, **kwargs):
        if kwargs.get("pre_mutation_check"):
            kwargs["pre_mutation_check"]()
        statuses.append((number, status, kwargs.get("expected_current")))

    monkeypatch.setattr(claim_issue, "set_status", transition)

    if race:
        with pytest.raises(claim_issue.KernelError, match="race"):
            claim_issue.claim(7, "codex-1")
        assert "--remove-label" in commands[-1]
    else:
        result = claim_issue.claim(7, "codex-1")
        assert result["status"] == "In Progress"
        assert statuses == [(7, "In Progress", "Ready")]
        assert "--add-label" in commands[0]


@pytest.mark.parametrize("mutation_failure", [False, True])
def test_claim_rollback_quota_surfaces_original_failure(monkeypatch, capsys, mutation_failure):
    snapshots = [
        {"number": 7, "labels": [], "state": "OPEN"},
        {"number": 7, "labels": [{"name": "agent:codex-1"}, {"name": "agent:codex-2"}], "state": "OPEN"},
        {"number": 7, "labels": [{"name": "agent:codex-1"}, {"name": "agent:codex-2"}], "state": "OPEN"},
    ]
    _mock_claim_context(monkeypatch, snapshots, status="Ready")
    commands = []

    def quota_on_rollback(argv, **_kwargs):
        commands.append(argv)
        if mutation_failure and "--add-label" in argv:
            raise claim_issue.KernelError("App edit failed")
        if "--remove-label" in argv:
            raise claim_issue.KernelError("GitHub GraphQL quota exhausted; stop and wait for the budget to reset")

    monkeypatch.setattr(claim_issue, "run", quota_on_rollback)
    monkeypatch.setattr(sys, "argv", ["claim_issue.py", "--issue", "7", "--agent", "codex-1"])
    with pytest.raises(SystemExit, match="2"):
        claim_issue.main()
    assert commands == [
        ["gh", "issue", "edit", "7", "--add-label", "agent:codex-1"],
        ["gh", "issue", "edit", "7", "--remove-label", "agent:codex-1"],
    ]
    error = capsys.readouterr().err
    assert "GitHub GraphQL quota exhausted; stop and wait for the budget to reset" in error
    assert "original claim failure: " + ("App edit failed" if mutation_failure else "claim race detected; no exclusive winner") in error


@pytest.mark.parametrize("agent", ["A", "contains space", "x", "../agent"])
def test_agent_ids_are_bounded(agent):
    with pytest.raises(claim_issue.KernelError):
        claim_issue.safe_agent(agent)


@pytest.mark.parametrize("partial", [False, True])
def test_app_claim_failure_retries_and_release_preserves_human_assignees(monkeypatch, partial):
    record = backlog_issue("status:ready")
    record["assignees"] = [{"login": "human-owner"}]
    failure = [True]
    monkeypatch.setattr(claim_issue, "issue", lambda _number: record)
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claim_issue, "other_active_claims", lambda *_args: [])

    def command(argv):
        assert "--add-assignee" not in argv and "--remove-assignee" not in argv
        adding = "--add-label" in argv
        fail = adding and bool(failure) and failure.pop()
        label = {"name": "agent:codex-1"}
        if not fail or partial:
            if adding:
                record["labels"].append(label)
            else:
                record["labels"].remove(label)
        if fail:
            raise claim_issue.KernelError("App edit failed")

    def transition(_number, status, *, expected_current, pre_mutation_check=None):
        assert claim_issue.status_of(record) == expected_current
        if pre_mutation_check:
            pre_mutation_check()
        record["labels"] = [label for label in record["labels"] if not label["name"].startswith("status:")]
        record["labels"].append({"name": "status:" + status.lower().replace(" ", "-")})

    monkeypatch.setattr(claim_issue, "run", command)
    monkeypatch.setattr(claim_issue, "set_status", transition)
    with pytest.raises(claim_issue.KernelError, match="App edit failed"):
        claim_issue.claim(3, "codex-1")
    assert claim_issue.status_of(record) == "Ready" and claim_issue.claimants(record) == []
    assert claim_issue.claim(3, "codex-1")["status"] == "In Progress"
    linked = iter([[], [], [44]])
    monkeypatch.setattr(claim_issue, "linked_open_prs", lambda _number: next(linked))
    with pytest.raises(claim_issue.KernelError, match="release raced"):
        claim_issue.release(3, "codex-1")
    assert claim_issue.status_of(record) == "In Progress"
    assert claim_issue.claimants(record) == ["agent:codex-1"]
    monkeypatch.setattr(claim_issue, "linked_open_prs", lambda _number: [])
    assert claim_issue.release(3, "codex-1")["status"] == "Ready"
    assert claim_issue.claimants(record) == [] and record["assignees"] == [{"login": "human-owner"}]


def test_release_requires_in_progress_and_no_linked_open_pr(monkeypatch):
    record = backlog_issue("agent:codex-1", "status:in-progress", number=7)
    monkeypatch.setattr(claim_issue, "issue", lambda _number: record)
    monkeypatch.setattr(claim_issue, "linked_open_prs", lambda _number: [44])
    monkeypatch.setattr(
        claim_issue,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked release must not mutate")
        ),
    )

    with pytest.raises(claim_issue.KernelError, match=r"linked open pull requests: \[44\]"):
        claim_issue.release(7, "codex-1")


def test_closed_stale_active_claim_blocks_a_second_claim(monkeypatch):
    monkeypatch.setattr(claim_issue, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        claim_issue,
        "gh_paginated",
        lambda _endpoint: [
            {**backlog_issue("agent:codex-1", "status:in-progress"), "state": "closed"},
            {**backlog_issue("agent:codex-1", "status:done"), "number": 4, "state": "closed"},
        ],
    )
    assert claim_issue.other_active_claims(7, "codex-1") == [3]


def test_release_rolls_back_status_when_claim_removal_fails(monkeypatch):
    record = backlog_issue("agent:codex-1", "status:in-progress", number=7)
    statuses = []
    monkeypatch.setattr(claim_issue, "issue", lambda _number: record)
    monkeypatch.setattr(claim_issue, "linked_open_prs", lambda _number: [])
    monkeypatch.setattr(
        claim_issue,
        "set_status",
        lambda number, status, *, expected_current, **_kwargs: statuses.append(
            (number, status, expected_current)
        ),
    )
    monkeypatch.setattr(
        claim_issue,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            claim_issue.KernelError("claim removal failed")
        ),
    )

    with pytest.raises(claim_issue.KernelError, match="claim removal failed"):
        claim_issue.release(7, "codex-1")
    assert statuses == [
        (7, "Ready", "In Progress"),
        (7, "In Progress", "Ready"),
    ]


def test_linked_open_pr_inventory_is_complete_and_bounded(monkeypatch):
    monkeypatch.setattr(claim_issue, "repo_slug", lambda: "owner/repo")
    connection = {
        "nodes": [{"number": 5, "state": "CLOSED"}, {"number": 7, "state": "OPEN"}],
        "pageInfo": {"hasNextPage": False},
    }
    monkeypatch.setattr(
        claim_issue,
        "gh_json",
        lambda *_args, **_kwargs: {
            "data": {"repository": {"issue": {"closedByPullRequestsReferences": connection}}}
        },
    )

    assert claim_issue.linked_open_prs(3) == [7]
