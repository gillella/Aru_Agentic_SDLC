from __future__ import annotations

import pytest

import common
import update_issue_status as uis
from epic_support import (
    _mock_mutation_pipeline,
    _ok_result,
    _tripwire_no_mutation,
    child_snapshot,
    epic_body,
    epic_record,
    mock_epic_context,
)


def test_epic_reconcile_success(monkeypatch):
    epic = epic_record()
    settled = {**epic, "state": "CLOSED", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    reads = iter([epic, settled])
    project_statuses = iter(["Backlog", "Done"])
    calls = {"close": 0, "set_status": 0}
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(reads),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
        project_status_fn=lambda number, cwd=None: next(project_statuses),
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "set_status", lambda number, status, **kw: calls.__setitem__("set_status", calls["set_status"] + 1))
        monkeypatch.setattr(mod, "run", lambda argv, **kw: (calls.__setitem__("close", calls["close"] + 1) if argv[:3] == ["gh", "issue", "close"] else None) or _ok_result())
    result = uis.apply_epic_reconciliation(100)
    assert result["applied"] is True and result["after"] == "Done" and result["state"] == "CLOSED"
    assert result["project_status"] == "Done" and calls == {"close": 1, "set_status": 1}


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
def test_epic_reconcile_adversarial_prestate_blocks_with_zero_mutations(monkeypatch, issue_status, project_status, expected_blockers):
    labels = ["type:epic"] + ([f"status:{issue_status.lower().replace(' ', '-')}"] if issue_status else [])
    mock_epic_context(
        monkeypatch,
        epic=epic_record(labels=labels),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status=issue_status,
        project_status=project_status,
    )
    _tripwire_no_mutation(monkeypatch)

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True and evidence["closable"] is False
    assert evidence["status"] == issue_status and evidence["project_status"] == project_status
    for blocker in expected_blockers:
        assert any(blocker in item for item in evidence["blockers"])
    with pytest.raises(uis.KernelError):
        uis.apply_epic_reconciliation(100)


@pytest.mark.parametrize(
    ("race_issue", "race_project"),
    [
        ("Ready", "Backlog"),
        ("Backlog", "Ready"),
        ("Ready", "Ready"),
    ],
)
def test_epic_reconcile_adversarial_prestate_race_blocks_with_zero_close_or_done_mutations(
    monkeypatch, race_issue, race_project
):
    """Adversarial transition after evidence validation must cause set_status to fail closed
    on expected_current='Backlog' before any mutation, executing zero close or Done mutations."""
    state = {"issue_status": "Backlog", "project_status": "Backlog"}
    commands = []

    def fake_evidence(number, cwd=None):
        state["issue_status"] = race_issue
        state["project_status"] = race_project
        return {
            "issue": number, "closable": True, "blocked": False, "blockers": [],
            "status": "Backlog", "project_status": "Backlog", "state": "OPEN",
            "children": {91: child_snapshot(91), 92: child_snapshot(92)},
        }

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: epic_record(labels=["type:epic", f"status:{state['issue_status'].lower()}"]),
        status_fn=lambda record: state["issue_status"],
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    monkeypatch.setattr(uis, "epic_reconcile_evidence", fake_evidence)
    _mock_mutation_pipeline(monkeypatch, commands)

    with pytest.raises(uis.StatusPreconditionError, match="must both equal expected 'Backlog'"):
        uis.apply_epic_reconciliation(100)

    assert commands == []


def test_epic_reconcile_adversarial_board_drift_blocks_with_zero_rollback(monkeypatch):
    """When Project card drifts to Ready during board_edit snapshot, epic reconciliation fails closed with zero rollback."""
    commands = []
    mock_epic_context(
        monkeypatch,
        epic=epic_record(),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
    )
    def fake_board(number, status, *a, **kw):
        raise common.StatusPreconditionError("issue #100 Project card status ('Ready') does not equal expected 'Backlog'")

    _mock_mutation_pipeline(monkeypatch, commands, board_edit_fn=fake_board)

    with pytest.raises(uis.StatusPreconditionError, match=r"Project card status \('Ready'\) does not equal expected 'Backlog'"):
        uis.apply_epic_reconciliation(100)

    assert commands == []


def test_epic_reconcile_issue_drifts_to_ready_blocks_in_pre_mutation_check_with_zero_mutations(monkeypatch):
    """When the issue status drifts to Ready after evidence collection, the pre-mutation
    recollection inside set_status fails closed before any issue or card mutation."""
    commands = []
    issue_reads = [
        epic_record(labels=["type:epic", "status:backlog"]),
        epic_record(labels=["type:epic", "status:backlog"]),
        epic_record(labels=["type:epic", "status:backlog"]),
        epic_record(labels=["type:epic", "status:ready"]),
    ]
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: issue_reads.pop(0) if issue_reads else epic_record(labels=["type:epic", "status:ready"]),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
    )
    _mock_mutation_pipeline(monkeypatch, commands)

    with pytest.raises(uis.StatusPreconditionError, match=r"epic #100 evidence drifted before apply"):
        uis.apply_epic_reconciliation(100)

    assert commands == []


def test_epic_reconcile_ensure_label_failure_does_not_invoke_rollback(monkeypatch):
    """When ensure_label fails before issue/card mutation, epic reconciliation re-raises with zero rollback."""
    commands = []
    mock_epic_context(
        monkeypatch,
        epic=epic_record(),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
    )
    _mock_mutation_pipeline(monkeypatch, commands)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "ensure_label", lambda *a, **kw: (_ for _ in ()).throw(common.KernelError("gh label create failed: network timeout")))

    with pytest.raises(uis.StatusPreconditionError, match="gh label create failed: network timeout"):
        uis.apply_epic_reconciliation(100)

    assert commands == []


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
    _tripwire_no_mutation(monkeypatch)

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
        monkeypatch, epic=epic, snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Backlog" if record is epic else "Done", project_status="Backlog",
    )
    _mock_mutation_pipeline(monkeypatch, mutations)

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["closable"] is True and evidence["blocked"] is False
    assert evidence["status"] == "Backlog" and evidence["project_status"] == "Backlog"
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
        status="Backlog", project_status="Backlog",
    )
    assert "epic-close-policy is ambiguous" in uis.epic_reconcile_evidence(100)["blockers"]


def test_epic_reconcile_blocks_unless_state_is_exactly_open(monkeypatch):
    mock_epic_context(monkeypatch, epic=epic_record(policy=None, state="CLOSED"))
    assert "epic is not open" in uis.epic_reconcile_evidence(100)["blockers"]


def test_epic_reconcile_contradictory_epic_status_blocks_without_crashing(monkeypatch):
    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\n"
    epic = {
        "number": 100, "body": body, "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:in-review"}, {"name": "status:done"}],
    }
    mock_epic_context(monkeypatch, epic=epic, snapshots={91: child_snapshot(91)}, project_status="Backlog")
    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True and evidence["closable"] is False and evidence["status"] is None
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
    _mock_mutation_pipeline(monkeypatch)

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


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("## Child Issues\n- #1\n", "missing"),
        ("epic-close-policy: children-only\n## Child Issues\n- #1\n", "missing"),
        ("## Description\n```\nepic-close-policy: children-only\n```\n## Child Issues\n- #1\n", "missing"),
        ("> epic-close-policy: children-only\n## Child Issues\n- #1\n", "missing"),
        ("## Child Issues\n- #1\n\n> epic-close-policy: children-only\n", "unexpected trailer content"),
        ("## Child Issues\n- #1\n\n```\nepic-close-policy: children-only\n```\n", "unexpected trailer content"),
        ("## Child Issues\nepic-close-policy: children-only\n- #1\n", "unexpected content"),
        ("## Child Issues\n- #1\n\nepic-close-policy: children-only\nepic-close-policy: manual\n", "ambiguous"),
        ("## Child Issues\n- #1\n\n## Child Issues\n- #2\n", "more than one ## Child Issues section"),
        ("## Example\n~~~\n## Child Issues\n- #1\n\nepic-close-policy: children-only\n~~~\n", "must contain a ## Child Issues section"),
        ("```\n## Child Issues\n- #1\n\nepic-close-policy: children-only\n", "must contain a ## Child Issues section"),
        ("    ## Child Issues\n    - #1\n\n    epic-close-policy: children-only\n", "must contain a ## Child Issues section"),
        ("> ## Child Issues\n> - #1\n>\n> epic-close-policy: children-only\n", "must contain a ## Child Issues section"),
    ],
)
def test_epic_close_policy_adversarial_binding(body, message):
    with pytest.raises(uis.KernelError, match=message):
        uis.epic_close_policy(body)


def test_epic_close_policy_requires_one_declaration():
    assert uis.epic_close_policy("## Child Issues\n- #1\n\nepic-close-policy: children-only\n") == "children-only"


def test_child_issues_section_ignores_fenced_and_quoted_examples():
    body = (
        "## Overview\n\n```\n## Child Issues\n- #999\n\nepic-close-policy: manual\n```\n\n"
        "> ## Child Issues\n> - #888\n\n## Child Issues\n- #2\n- #1\n\nepic-close-policy: children-only\n"
    )
    assert uis.parse_child_issues(body) == [1, 2] and uis.epic_close_policy(body) == "children-only"


@pytest.mark.parametrize(
    ("drifted_snapshot", "match"),
    [
        (child_snapshot(91, state="OPEN", status="done"), "child #91 is not closed"),
        (child_snapshot(91, state="CLOSED", status="ready"), "child #91 is not status:done"),
    ],
)
def test_epic_reconcile_adversarial_child_drift_blocks_with_zero_mutations(monkeypatch, drifted_snapshot, match):
    """When a child reopens or loses status:done before apply, fresh evidence blocks with StatusPreconditionError and zero mutations."""
    commands = []
    snapshots_seq = [
        {91: child_snapshot(91, state="CLOSED", status="done")},
        {91: drifted_snapshot},
    ]
    mock_epic_context(monkeypatch, epic=epic_record(children=[91]), status="Backlog", project_status="Backlog")
    monkeypatch.setattr(uis, "child_issue_snapshots", lambda numbers, cwd=None: snapshots_seq.pop(0))
    _mock_mutation_pipeline(monkeypatch, commands)
    with pytest.raises(uis.StatusPreconditionError, match=match):
        uis.apply_epic_reconciliation(100)
    assert commands == []


@pytest.mark.parametrize(
    ("markdown", "unchecked"),
    [
        ("## Acceptance Criteria\n\n- [ ] ship the thing\n", ["- [ ] ship the thing"]),
        ("## Acceptance Criteria\n\n- [x] ship the thing\n", []),
        ("## Acceptance Criteria\n\n- [X] done\n- [ ] not yet\n", ["- [ ] not yet"]),
        ("## Acceptance Criteria\n\n```\n- [ ] fenced example\n```\n", []),
        ("## Acceptance Criteria\n\n~~~\n- [ ] tilde-fenced example\n~~~\n", []),
        ("## Acceptance Criteria\n\n> - [ ] quoted example\n", []),
        ("## Acceptance Criteria\n\n    - [ ] indented example\n", []),
        ("## Notes\n\n```\n## Acceptance Criteria\n- [ ] fenced heading\n```\n", []),
        ("> ## Acceptance Criteria\n> - [ ] quoted section\n", []),
        ("## Acceptance Criteria\n\n- [ ]\n- [ ] real item\n", ["- [ ] real item"]),
        ("## Acceptance Criteria\n\n```\n- [ ] ex\n```\n\n- [ ] outside the fence\n",
         ["- [ ] outside the fence"]),
    ],
)
def test_unchecked_acceptance_criteria_scanner_is_markdown_aware(markdown, unchecked):
    assert uis.unchecked_acceptance_criteria(markdown) == unchecked


@pytest.mark.parametrize(
    ("acceptance", "blocks"),
    [
        ("\n\n## Acceptance Criteria\n\n- [ ] ship the thing\n", True),
        ("\n\n## Acceptance Criteria\n\n- [x] shipped\n", False),
        ("\n\n## Acceptance Criteria\n\n```\n- [ ] example only\n```\n", False),
        ("\n\n## Acceptance Criteria\n\n> - [ ] quoted example\n", False),
        ("\n\n## Acceptance Criteria\n\n    - [ ] indented example\n", False),
        ("\n\n## Notes\n\n```\n## Acceptance Criteria\n- [ ] fenced heading\n```\n", False),
    ],
)
def test_epic_reconcile_acceptance_criteria_gate_blocks_with_zero_mutations(
    monkeypatch, acceptance, blocks
):
    body = epic_body() + acceptance
    mock_epic_context(
        monkeypatch,
        epic={
            "number": 100, "state": "OPEN", "body": body,
            "labels": [{"name": "type:epic"}, {"name": "status:backlog"}],
        },
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
    )
    _tripwire_no_mutation(monkeypatch)

    evidence = uis.epic_reconcile_evidence(100)
    if blocks:
        assert evidence["blocked"] is True and evidence["closable"] is False
        assert any(
            "Acceptance Criteria" in item and "unchecked" in item
            for item in evidence["blockers"]
        )
        with pytest.raises(uis.KernelError, match="unchecked"):
            uis.apply_epic_reconciliation(100)
    else:
        assert evidence["closable"] is True, evidence["blockers"]
