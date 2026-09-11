from __future__ import annotations

import pytest

import merge_state
import triage_backlog
from test_governed_merge import board_evidence, issue_record

BODY = "## Acceptance Criteria\n\n- [ ] exact behavior is verified\n\n### touches:\nsrc/example.py\n"


def pinned(record, number=7):
    label = merge_state.READY_PREFIX + merge_state.ready_digest(number, record["body"])
    return {**record, "labels": [*record["labels"], {"name": label}]}


@pytest.fixture(autouse=True)
def board(monkeypatch):
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: board_evidence())


def test_digest_ignores_ticks_but_tracks_scope():
    digest = merge_state.ready_digest(7, BODY)
    assert digest == merge_state.ready_digest(7, BODY.replace("- [ ]", "- [x]"))
    assert digest != merge_state.ready_digest(8, BODY)
    assert digest != merge_state.ready_digest(7, BODY.replace("src/example.py", "src/example.py, scripts/**"))
    assert digest != merge_state.ready_digest(7, BODY.replace("exact behavior", "any behavior"))


def test_matching_pin_passes_and_a_missing_pin_is_grandfathered(monkeypatch):
    for record in (pinned(issue_record("src/example.py")), issue_record("src/example.py")):
        monkeypatch.setattr(merge_state, "issue", lambda _n, r=record: r)
        assert merge_state.issue_gate([7], ["src/example.py"])[0]["issue"] == 7


def test_scope_widened_after_promotion_is_refused(monkeypatch):
    promoted = pinned(issue_record("src/example.py"))
    widened = {**promoted, "body": promoted["body"].replace("src/example.py", "src/example.py, scripts/**")}
    monkeypatch.setattr(merge_state, "issue", lambda _n: widened)
    # Without the pin, the widened touches: would admit this path.
    with pytest.raises(merge_state.KernelError, match="changed after promotion"):
        merge_state.issue_gate([7], ["scripts/merge_pr.py"])


def test_two_pins_are_refused(monkeypatch):
    record = pinned(issue_record("src/example.py"))
    record["labels"].append({"name": "ready:0000000000000000"})
    monkeypatch.setattr(merge_state, "issue", lambda _n: record)
    with pytest.raises(merge_state.KernelError, match="changed after promotion"):
        merge_state.issue_gate([7], ["src/example.py"])


def test_promotion_pins_the_evaluated_body_and_drops_stale_pins(monkeypatch):
    labels, commands = [], []
    monkeypatch.setattr(triage_backlog, "ensure_label", lambda name, **_kwargs: labels.append(name))
    monkeypatch.setattr(triage_backlog, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(triage_backlog, "issue", lambda _n: pytest.fail("must pin the evaluated record"))
    monkeypatch.setattr(triage_backlog, "set_status", lambda *_args, **_kwargs: None)
    triage_backlog.promote_issue(7, record={"number": 7, "body": BODY, "labels": [{"name": "ready:stale"}]})
    pin = merge_state.READY_PREFIX + merge_state.ready_digest(7, BODY)
    assert labels == [pin]
    assert commands == [["gh", "issue", "edit", "7", "--add-label", pin, "--remove-label", "ready:stale"]]


def test_close_out_deletes_the_pin_only_after_the_final_gate(monkeypatch):
    record = pinned(issue_record("src/example.py"))
    record.update(state="CLOSED", labels=[{"name": "status:done"}, *record["labels"][1:]])
    monkeypatch.setattr(merge_state, "issue", lambda _n: record)
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: board_evidence("Done"))
    deleted = []
    monkeypatch.setattr(merge_state, "run", lambda argv, **kwargs: deleted.append((argv, kwargs)))
    merge_state.close_out([7], ["src/example.py"])
    pin = merge_state.READY_PREFIX + merge_state.ready_digest(7, record["body"])
    assert deleted == [(["gh", "label", "delete", pin, "--yes"], {"check": False})]
