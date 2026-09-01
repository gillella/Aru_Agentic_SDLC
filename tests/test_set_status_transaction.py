from __future__ import annotations

import subprocess

import pytest

import common


def board_payload(
    *,
    items: list[dict] | None = None,
    has_next_page: bool = False,
    field: dict | None = None,
) -> dict:
    if items is None:
        items = [{"id": "PVTI_7", "project": {"id": "PVT_1"}}]
    if field is None:
        field = {
            "id": "PVTSSF_status",
            "name": "Status",
            "options": [
                {"id": "backlog-option", "name": "Backlog"},
                {"id": "ready-option", "name": "Ready"},
                {"id": "done-option", "name": "Done"},
            ],
        }
    return {
        "data": {
            "issueNode": {"projectItems": {"nodes": items, "pageInfo": {"hasNextPage": has_next_page}}},
            "projectNode": {"field": field},
        }
    }


@pytest.mark.parametrize(
    ("issue_status", "project_status", "expected", "should_fail"),
    [
        ("Backlog", "Backlog", "Backlog", False),
        ("Ready", "Backlog", "Backlog", True),
        ("Backlog", "Ready", "Backlog", True),
        ("Ready", "Ready", "Backlog", True),
    ],
)
def test_set_status_expected_current_contract(
    monkeypatch, issue_status, project_status, expected, should_fail
):
    commands = []
    settled = False

    def issue_record(number, cwd=None):
        value = "Done" if settled else issue_status
        return {"number": 7, "state": "OPEN", "labels": [{"name": f"status:{value.lower()}"}]}

    monkeypatch.setattr(common, "issue", issue_record)
    monkeypatch.setattr(
        common,
        "project_item_status",
        lambda number, cwd=None: "Done" if settled else project_status,
    )
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: None)
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])
    def command(argv, **kw):
        nonlocal settled
        commands.append(argv)
        if argv[:3] == ["gh", "project", "item-edit"]:
            settled = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(common, "run", command)

    if should_fail:
        with pytest.raises(common.StatusPreconditionError, match=f"must both equal expected {expected!r}"):
            common.set_status(7, "Done", expected_current=expected)
        assert commands == []
    else:
        common.set_status(7, "Done", expected_current=expected)
        assert any(cmd[:3] == ["gh", "issue", "edit"] and "--add-label" in cmd for cmd in commands)
        assert any(cmd[:3] == ["gh", "project", "item-edit"] for cmd in commands)


@pytest.mark.parametrize(
    ("rollback_error", "match_patterns"),
    [
        (common.KernelError("gh failed: issue edit failed: 502 Bad Gateway"), ["issue edit failed: 502 Bad Gateway", "project item-edit network error: 500"]),
        (common.KernelError(common.QUOTA_STOP_MESSAGE), ["quota exhausted", "stop and wait", "project item-edit network error: 500"]),
    ],
)
def test_set_status_rollback_failure_preserves_messages_and_quota(monkeypatch, rollback_error, match_patterns):
    commands = []
    issue_rec = {"number": 7, "state": "OPEN", "labels": [{"name": "status:backlog"}]}
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_rec)
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: "Backlog")
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: None)
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])

    def fake_run(argv, **kw):
        commands.append(argv)
        if argv[:3] == ["gh", "project", "item-edit"]:
            raise common.KernelError("gh failed: project item-edit network error: 500")
        if argv[:3] == ["gh", "issue", "edit"] and "--remove-label" in argv and argv[argv.index("--remove-label") + 1] == "status:done":
            raise rollback_error
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(common, "run", fake_run)
    with pytest.raises(common.KernelError) as exc_info:
        common.set_status(7, "Done")
    for pattern in match_patterns:
        assert pattern in str(exc_info.value)
    assert len(commands) == 3


def test_set_status_adversarial_board_drift_blocks_before_issue_edit(monkeypatch):
    """When project status drifts between initial precheck and board_edit snapshot,
    set_status fails closed with zero issue or project Done commands."""
    commands = []
    issue_rec = {"number": 7, "state": "OPEN", "labels": [{"name": "status:backlog"}]}
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_rec)
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: commands.append(["ensure_label", *a]))
    monkeypatch.setattr(common, "run", lambda argv, **kw: commands.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
    snapshots = [
        board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Backlog"}}]),
        board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Ready"}}]),
    ]

    def fake_gh_json(args, *, cwd=None, auth=None):
        if args[:2] == ["api", "repos/owner/repo/issues/7"]:
            return {"number": 7, "node_id": "I_7"}
        return snapshots.pop(0)

    monkeypatch.setattr(common, "gh_json", fake_gh_json)
    with pytest.raises(common.StatusPreconditionError, match=r"Project card status \('Ready'\) does not equal expected 'Backlog'"):
        common.set_status(7, "Done", expected_current="Backlog")
    assert commands == []


def test_set_status_adversarial_issue_drift_before_mutation_blocks_on_final_reread(monkeypatch):
    """When issue status drifts to Ready during board_edit, final issue reread blocks with zero mutations."""
    commands = []
    issue_reads = [
        {"number": 7, "state": "OPEN", "labels": [{"name": "status:backlog"}]},
        {"number": 7, "state": "OPEN", "labels": [{"name": "status:ready"}]},
    ]
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_reads.pop(0))
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: "Backlog")
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: commands.append(["ensure_label", *a]))
    monkeypatch.setattr(common, "run", lambda argv, **kw: commands.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))

    with pytest.raises(common.StatusPreconditionError, match=r"issue #7 status \('Ready'\) does not equal expected 'Backlog'"):
        common.set_status(7, "Done", expected_current="Backlog")
    assert commands == []


def test_set_status_ensure_label_failure_precondition_distinction(monkeypatch):
    """ensure_label failure raises StatusPreconditionError under expected_current, but generic KernelError without."""
    issue_rec = {"number": 7, "state": "OPEN", "labels": [{"name": "status:backlog"}]}
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_rec)
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: "Backlog")
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: (_ for _ in ()).throw(common.KernelError("gh label create failed: network timeout")))

    with pytest.raises(common.StatusPreconditionError, match="gh label create failed: network timeout"):
        common.set_status(7, "Done", expected_current="Backlog")

    with pytest.raises(common.KernelError, match="gh label create failed: network timeout") as excinfo:
        common.set_status(7, "Done")
    assert not isinstance(excinfo.value, common.StatusPreconditionError)


def test_set_status_pre_mutation_check_runs_after_preflight_before_first_mutation(monkeypatch):
    """The optional pre_mutation_check fires once the transition is authorised but
    before any issue/card mutation; raising from it leaves zero commands."""
    commands = []
    settled = False
    monkeypatch.setattr(
        common,
        "issue",
        lambda number, cwd=None: {
            "number": 7,
            "state": "OPEN",
            "labels": [{"name": "status:done" if settled else "status:backlog"}],
        },
    )
    monkeypatch.setattr(
        common,
        "project_item_status",
        lambda number, cwd=None: "Done" if settled else "Backlog",
    )
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: None)
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])
    def command(argv, **kw):
        nonlocal settled
        commands.append(argv)
        if argv[:3] == ["gh", "project", "item-edit"]:
            settled = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(common, "run", command)

    def _raise() -> None:
        raise common.StatusPreconditionError("child evidence drifted before apply")

    with pytest.raises(common.StatusPreconditionError, match="child evidence drifted before apply"):
        common.set_status(7, "Done", expected_current="Backlog", pre_mutation_check=_raise)
    assert commands == []

    observed = []
    common.set_status(
        7, "Done", expected_current="Backlog",
        pre_mutation_check=lambda: observed.append(list(commands)),
    )
    assert observed == [[]]
    assert any(cmd[:3] == ["gh", "issue", "edit"] for cmd in commands)
    assert any(cmd[:3] == ["gh", "project", "item-edit"] for cmd in commands)


def test_set_status_drift_callback_raises_before_any_ensure_label_or_mutation(monkeypatch):
    """The pre_mutation_check drift guard must fire before ensure_label so an
    aborting callback leaves zero label/run/board mutations behind."""
    commands: list[list] = []
    issue_rec = {"number": 7, "state": "OPEN", "labels": [{"name": "status:backlog"}]}
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_rec)
    monkeypatch.setattr(common, "project_item_status", lambda number, cwd=None: "Backlog")
    monkeypatch.setattr(
        common, "board_edit",
        lambda number, status, *a, **kw: commands.append(["board_edit", status]) or ["project", "item-edit", "--id", "1"],
    )
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: commands.append(["ensure_label", *a]))
    monkeypatch.setattr(
        common, "run",
        lambda argv, **kw: commands.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )

    def _raise() -> None:
        raise common.StatusPreconditionError("epic evidence drifted before apply")

    with pytest.raises(common.StatusPreconditionError, match="epic evidence drifted before apply"):
        common.set_status(7, "Done", expected_current="Backlog", pre_mutation_check=_raise)

    assert not any(cmd[:1] == ["ensure_label"] for cmd in commands)
    assert not any(cmd[:3] == ["gh", "issue", "edit"] for cmd in commands)
    assert not any(cmd[:3] == ["gh", "project", "item-edit"] for cmd in commands)


def test_set_status_ensure_label_runs_after_pre_mutation_check_and_before_issue_edit(monkeypatch):
    """When the drift guard passes, ensure_label executes only after it and
    immediately before the issue edit."""
    order: list[str] = []
    settled = False
    monkeypatch.setattr(
        common,
        "issue",
        lambda number, cwd=None: {
            "number": 7,
            "state": "OPEN",
            "labels": [{"name": "status:done" if settled else "status:backlog"}],
        },
    )
    monkeypatch.setattr(
        common,
        "project_item_status",
        lambda number, cwd=None: "Done" if settled else "Backlog",
    )
    monkeypatch.setattr(common, "board_edit", lambda number, status, *a, **kw: ["project", "item-edit", "--id", "1"])
    monkeypatch.setattr(common, "ensure_label", lambda *a, **kw: order.append("ensure_label"))
    def command(argv, **kw):
        nonlocal settled
        order.append(argv[1] if argv[:1] == ["gh"] else "run")
        if argv[:3] == ["gh", "project", "item-edit"]:
            settled = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(common, "run", command)

    common.set_status(
        7, "Done", expected_current="Backlog",
        pre_mutation_check=lambda: order.append("pre_mutation_check"),
    )

    assert order[:3] == ["pre_mutation_check", "ensure_label", "issue"]


def test_set_status_rejects_silent_non_settlement(monkeypatch):
    issue_rec = {
        "number": 7,
        "state": "OPEN",
        "labels": [{"name": "status:backlog"}],
    }
    monkeypatch.setattr(common, "issue", lambda number, cwd=None: issue_rec)
    monkeypatch.setattr(
        common, "project_item_status", lambda number, cwd=None: "Backlog"
    )
    monkeypatch.setattr(
        common,
        "board_edit",
        lambda number, status, *args, **kwargs: [
            "project", "item-edit", "--id", "1"
        ],
    )
    monkeypatch.setattr(common, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        common,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr=""
        ),
    )

    with pytest.raises(common.KernelError, match="did not settle"):
        common.set_status(7, "Done", expected_current="Backlog")
