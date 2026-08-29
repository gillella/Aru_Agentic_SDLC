from __future__ import annotations

import sys

import pytest

import common
import update_issue_status as uis
from epic_support import (
    _mock_mutation_pipeline,
    _ok_result,
    child_snapshot,
    epic_body,
    epic_record,
    mock_epic_context,
)


def test_apply_epic_reconciliation_read_back_fails_closed(monkeypatch):
    epic = epic_record()
    final = {**epic, "state": "OPEN", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    readbacks = iter([epic, epic, final])
    rollback = []
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(readbacks),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "set_status", lambda *a, **kw: None)
    project_statuses = iter(["Backlog", "Backlog", "Done"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_statuses))
    monkeypatch.setattr(uis, "_rollback_epic_reconciliation", lambda *args, **kwargs: rollback.append(kwargs))

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert len(rollback) == 1


def test_epic_rollback_reopens_and_reverts_status_when_close_settled_but_card_did_not(monkeypatch):
    """Exercise the real rollback function end-to-end, restoring exact Backlog."""
    epic = epic_record()
    final = {**epic, "state": "CLOSED", "labels": [{"name": "type:epic"}, {"name": "status:done"}]}
    reads = iter([epic, epic, final, final, epic])
    run_calls = []
    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: next(reads),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", [])) else "Backlog",
        project_status="Backlog",
    )
    _mock_mutation_pipeline(monkeypatch, run_calls)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "set_status", lambda *a, **kw: None)

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
    _mock_mutation_pipeline(monkeypatch, run_calls)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "set_status", lambda *a, **kw: None)

    def fail_close(argv, **kwargs):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return _ok_result()

    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fail_close)
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
    [("unsettled", "did not settle"), ("command_error", "reopen failed")],
)
def test_rollback_epic_reconciliation_surfaces_combined_error(monkeypatch, fail_mode, expected_match):
    monkeypatch.setattr(uis, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])

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
                labels = [lbl for lbl in labels if lbl != argv[argv.index("--remove-label") + 1]]
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
        issue_fn=lambda number, cwd=None: {"number": number, "title": "Epic", "body": epic_body(), "state": state["issue_state"], "labels": [{"name": name} for name in state["issue_labels"]]},
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)

    with pytest.raises(uis.KernelError, match="project item-edit network error after commit"):
        uis.apply_epic_reconciliation(100)

    backlog_board_edits = [argv for argv in run_calls if argv[:3] == ["gh", "project", "item-edit"] and "opt_backlog" in argv]
    assert len(backlog_board_edits) == 1
    assert state["issue_state"] == "OPEN" and "status:backlog" in state["issue_labels"]
    assert "status:done" not in state["issue_labels"] and state["project_status"] == "Backlog"


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

    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "issue", lambda number, cwd=None: {"number": number, "state": state["issue_state"], "labels": [{"name": name} for name in state["issue_labels"]]})
        monkeypatch.setattr(mod, "project_item_status", lambda number, cwd=None: state["project_status"])

    uis._rollback_epic_reconciliation(100, original=uis.KernelError("original failure"))

    assert state["issue_state"] == "OPEN" and "status:backlog" in state["issue_labels"] and state["project_status"] == "Backlog"


def test_cli_epic_reconcile_check_and_apply(monkeypatch, capsys):
    monkeypatch.setattr(
        uis,
        "epic_reconcile_evidence",
        lambda issue, cwd=None: {
            "issue": issue, "closable": True, "blocked": False, "blockers": [],
            "status": "Backlog", "project_status": "Backlog", "state": "OPEN", "children": {},
        },
    )
    monkeypatch.setattr(sys, "argv", ["update_issue_status.py", "--issue", "100", "--reconcile-epic", "--check"])
    assert uis.main() == 0
    out = capsys.readouterr().out
    assert '"project_status": "Backlog"' in out and '"closable": true' in out


def test_epic_reconcile_acceptance_criterion_unchecked_mid_transaction_rolls_back(monkeypatch):
    """A criterion flips to unchecked after the epic settles Done+closed; the
    closure-invariant recheck must roll the epic back to OPEN/Backlog/Backlog."""
    state = {
        "epic_state": "OPEN", "epic_labels": ["type:epic", "status:backlog"],
        "project_status": "Backlog", "accept": "- [x] verified",
    }
    run_calls: list[list[str]] = []

    def body() -> str:
        return epic_body() + f"\n\n## Acceptance Criteria\n\n{state['accept']}\n"

    def fake_run(argv, **kw):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            state["epic_state"] = "CLOSED"
            state["accept"] = "- [ ] regression found during closure"
        elif argv[:3] == ["gh", "issue", "reopen"]:
            state["epic_state"] = "OPEN"
        elif argv[:3] == ["gh", "issue", "edit"]:
            labels = list(state["epic_labels"])
            if "--remove-label" in argv:
                labels = [name for name in labels if name != argv[argv.index("--remove-label") + 1]]
            if "--add-label" in argv:
                labels.append(argv[argv.index("--add-label") + 1])
            state["epic_labels"] = labels
        elif argv[:3] == ["gh", "project", "item-edit"]:
            state["project_status"] = "Done" if "opt_done" in argv else "Backlog"
        return _ok_result()

    def fake_set_status(number, status, *, expected_current=None, pre_mutation_check=None, cwd=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        state["epic_labels"] = ["type:epic", "status:done"]
        state["project_status"] = "Done"

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number, "body": body(), "state": state["epic_state"],
            "labels": [{"name": name} for name in state["epic_labels"]],
        },
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: (
            "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", []))
            else "Backlog"
        ),
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "set_status", fake_set_status)

    with pytest.raises(uis.KernelError, match="Acceptance Criteria"):
        uis.apply_epic_reconciliation(100)

    assert state["epic_state"] == "OPEN"
    assert "status:backlog" in state["epic_labels"] and "status:done" not in state["epic_labels"]
    assert state["project_status"] == "Backlog"
    assert ["gh", "issue", "reopen", "100"] in run_calls


def test_epic_reconcile_child_reopens_after_settlement_triggers_authoritative_rollback(monkeypatch):
    """Mutation lands (epic Done + closed) but a declared child reopens before the
    closure-invariant recheck; the post-close child_issue_snapshots reread must
    detect the regression and roll the epic back to OPEN/Backlog/Backlog."""
    state = {
        "epic_state": "OPEN", "epic_labels": ["type:epic", "status:backlog"],
        "project_status": "Backlog",
    }
    child = {91: ("CLOSED", "done"), 92: ("CLOSED", "done")}
    run_calls: list[list[str]] = []

    def fake_run(argv, **kw):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            state["epic_state"] = "CLOSED"
            child[91] = ("OPEN", "backlog")  # child drifts open during the transaction
        elif argv[:3] == ["gh", "issue", "reopen"]:
            state["epic_state"] = "OPEN"
        elif argv[:3] == ["gh", "issue", "edit"]:
            labels = list(state["epic_labels"])
            if "--remove-label" in argv:
                labels = [name for name in labels if name != argv[argv.index("--remove-label") + 1]]
            if "--add-label" in argv:
                labels.append(argv[argv.index("--add-label") + 1])
            state["epic_labels"] = labels
        elif argv[:3] == ["gh", "project", "item-edit"]:
            state["project_status"] = "Done" if "opt_done" in argv else "Backlog"
        return _ok_result()

    def fake_set_status(number, status, *, expected_current=None, pre_mutation_check=None, cwd=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        state["epic_labels"] = ["type:epic", "status:done"]
        state["project_status"] = "Done"

    def fake_snapshots(numbers, cwd=None):
        return {n: child_snapshot(n, state=child[n][0], status=child[n][1]) for n in numbers}

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number, "body": epic_body(), "state": state["epic_state"],
            "labels": [{"name": name} for name in state["epic_labels"]],
        },
        status_fn=lambda record: (
            "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", []))
            else "Backlog"
        ),
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    monkeypatch.setattr(uis, "child_issue_snapshots", fake_snapshots)
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "set_status", fake_set_status)

    with pytest.raises(uis.KernelError, match="child issue"):
        uis.apply_epic_reconciliation(100)

    assert state["epic_state"] == "OPEN"
    assert "status:backlog" in state["epic_labels"] and "status:done" not in state["epic_labels"]
    assert state["project_status"] == "Backlog"
    assert ["gh", "issue", "reopen", "100"] in run_calls


def test_epic_evidence_records_full_sorted_dependency_roster(monkeypatch):
    """Machine evidence must carry the complete sorted depends-on roster, not
    only the unresolved entries, so a closed->closed swap is still visible."""
    mock_epic_context(
        monkeypatch,
        epic=epic_record(depends_on=[7, 5]),
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
        depends=[],
    )
    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["dependencies"] == [5, 7]
    assert evidence["closable"] is True


def test_epic_pre_mutation_drift_detects_dependency_swap_when_both_closed(monkeypatch):
    """#5 -> #6 in the depends-on roster (both CLOSED) must abort in the
    pre-mutation recollection with zero mutations even though nothing is open."""
    commands: list[list[str]] = []
    state = {"deps": [5]}

    def swap_then_board(number, status, *a, **kw):
        state["deps"] = [6]  # roster rewritten after evidence + preflight, before pre-mutation recheck
        return ["project", "item-edit", "--id", "i1", "--single-select-option-id", f"opt_{status.lower()}"]

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number, "state": "OPEN", "body": epic_body(depends_on=state["deps"]),
            "labels": [{"name": "type:epic"}, {"name": "status:backlog"}],
        },
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status="Backlog",
        project_status="Backlog",
        depends=[],
    )
    _mock_mutation_pipeline(monkeypatch, commands, board_edit_fn=swap_then_board)

    with pytest.raises(uis.StatusPreconditionError, match="evidence drifted before apply"):
        uis.apply_epic_reconciliation(100)

    assert commands == []


def test_epic_dependency_roster_change_after_settlement_rolls_back(monkeypatch):
    """The depends-on roster is rewritten mid-transaction (#5 -> #6, both CLOSED);
    the post-settlement closure invariant must force an authoritative rollback."""
    state = {
        "epic_state": "OPEN", "epic_labels": ["type:epic", "status:backlog"],
        "project_status": "Backlog", "deps": [5],
    }
    run_calls: list[list[str]] = []

    def body() -> str:
        return epic_body(depends_on=state["deps"])

    def fake_run(argv, **kw):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            state["epic_state"] = "CLOSED"
            state["deps"] = [6]
        elif argv[:3] == ["gh", "issue", "reopen"]:
            state["epic_state"] = "OPEN"
        elif argv[:3] == ["gh", "issue", "edit"]:
            labels = list(state["epic_labels"])
            if "--remove-label" in argv:
                labels = [name for name in labels if name != argv[argv.index("--remove-label") + 1]]
            if "--add-label" in argv:
                labels.append(argv[argv.index("--add-label") + 1])
            state["epic_labels"] = labels
        elif argv[:3] == ["gh", "project", "item-edit"]:
            state["project_status"] = "Done" if "opt_done" in argv else "Backlog"
        return _ok_result()

    def fake_set_status(number, status, *, expected_current=None, pre_mutation_check=None, cwd=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        state["epic_labels"] = ["type:epic", "status:done"]
        state["project_status"] = "Done"

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number, "body": body(), "state": state["epic_state"],
            "labels": [{"name": name} for name in state["epic_labels"]],
        },
        snapshots={91: child_snapshot(91), 92: child_snapshot(92)},
        status_fn=lambda record: (
            "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", []))
            else "Backlog"
        ),
        project_status_fn=lambda number, cwd=None: state["project_status"],
        depends=[],
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "set_status", fake_set_status)

    with pytest.raises(uis.KernelError, match="depends-on roster changed after settlement"):
        uis.apply_epic_reconciliation(100)

    assert state["epic_state"] == "OPEN"
    assert "status:backlog" in state["epic_labels"] and "status:done" not in state["epic_labels"]
    assert state["project_status"] == "Backlog"
    assert ["gh", "issue", "reopen", "100"] in run_calls


def test_epic_reconcile_child_status_loss_after_settlement_rolls_back(monkeypatch):
    """A child stays CLOSED but silently loses status:done during the transaction;
    the exact child-evidence comparison must still force an authoritative rollback."""
    state = {
        "epic_state": "OPEN", "epic_labels": ["type:epic", "status:backlog"],
        "project_status": "Backlog",
    }
    child = {91: ("CLOSED", "done")}
    run_calls: list[list[str]] = []

    def fake_run(argv, **kw):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            state["epic_state"] = "CLOSED"
            child[91] = ("CLOSED", "ready")
        elif argv[:3] == ["gh", "issue", "reopen"]:
            state["epic_state"] = "OPEN"
        elif argv[:3] == ["gh", "issue", "edit"]:
            labels = list(state["epic_labels"])
            if "--remove-label" in argv:
                labels = [name for name in labels if name != argv[argv.index("--remove-label") + 1]]
            if "--add-label" in argv:
                labels.append(argv[argv.index("--add-label") + 1])
            state["epic_labels"] = labels
        elif argv[:3] == ["gh", "project", "item-edit"]:
            state["project_status"] = "Done" if "opt_done" in argv else "Backlog"
        return _ok_result()

    def fake_set_status(number, status, *, expected_current=None, pre_mutation_check=None, cwd=None):
        if pre_mutation_check is not None:
            pre_mutation_check()
        state["epic_labels"] = ["type:epic", "status:done"]
        state["project_status"] = "Done"

    mock_epic_context(
        monkeypatch,
        issue_fn=lambda number, cwd=None: {
            "number": number, "body": epic_body(children=[91]), "state": state["epic_state"],
            "labels": [{"name": name} for name in state["epic_labels"]],
        },
        status_fn=lambda record: (
            "Done" if any(lbl.get("name") == "status:done" for lbl in record.get("labels", []))
            else "Backlog"
        ),
        project_status_fn=lambda number, cwd=None: state["project_status"],
    )
    monkeypatch.setattr(
        uis, "child_issue_snapshots",
        lambda numbers, cwd=None: {91: child_snapshot(91, state=child[91][0], status=child[91][1])},
    )
    _mock_mutation_pipeline(monkeypatch)
    for mod in (uis, common):
        monkeypatch.setattr(mod, "run", fake_run)
        monkeypatch.setattr(mod, "set_status", fake_set_status)

    with pytest.raises(uis.KernelError, match="child issue"):
        uis.apply_epic_reconciliation(100)

    assert state["epic_state"] == "OPEN"
    assert "status:backlog" in state["epic_labels"] and state["project_status"] == "Backlog"
    assert ["gh", "issue", "reopen", "100"] in run_calls
