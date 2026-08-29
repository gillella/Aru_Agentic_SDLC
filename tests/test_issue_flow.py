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
    records = [
        {**backlog_issue("type:epic"), "number": 4},
        {**backlog_issue("needs-human"), "number": 3},
    ]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: records)

    def unexpected_promotion(_number, _status):
        raise AssertionError("non-executable work must not enter Ready")

    monkeypatch.setattr(triage_backlog, "set_status", unexpected_promotion)

    assert triage_backlog.triage(promote_all=True) == {
        "promoted": [],
        "rejected": {
            3: ["needs-human issues cannot enter Ready"],
            4: ["type:epic issues cannot enter Ready"],
        },
    }


def test_triage_promotes_one_complete_issue(monkeypatch):
    records = [
        {"number": 2, "title": "blocked"},
        {"number": 3, "title": "ready"},
        {"number": 4, "title": "also ready"},
    ]
    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: records)
    monkeypatch.setattr(
        triage_backlog, "evaluate", lambda item: ["missing"] if item["number"] == 2 else []
    )
    promoted = []
    monkeypatch.setattr(
        triage_backlog, "set_status", lambda number, status: promoted.append((number, status))
    )
    result = triage_backlog.triage()
    assert result["promoted"] == [3]
    assert promoted == [(3, "Ready")]
    assert result["rejected"] == {2: ["missing"]}


@pytest.mark.parametrize("race", [False, True])
def test_claim_settlement_and_race_rollback(monkeypatch, race):
    snapshots = iter(
        [
            {"number": 7, "labels": [], "state": "OPEN"},
            {
                "number": 7,
                "labels": [{"name": "agent:codex-1"}]
                + ([{"name": "agent:codex-2"}] if race else []),
                "state": "OPEN",
            },
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
    monkeypatch.setattr(
        claim_issue, "set_status", lambda number, status: statuses.append((number, status))
    )

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
    monkeypatch.setattr(sys, "argv", ["claim_issue.py", "--issue", "7", "--agent", "codex-1"])

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
        lines.append("")
        lines.append(f"epic-close-policy: {policy}")
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
) -> dict:
    if labels is None:
        labels = ["type:epic", "status:backlog"]
    return {
        "number": number,
        "body": epic_body(children=children, policy=policy, depends_on=depends_on),
        "state": state,
        "labels": [{"name": name} for name in labels],
    }


def child_snapshot(
    number: int,
    *,
    state: str = "CLOSED",
    status: str = "done",
    repository: str = "owner/repo",
) -> dict:
    return {
        "number": number,
        "state": state,
        "repository": repository,
        "labels": [{"name": f"status:{status}"}],
    }


def test_epic_reconcile_success(monkeypatch):
    import update_issue_status as uis

    epic = epic_record()
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}
    calls = {"close": 0, "set_status": 0}
    settled = {
        **epic,
        "state": "CLOSED",
        "labels": [{"name": "type:epic"}, {"name": "status:done"}],
    }
    reads = iter([epic, settled])

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(reads))
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis, "status_of", lambda record: "Backlog" if record.get("state") == "OPEN" else "Done"
    )
    monkeypatch.setattr(
        uis,
        "set_status",
        lambda number, status, cwd=None: calls.__setitem__("set_status", calls["set_status"] + 1),
    )
    project_statuses = iter(["Backlog", "Done"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_statuses))

    def fake_run(argv, **kwargs):
        if argv[:3] == ["gh", "issue", "close"]:
            calls["close"] += 1
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(uis, "run", fake_run)

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
    import update_issue_status as uis

    labels = ["type:epic"]
    if issue_status:
        labels.append(f"status:{issue_status.lower().replace(' ', '-')}")
    epic = epic_record(labels=labels)
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "status_of", lambda record: issue_status)
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: project_status)

    def unexpected_mutation(*args, **kwargs):
        raise AssertionError("must not mutate when prestate is blocked")

    monkeypatch.setattr(uis, "set_status", unexpected_mutation)
    monkeypatch.setattr(uis, "run", unexpected_mutation)

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
    import update_issue_status as uis

    epic = epic_record()
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog")
    monkeypatch.setattr(
        uis,
        "project_item_status",
        lambda number, cwd=None: (_ for _ in ()).throw(
            uis.KernelError("Project Board card snapshot returned a GraphQL error")
        ),
    )

    def unexpected_mutation(*args, **kwargs):
        raise AssertionError("must not mutate when project evidence fails")

    monkeypatch.setattr(uis, "set_status", unexpected_mutation)
    monkeypatch.setattr(uis, "run", unexpected_mutation)

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
def test_epic_reconcile_blocked_cases(
    monkeypatch, labels, policy, children, snapshots, depends, blocker
):
    import update_issue_status as uis

    epic = epic_record(labels=labels, policy=policy, children=children)
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: depends)
    monkeypatch.setattr(uis, "child_issue_snapshots", lambda numbers, cwd=None: snapshots or {})
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "Backlog"
        if "status:backlog" in [lbl["name"] for lbl in record.get("labels", [])]
        else None,
    )
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert any(blocker in item for item in evidence["blockers"])


def test_epic_reconcile_check_reports_closable_without_mutation(monkeypatch):
    import update_issue_status as uis

    epic = epic_record()
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}
    mutations = []

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog" if record is epic else "Done")
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")
    monkeypatch.setattr(
        uis, "set_status", lambda *args, **kwargs: mutations.append(("set_status", args))
    )
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
    import update_issue_status as uis

    with pytest.raises(uis.KernelError, match=match):
        uis.parse_child_issues(body)


def test_parse_child_issues_ordering_and_trailers():
    import update_issue_status as uis

    assert uis.parse_child_issues("## Child Issues\n- #9\n- #2\n") == [2, 9]
    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\ndepends-on: #5\n"
    assert uis.parse_child_issues(body) == [91]


def test_epic_reconcile_ambiguous_policy_blocks(monkeypatch):
    import update_issue_status as uis

    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\nepic-close-policy: manual\n"
    epic = {"number": 100, "body": body, "state": "OPEN", "labels": [{"name": "type:epic"}, {"name": "status:backlog"}]}
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog")
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    evidence = uis.epic_reconcile_evidence(100)
    assert "epic-close-policy is ambiguous" in evidence["blockers"]


def test_epic_reconcile_blocks_unless_state_is_exactly_open(monkeypatch):
    import update_issue_status as uis

    epic = epic_record(policy=None, state="CLOSED")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    assert "epic is not open" in uis.epic_reconcile_evidence(100)["blockers"]


def test_epic_reconcile_contradictory_epic_status_blocks_without_crashing(monkeypatch):
    import update_issue_status as uis

    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\n"
    epic = {
        "number": 100,
        "body": body,
        "state": "OPEN",
        "labels": [
            {"name": "type:epic"},
            {"name": "status:in-review"},
            {"name": "status:done"},
        ],
    }
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {91: child_snapshot(91)}
    )
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    evidence = uis.epic_reconcile_evidence(100)

    assert evidence["blocked"] is True
    assert evidence["closable"] is False
    assert evidence["status"] is None
    assert any("contradictory status labels" in item for item in evidence["blockers"])


def test_epic_reconcile_mutation_failure_rolls_back(monkeypatch):
    import update_issue_status as uis

    epic = epic_record()
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}
    commands = []

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog" if record is epic else "Done")
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    def fail_close(argv, **kwargs):
        commands.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(uis, "run", fail_close)
    monkeypatch.setattr(
        uis, "_rollback_epic_reconciliation", lambda *args, **kwargs: commands.append(["rollback"])
    )

    with pytest.raises(uis.KernelError, match="close failed"):
        uis.apply_epic_reconciliation(100)

    assert ["gh", "issue", "close", "100", "--reason", "completed"] in commands
    assert ["rollback"] in commands


def test_epic_close_policy_requires_one_declaration():
    import update_issue_status as uis

    body = "## Child Issues\n- #1\n\nepic-close-policy: children-only\n"
    assert uis.epic_close_policy(body) == "children-only"
    with pytest.raises(uis.KernelError, match="missing"):
        uis.epic_close_policy("## Child Issues\n- #1\n")
    with pytest.raises(uis.KernelError, match="ambiguous"):
        uis.epic_close_policy("epic-close-policy: children-only\nepic-close-policy: manual\n")


def test_apply_epic_reconciliation_read_back_fails_closed(monkeypatch):
    import update_issue_status as uis

    epic = epic_record()
    final = {
        **epic,
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:done"}],
    }
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {91: child_snapshot(91), 92: child_snapshot(92)}
    )
    monkeypatch.setattr(
        uis, "status_of", lambda record: "Backlog" if record.get("state") == "OPEN" else "Done"
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: None)
    project_statuses = iter(["Backlog", "Done"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_statuses))
    readbacks = iter([epic, final])
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(readbacks))
    rollback = []
    monkeypatch.setattr(
        uis, "_rollback_epic_reconciliation", lambda *args, **kwargs: rollback.append(kwargs)
    )

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert len(rollback) == 1


def test_epic_rollback_reopens_and_reverts_status_when_close_settled_but_card_did_not(
    monkeypatch,
):
    """Exercise the real rollback function end-to-end, restoring exact Backlog."""
    import update_issue_status as uis

    epic = epic_record()
    final = {
        **epic,
        "state": "CLOSED",
        "labels": [{"name": "type:epic"}, {"name": "status:done"}],
    }
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {91: child_snapshot(91), 92: child_snapshot(92)}
    )
    monkeypatch.setattr(
        uis, "status_of", lambda record: "Backlog" if record.get("state") == "OPEN" else "Done"
    )
    set_status_calls = []
    monkeypatch.setattr(
        uis,
        "set_status",
        lambda number, status, cwd=None: set_status_calls.append((number, status)),
    )
    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(uis, "run", fake_run)
    # The Project card silently stays on Backlog even though the issue closed
    project_reads = iter(["Backlog", "Backlog", "Backlog"])
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: next(project_reads))
    # readbacks: 1=evidence, 2=settle check, 3=rollback reopen check, 4=rollback settle check
    readbacks = iter([epic, final, final, epic])
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(readbacks))

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert ["gh", "issue", "reopen", "100"] in run_calls
    assert (100, "Backlog") in set_status_calls


def _ok_result():
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


@pytest.mark.parametrize(
    ("remote_state_after_raise", "expect_reopen"),
    [("OPEN", False), ("CLOSED", True)],
)
def test_epic_rollback_reopen_decision_is_authoritative_not_a_local_flag(
    monkeypatch, remote_state_after_raise, expect_reopen
):
    """Close can raise locally after landing remotely; rollback must reopen
    based on a fresh authoritative read, never a local success flag."""
    import update_issue_status as uis

    epic = epic_record()
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}
    reads = iter([epic, {**epic, "state": remote_state_after_raise}, epic])

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(reads))
    monkeypatch.setattr(
        uis, "child_issue_snapshots", lambda numbers, cwd=None: {n: snapshots[n] for n in numbers}
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis, "status_of", lambda record: "Backlog" if record.get("state") == "OPEN" else "Done"
    )
    set_status_calls = []
    monkeypatch.setattr(
        uis,
        "set_status",
        lambda number, status, cwd=None: set_status_calls.append((number, status)),
    )
    run_calls = []

    def fail_close(argv, **kwargs):
        run_calls.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return _ok_result()

    monkeypatch.setattr(uis, "run", fail_close)
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")

    with pytest.raises(uis.KernelError, match="close failed"):
        uis.apply_epic_reconciliation(100)

    assert (["gh", "issue", "reopen", "100"] in run_calls) is expect_reopen
    assert (100, "Backlog") in set_status_calls


def test_rollback_epic_reconciliation_verifies_settled_state(monkeypatch):
    import update_issue_status as uis

    restored = {"number": 100, "state": "OPEN", "labels": [{"name": "status:backlog"}]}
    run_calls = []

    def fake_run(argv, **kwargs):
        run_calls.append(argv)
        return _ok_result()

    monkeypatch.setattr(uis, "run", fake_run)
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: restored)
    monkeypatch.setattr(uis, "status_of", lambda record: "Backlog")
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Backlog")
    set_status_calls = []
    monkeypatch.setattr(uis, "set_status", lambda n, s, cwd=None: set_status_calls.append((n, s)))

    uis._rollback_epic_reconciliation(
        100,
        original=uis.KernelError("original failure"),
    )

    assert not any(argv[:3] == ["gh", "issue", "reopen"] for argv in run_calls)
    assert set_status_calls == [(100, "Backlog")]


@pytest.mark.parametrize(
    ("fail_mode", "expected_match"),
    [
        ("unsettled", "did not settle"),
        ("command_error", "reopen failed"),
    ],
)
def test_rollback_epic_reconciliation_surfaces_combined_error(
    monkeypatch, fail_mode, expected_match
):
    import update_issue_status as uis

    if fail_mode == "unsettled":
        stuck = {"number": 100, "state": "CLOSED", "labels": [{"name": "status:done"}]}
        monkeypatch.setattr(uis, "run", lambda argv, **kwargs: _ok_result())
        monkeypatch.setattr(uis, "issue", lambda number, cwd=None: stuck)
        monkeypatch.setattr(uis, "status_of", lambda record: "Done")
        monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Done")
        monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    else:
        monkeypatch.setattr(uis, "issue", lambda number, cwd=None: {"number": 100, "state": "CLOSED"})

        def fail_reopen(argv, **kwargs):
            if argv[:3] == ["gh", "issue", "reopen"]:
                raise uis.KernelError("reopen failed")
            return _ok_result()

        monkeypatch.setattr(uis, "run", fail_reopen)

    with pytest.raises(uis.KernelError, match=expected_match) as excinfo:
        uis._rollback_epic_reconciliation(100, original=uis.KernelError("original failure"))
    assert "original failure" in str(excinfo.value)


def test_cli_epic_reconcile_check_and_apply(monkeypatch, capsys):
    import update_issue_status as uis

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
