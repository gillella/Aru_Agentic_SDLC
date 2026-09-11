"""The scope contract after the Ready pin was removed.

`touches:` still reserves paths and still refuses a diff that falls outside the
declaration. What is gone is the assertion that the declaration has not changed since
promotion, because the kernel offered no way to satisfy it: there is no transition to
Backlog, and once a pull request is open the issue is In Review and release refuses, so
the declaration was frozen exactly when a correction was most likely.
"""

from __future__ import annotations

import pytest

import merge_state
import triage_backlog
from test_governed_merge import board_evidence, issue_record


@pytest.fixture(autouse=True)
def board(monkeypatch):
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: board_evidence())


def test_the_live_declaration_still_bounds_the_diff(monkeypatch):
    monkeypatch.setattr(merge_state, "issue", lambda _n: issue_record("src/example.py"))
    with pytest.raises(merge_state.KernelError, match="outside the linked issue touches contract"):
        merge_state.issue_gate([7], ["scripts/merge_pr.py"])
    assert merge_state.issue_gate([7], ["src/example.py"])


def test_a_declaration_corrected_in_flight_is_honoured(monkeypatch):
    """The point of the change: correcting scope needs no status change and no board edit."""
    promoted = issue_record("src/example.py")
    corrected = {**promoted,
                 "body": promoted["body"].replace("src/example.py", "src/example.py, scripts/**")}
    monkeypatch.setattr(merge_state, "issue", lambda _n: corrected)
    evidence = merge_state.issue_gate([7], ["scripts/merge_pr.py"])
    # And the scope that was actually enforced is recorded, so it stays auditable.
    assert evidence[0]["touches"] == ["src/example.py", "scripts/**"]


def test_a_stale_scope_label_no_longer_refuses_a_merge(monkeypatch):
    """Issues promoted before this change still carry a ready:* label; it must be inert."""
    record = issue_record("src/example.py")
    record["labels"].append({"name": "ready:0000000000000000"})
    monkeypatch.setattr(merge_state, "issue", lambda _n: record)
    assert merge_state.issue_gate([7], ["src/example.py"])


def test_promotion_no_longer_stamps_a_scope_pin():
    assert not hasattr(triage_backlog, "pin_ready_contract")
    assert not hasattr(merge_state, "ready_digest")


def test_no_kernel_message_instructs_an_unimplemented_transition():
    """The kernel must not tell an operator to do something it gives them no way to do."""
    from pathlib import Path

    kernel = Path(merge_state.__file__).resolve().parent
    offenders = [
        path.name
        for path in kernel.glob("*.py")
        if "return it to Backlog" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
