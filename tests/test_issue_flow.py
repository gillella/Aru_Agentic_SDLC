from __future__ import annotations

import sys
from typing import Any

import pytest

import claim_issue
import common
import triage_backlog
import update_issue_status as uis


def _ok_result():
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def backlog_issue(*labels: str) -> dict[str, Any]:
    return {
        "number": 3,
        "title": "ready",
        "body": "## Acceptance Criteria\n\n- [ ] Complete the fix.\n\ntouches: scripts/example.py",
        "state": "OPEN",
        "labels": [{"name": label} for label in labels],
    }


def epic_body(
    *,
    children: list[int] | None = None,
    policy: str | None = "children-only",
    depends_on: list[int] | None = None,
) -> str:
    lines = ["## Child Issues"]
    for number in children or [91, 92]:
        lines.append(f"- #{number}")
    if policy is not None:
        lines.extend(["", f"epic-close-policy: {policy}"])
    if depends_on:
        lines.extend(f"depends-on: #{number}" for number in depends_on)
    return "\n".join(lines) + "\n"


def epic_record(
    *,
    number: int = 100,
    children: list[int] | None = None,
    policy: str | None = "children-only",
    depends_on: list[int] | None = None,
    labels: list[str] | None = None,
    state: str = "OPEN",
) -> dict[str, Any]:
    return {
        "number": number,
        "body": epic_body(children=children, policy=policy, depends_on=depends_on),
        "state": state,
        "labels": [{"name": name} for name in (labels if labels is not None else ["type:epic", "status:backlog"])],
    }


def child_snapshot(
    number: int,
    *,
    state: str = "CLOSED",
    status: str = "done",
    repository: str = "owner/repo",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "repository": repository,
        "labels": [{"name": f"status:{status}"}],
    }


_UNSET = object()


def mock_epic_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    epic: dict[str, Any] | None = None,
    issue_fn: Any = None,
    snapshots: dict[int, Any] | None = None,
    status_fn: Any = None,
    status: Any = _UNSET,
    project_status_fn: Any = None,
    project_status: Any = _UNSET,
    depends: list[int] | None = None,
) -> None:
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: depends or [])
    monkeypatch.setattr(common, "unresolved_dependencies", lambda record, cwd=None: depends or [])
    if snapshots is not None:
        monkeypatch.setattr(uis, "child_issue_snapshots", lambda numbers, cwd=None: snapshots)
    if issue_fn is not None:
        monkeypatch.setattr(uis, "issue", issue_fn)
        monkeypatch.setattr(common, "issue", issue_fn)
    elif epic is not None:
        monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
        monkeypatch.setattr(common, "issue", lambda number, cwd=None: epic)
    if status_fn is not None:
        monkeypatch.setattr(uis, "status_of", status_fn)
        monkeypatch.setattr(common, "status_of", status_fn)
    elif status is not _UNSET:
        monkeypatch.setattr(uis, "status_of", lambda record: status)
        monkeypatch.setattr(common, "status_of", lambda record: status)
    if project_status_fn is not None:
        monkeypatch.setattr(uis, "project_item_status", project_status_fn)
        monkeypatch.setattr(common, "project_item_status", project_status_fn)
    elif project_status is not _UNSET:
        monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: project_status)
        monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: project_status)


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
    monkeypatch.setattr(triage_backlog, "set_status", lambda number, status: promoted.append((number, status)))
    result = triage_backlog.triage()
    assert result["promoted"] == [3]
    assert promoted == [(3, "Ready")]
    assert result["rejected"] == {2: ["missing"]}


@pytest.mark.parametrize("race", [False, True])
def test_claim_settlement_and_race_rollback(monkeypatch, race):
    snapshots = iter([
        {"number": 7, "labels": [], "state": "OPEN"},
        {"number": 7, "labels": [{"name": "agent:codex-1"}] + ([{"name": "agent:codex-2"}] if race else []), "state": "OPEN"},
        {"number": 7, "labels": [{"name": "agent:codex-1"}], "state": "OPEN"},
    ])
    monkeypatch.setattr(claim_issue, "issue", lambda _number: next(snapshots))
    statuses_seen = iter(["Ready", "In Progress"])
    monkeypatch.setattr(claim_issue, "status_of", lambda _record: next(statuses_seen))
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    commands, statuses = [], []
    monkeypatch.setattr(claim_issue, "run", lambda argv, **_kwargs: commands.append(argv))
    monkeypatch.setattr(claim_issue, "set_status", lambda number, status: statuses.append((number, status)))

    if race:
        with pytest.raises(claim_issue.KernelError, match="race"):
            claim_issue.claim(7, "codex-1")
        assert "--remove-label" in commands[-1]
    else:
        result = claim_issue.claim(7, "codex-1")
        assert result["status"] == "In Progress"
        assert statuses == [(7, "In Progress")]
        assert "--add-label" in commands[0]


def test_claim_rollback_quota_surfaces_original_failure(monkeypatch, capsys):
    snapshots = iter([
        {"number": 7, "labels": [], "state": "OPEN"},
        {"number": 7, "labels": [{"name": "agent:codex-1"}, {"name": "agent:codex-2"}], "state": "OPEN"},
    ])
    monkeypatch.setattr(claim_issue, "issue", lambda _number: next(snapshots))
    monkeypatch.setattr(claim_issue, "status_of", lambda _record: "Ready")
    monkeypatch.setattr(claim_issue, "contract_errors", lambda _record: [])
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []

    def quota_on_rollback(argv, **_kwargs):
        commands.append(argv)
        if "--remove-label" in argv:
            raise claim_issue.KernelError("GitHub GraphQL quota exhausted; stop and wait for the budget to reset")

    monkeypatch.setattr(claim_issue, "run", quota_on_rollback)
    monkeypatch.setattr(sys, "argv", ["claim_issue.py", "--issue", "7", "--agent", "codex-1"])

    with pytest.raises(SystemExit, match="2"):
        claim_issue.main()

    assert commands == [
        ["gh", "issue", "edit", "7", "--add-label", "agent:codex-1", "--add-assignee", "@me"],
        ["gh", "issue", "edit", "7", "--remove-label", "agent:codex-1", "--remove-assignee", "@me"],
    ]
    assert capsys.readouterr().err.endswith(
        "claim_issue.py: error: GitHub GraphQL quota exhausted; stop and wait for the budget "
        "to reset; original claim failure: claim race detected; no exclusive winner\n"
    )


@pytest.mark.parametrize("agent", ["A", "contains space", "x", "../agent"])
def test_agent_ids_are_bounded(agent):
    with pytest.raises(claim_issue.KernelError):
        claim_issue.safe_agent(agent)


def test_epic_reconcile_success(monkeypatch):
    epic = epic_record()
    settled = {**epic, "state": "CLOSED", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    reads = iter([epic, settled])
    calls = {"close": 0, "set_status": 0}
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(reads),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
        project_status_fn=lambda number, cwd=None: next(iter(["Backlog", "Done"])),
    )
    project_statuses = iter(["Backlog", "Done"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_statuses))
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: next(project_statuses))
    monkeypatch.setattr(uis, "set_status", lambda number, status, cwd=None: calls.__setitem__("set_status", calls["set_status"] + 1))
    monkeypatch.setattr(common, "set_status", lambda number, status, cwd=None: calls.__setitem__("set_status", calls["set_status"] + 1))

    def fake_run(argv, **kwargs):
        if argv[:3] == ["gh", "issue", "close"]:
            calls["close"] += 1
        return _ok_result()

    monkeypatch.setattr(uis, "run", fake_run)
    monkeypatch.setattr(common, "run", fake_run)
    result = uis.apply_epic_reconciliation(100)
    assert result["applied"] is True
    assert result["after"] == "Done"
    assert result["state"] == "CLOSED"
    assert result["project_status"] == "Done"
    assert calls == {"close": 1, "set_status": 1}


@pytest.mark.parametrize(
    ("issue_status", "project_status", "expected_blockers"),
    [
        ("Backlog", "Ready", ["not Backlog (Ready)", "disagree"]),
        ("In Review", "Backlog", ["not Backlog (In Review)", "disagree"]),
        (None, None, ["epic status label is missing", "linked Project card Status is unset"]),
        ("Ready", "Ready", ["not Backlog (Ready)"]),
        ("In Review", "In Review", ["not Backlog (In Review)"]),
    ],
)
def test_epic_reconcile_adversarial_prestate_blocks_with_zero_mutations(
    monkeypatch, issue_status, project_status, expected_blockers
):
    labels = ["type:epic"]
    if issue_status:
        labels.append(f"status:{issue_status.lower().replace(' ', '-')}")
    mock_epic_context(
        monkeypatch,
        epic=epic_record(labels=labels),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status=issue_status,
        project_status=project_status,
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no mutate")))
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no mutate")))

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert evidence["status"] == issue_status
    assert evidence["project_status"] == project_status
    for blocker in expected_blockers:
        assert any(blocker in item for item in evidence["blockers"])
    with pytest.raises(uis.KernelError):
        uis.apply_epic_reconciliation(100)


def test_epic_reconcile_malformed_project_evidence_blocks_with_zero_mutations(monkeypatch):
    mock_epic_context(
        monkeypatch,
        epic=epic_record(),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status_fn=lambda number, cwd=None: (_ for _ in ()).throw(
            uis.KernelError("Project Board card snapshot returned a GraphQL error")
        ),
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no mutate")))
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no mutate")))

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert evidence["project_status"] is None
    assert any("GraphQL error" in item for item in evidence["blockers"])
    with pytest.raises(uis.KernelError, match="GraphQL error"):
        uis.apply_epic_reconciliation(100)


@pytest.mark.parametrize(
    ("labels", "policy", "children", "snapshots", "depends", "blocker"),
    [
        ([], "children-only", None, None, [], "issue is not type:epic"),
        (["type:epic", "needs-human", "status:backlog"], "children-only", None, None, [], "needs-human"),
        (["type:epic", "status:backlog"], "manual", None, None, [], "epic-close-policy: manual"),
        (["type:epic", "status:backlog"], None, None, None, [], "epic-close-policy is missing"),
        (["type:epic", "status:backlog"], "children-only", [91], {}, [], "child #91 is missing"),
        (["type:epic", "status:backlog"], "children-only", [91], {91: child_snapshot(91, state="OPEN")}, [], "child #91 is not closed"),
        (["type:epic", "status:backlog"], "children-only", [91], {91: child_snapshot(91, status="ready")}, [], "child #91 is not status:done"),
        (["type:epic", "status:backlog"], "children-only", [91], {91: child_snapshot(91, repository="other/repo")}, [], "child #91 is not in this repository"),
        (["type:epic", "status:backlog"], "children-only", None, None, [90], "open depends-on: #90"),
    ],
)
def test_epic_reconcile_blocked_cases(monkeypatch, labels, policy, children, snapshots, depends, blocker):
    mock_epic_context(
        monkeypatch,
        epic=epic_record(labels=labels, policy=policy, children=children),
        snapshots=snapshots or {},
        status_fn=lambda record: "Backlog" if "status:backlog" in [lbl["name"] for lbl in record.get("labels", [])] else None,
        project_status="Backlog",
        depends=depends,
    )
    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert any(blocker in item for item in evidence["blockers"])


def test_epic_reconcile_check_reports_closable_without_mutation(monkeypatch):
    epic = epic_record()
    mutations = []
    mock_epic_context(
        monkeypatch,
        epic=epic,
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record is epic else "Done",
        project_status="Backlog",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: mutations.append(("set_status", args)))
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: mutations.append(("run", args)))

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["closable"] is True
    assert evidence["blocked"] is False
    assert evidence["status"] == "Backlog"
    assert evidence["project_status"] == "Backlog"
    assert evidence["children"] == {
        "91": {"number": 91, "state": "CLOSED", "status": "Done", "repository": "owner/repo"},
        "92": {"number": 92, "state": "CLOSED", "status": "Done", "repository": "owner/repo"},
    }
    assert mutations == []


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("## Child Issues\n- not-a-ref\n", "malformed"),
        ("## Child Issues\n- other/repo#9\n", "malformed"),
        ("## Child Issues\n- #1\n- #1\n", "duplicate"),
        ("## Child Issues\n- #1\nprose\n- #2\n", "unexpected content"),
        ("## Child Issues\n- #1\n\narbitrary trailing prose\n", "unexpected trailer content"),
        ("## Child Issues\n- #1\n\n- #2\n", "interrupted child list"),
        ("## Child Issues\n- #1\n\n## Child Issues\n- #2\n", "more than one ## Child Issues section"),
    ],
)
def test_parse_child_issues_rejects_malformed_sections(body, match):
    with pytest.raises(uis.KernelError, match=match):
        uis.parse_child_issues(body)


def test_parse_child_issues_ordering_and_trailers():
    assert uis.parse_child_issues("## Child Issues\n- #9\n- #2\n") == [2, 9]
    assert uis.parse_child_issues("## Child Issues\n- #91\n\nepic-close-policy: children-only\ndepends-on: #5\n") == [91]


def test_epic_reconcile_ambiguous_policy_blocks(monkeypatch):
    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\nepic-close-policy: manual\n"
    mock_epic_context(
        monkeypatch,
        epic={"number": 100, "body": body, "state": "OPEN", "labels": [{"name": "type:epic"}, {"name": "status:backlog"}]},
        status="Backlog",
        project_status="Backlog",
    )
    assert "epic-close-policy is ambiguous" in uis.epic_reconcile_evidence(100)["blockers"]


def test_epic_reconcile_blocks_unless_state_is_exactly_open(monkeypatch):
    mock_epic_context(monkeypatch, epic=epic_record(policy=None, state="CLOSED"))
    assert "epic is not open" in uis.epic_reconcile_evidence(100)["blockers"]


def test_epic_reconcile_contradictory_epic_status_blocks_without_crashing(monkeypatch):
    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\n"
    epic = {
        "number": 100,
        "body": body,
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:in-review"}, {"name": "status:done"}],
    }
    mock_epic_context(
        monkeypatch,
        epic=epic,
        snapshots={91: child_snapshot(91)},
        project_status="Backlog",
    )
    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert evidence["status"] is None
    assert any("contradictory status labels" in item for item in evidence["blockers"])


def test_epic_reconcile_mutation_failure_rolls_back(monkeypatch):
    commands = []
    mock_epic_context(
        monkeypatch,
        epic=epic_record(),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
        project_status="Backlog",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)

    def fail_close(argv, **kwargs):
        commands.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return _ok_result()

    monkeypatch.setattr(uis, "run", fail_close)
    monkeypatch.setattr(uis, "_rollback_epic_reconciliation", lambda *args, **kwargs: commands.append(["rollback"]))

    with pytest.raises(uis.KernelError, match="close failed"):
        uis.apply_epic_reconciliation(100)

    assert ["gh", "issue", "close", "100", "--reason", "completed"] in commands
    assert ["rollback"] in commands


def test_epic_close_policy_requires_one_declaration():
    assert uis.epic_close_policy("## Child Issues\n- #1\n\nepic-close-policy: children-only\n") == "children-only"
    with pytest.raises(uis.KernelError, match="missing"):
        uis.epic_close_policy("## Child Issues\n- #1\n")
    with pytest.raises(uis.KernelError, match="ambiguous"):
        uis.epic_close_policy("epic-close-policy: children-only\nepic-close-policy: manual\n")


def test_apply_epic_reconciliation_read_back_fails_closed(monkeypatch):
    epic = epic_record()
    final = {**epic, "state": "OPEN", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    readbacks = iter([epic, final])
    rollback = []
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(readbacks),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: None)
    project_statuses = iter(["Backlog", "Done"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_statuses))
    monkeypatch.setattr(uis, "_rollback_epic_reconciliation", lambda *args, **kwargs: rollback.append(kwargs))

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert len(rollback) == 1


def test_epic_rollback_reopens_and_reverts_status_when_close_settled_but_card_did_not(monkeypatch):
    """Exercise the real rollback function end-to-end, restoring exact Backlog."""
    epic = epic_record()
    final = {**epic, "state": "CLOSED", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    readbacks = iter([epic, final, final, epic])
    run_calls = []
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(readbacks),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", [])) else "Backlog",
        project_status="Backlog",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "run", lambda argv, **kwargs: run_calls.append(argv) or _ok_result())
    monkeypatch.setattr(common, "run", lambda argv, **kwargs: run_calls.append(argv) or _ok_result())

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert ["gh", "issue", "reopen", "100"] in run_calls
    assert ["gh", "issue", "edit", "100", "--add-label", "status:backlog", "--remove-label", "status:done"] in run_calls


@pytest.mark.parametrize(
    ("remote_state_after_raise", "expect_reopen"),
    [("OPEN", False), ("CLOSED", True)],
)
def test_epic_rollback_reopen_decision_is_authoritative_not_a_local_flag(
    monkeypatch, remote_state_after_raise, expect_reopen
):
    """Close can raise locally after landing remotely; rollback must reopen
    based on a fresh authoritative read, never a local success flag."""
    epic = epic_record()
    final = {**epic, "state": remote_state_after_raise, "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    reads = iter([epic, final, final, epic])
    run_calls = []
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(reads),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", [])) else "Backlog",
        project_status="Backlog",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "ensure_label", lambda *args, **kwargs: None)

    def fail_close(argv, **kwargs):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return _ok_result()

    monkeypatch.setattr(uis, "run", fail_close)
    monkeypatch.setattr(common, "run", fail_close)
    with pytest.raises(uis.KernelError, match="close failed"):
        uis.apply_epic_reconciliation(100)

    assert (["gh", "issue", "reopen", "100"] in run_calls) is expect_reopen
    assert ["gh", "issue", "edit", "100", "--add-label", "status:backlog", "--remove-label", "status:done"] in run_calls


def test_rollback_epic_reconciliation_verifies_settled_state(monkeypatch):
    run_calls = []
    monkeypatch.setattr(uis, "run", lambda argv, **kwargs: run_calls.append(argv) or _ok_result())
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: {"number": 100, "state": "OPEN", "labels": [{"name": "status:backlog"}]})
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog")
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    uis._rollback_epic_reconciliation(100, original=uis.KernelError("original failure"))

    assert not any(argv[:3] == ["gh", "issue", "reopen"] for argv in run_calls)
    assert not any(argv[:3] == ["gh", "issue", "edit"] for argv in run_calls)
    assert not any(argv[:3] == ["gh", "project", "item-edit"] for argv in run_calls)


@pytest.mark.parametrize(
    ("fail_mode", "expected_match"),
    [
        ("unsettled", "did not settle"),
        ("command_error", "reopen failed"),
    ],
)
def test_rollback_epic_reconciliation_surfaces_combined_error(monkeypatch, fail_mode, expected_match):
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "board_edit", lambda number, status, cwd=None: ["project", "item-edit", "--id", "1"])

    if fail_mode == "unsettled":
        monkeypatch.setattr(uis, "run", lambda argv, **kwargs: _ok_result())
        monkeypatch.setattr(uis, "issue", lambda number, cwd=None: {"number": 100, "state": "CLOSED", "labels": [{"name": "status:done"}]})
        monkeypatch.setattr(uis, "status_of", lambda record: "Done")
        monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Done")
    else:
        monkeypatch.setattr(uis, "issue", lambda number, cwd=None: {"number": 100, "state": "CLOSED"})
        monkeypatch.setattr(uis, "status_of", lambda record: "Backlog")
        monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

        def fail_reopen(argv, **kwargs):
            if argv[:3] == ["gh", "issue", "reopen"]:
                raise uis.KernelError("reopen failed")
            return _ok_result()

        monkeypatch.setattr(uis, "run", fail_reopen)

    with pytest.raises(uis.KernelError, match=expected_match) as excinfo:
        uis._rollback_epic_reconciliation(100, original=uis.KernelError("original failure"))
    assert "original failure" in str(excinfo.value)


def test_epic_rollback_detects_project_done_after_ambiguous_set_status_item_edit_failure(monkeypatch):
    """Inner set_status restores issue label on item-edit failure, but Project card
    remains Done if the mutation landed remotely before failing; outer rollback
    must detect Project Done from authoritative read and force board back to Backlog."""
    state = {"issue_state": "OPEN", "issue_labels": ["type:epic", "status:backlog"], "project_status": "Backlog"}
    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "edit"]:
            labels = list(state["issue_labels"])
            if "--remove-label" in argv:
                labels = [label_name for label_name in labels if label_name != argv[argv.index("--remove-label") + 1]]
            if "--add-label" in argv:
                labels.append(argv[argv.index("--add-label") + 1])
            state["issue_labels"] = labels
            return _ok_result()
        if argv[:3] == ["gh", "project", "item-edit"]:
            if "opt_done" in argv:
                state["project_status"] = "Done"
                raise uis.KernelError("project item-edit network error after commit")
            if "opt_backlog" in argv:
                state["project_status"] = "Backlog"
                return _ok_result()
        return _ok_result()

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number,
            "title": "Epic",
            "body": epic_body(),
            "state": state["issue_state"],
            "labels": [{"name": name} for name in state["issue_labels"]],
        },
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    monkeypatch.setattr(uis, "run", fake_run)
    monkeypatch.setattr(common, "run", fake_run)
    monkeypatch.setattr(
        uis,
        "board_edit",
        lambda number, status, cwd=None: ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"],
    )
    monkeypatch.setattr(
        common,
        "board_edit",
        lambda number, status, cwd=None: ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"],
    )
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "ensure_label", lambda *args, **kwargs: None)

    with pytest.raises(uis.KernelError, match="project item-edit network error after commit"):
        uis.apply_epic_reconciliation(100)

    backlog_board_edits = [argv for argv in run_calls if argv[:3] == ["gh", "project", "item-edit"] and "opt_backlog" in argv]
    assert len(backlog_board_edits) == 1
    assert state["issue_state"] == "OPEN"
    assert "status:backlog" in state["issue_labels"]
    assert "status:done" not in state["issue_labels"]
    assert state["project_status"] == "Backlog"


def test_epic_rollback_ambiguous_repair_command_lands_then_raises_settles_on_readback(monkeypatch):
    """A rollback repair command may fail locally after landing remotely; if authoritative
    readback proves OPEN/Backlog/Backlog, treat rollback as settled."""
    state = {"issue_state": "OPEN", "issue_labels": ["type:epic", "status:done"], "project_status": "Done"}
    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "edit"]:
            state["issue_labels"] = ["type:epic", "status:backlog"]
            return _ok_result()
        if argv[:3] == ["gh", "project", "item-edit"]:
            if "opt_backlog" in argv:
                state["project_status"] = "Backlog"
                raise uis.KernelError("rollback item-edit connection reset after commit")
        return _ok_result()

    monkeypatch.setattr(uis, "run", fake_run)
    monkeypatch.setattr(common, "run", fake_run)
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: {"number": number, "state": state["issue_state"], "labels": [{"name": name} for name in state["issue_labels"]]})
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: {"number": number, "state": state["issue_state"], "labels": [{"name": name} for name in state["issue_labels"]]})
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: state["project_status"])
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: state["project_status"])
    monkeypatch.setattr(
        uis,
        "board_edit",
        lambda number, status, cwd=None: ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"],
    )
    monkeypatch.setattr(
        common,
        "board_edit",
        lambda number, status, cwd=None: ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"],
    )
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(common, "ensure_label", lambda *args, **kwargs: None)

    uis._rollback_epic_reconciliation(100, original=uis.KernelError("original failure"))

    assert state["issue_state"] == "OPEN"
    assert "status:backlog" in state["issue_labels"]
    assert state["project_status"] == "Backlog"


def test_cli_epic_reconcile_check_and_apply(monkeypatch, capsys):
    monkeypatch.setattr(
        uis,
        "epic_reconcile_evidence",
        lambda issue, cwd=None: {
            "issue": issue,
            "closable": True,
            "blocked": False,
            "blockers": [],
            "status": "Backlog",
            "project_status": "Backlog",
            "state": "OPEN",
            "children": {},
        },
    )
    monkeypatch.setattr(sys, "argv", ["update_issue_status.py", "--issue", "100", "--reconcile-epic", "--check"])
    assert uis.main() == 0
    out = capsys.readouterr().out
    assert '"project_status": "Backlog"' in out
    assert '"closable": true' in out
