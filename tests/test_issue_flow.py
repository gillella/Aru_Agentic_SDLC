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


@pytest.mark.parametrize(
    ("label", "diagnostic"),
    [
        ("needs-human", "needs-human issues cannot enter Ready"),
        ("type:epic", "type:epic issues cannot enter Ready"),
    ],
)
def test_triage_rejects_non_executable_work(label, diagnostic):
    assert triage_backlog.evaluate(backlog_issue(label)) == [diagnostic]


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
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {n: snapshots[n] for n in numbers},
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
    )
    monkeypatch.setattr(
        uis,
        "set_status",
        lambda number, status, cwd=None: calls.__setitem__("set_status", calls["set_status"] + 1),
    )
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Done")

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
    ("labels", "policy", "children", "snapshots", "depends", "blocker"),
    [
        ([], "children-only", None, None, [], "issue is not type:epic"),
        (["type:epic", "needs-human"], "children-only", None, None, [], "needs-human"),
        (["type:epic"], "manual", None, None, [], "epic-close-policy: manual"),
        (["type:epic"], None, None, None, [], "epic-close-policy is missing"),
        (["type:epic"], "children-only", [91], {}, [], "child #91 is missing"),
        (
            ["type:epic"],
            "children-only",
            [91],
            {91: child_snapshot(91, state="OPEN")},
            [],
            "child #91 is not closed",
        ),
        (
            ["type:epic"],
            "children-only",
            [91],
            {91: child_snapshot(91, status="ready")},
            [],
            "child #91 is not status:done",
        ),
        (
            ["type:epic"],
            "children-only",
            [91],
            {91: child_snapshot(91, repository="other/repo")},
            [],
            "child #91 is not in this repository",
        ),
        (["type:epic"], "children-only", None, None, [90], "open depends-on: #90"),
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
    monkeypatch.setattr(
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: snapshots or {},
    )

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
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {n: snapshots[n] for n in numbers},
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "Backlog" if record is epic else "Done",
    )
    monkeypatch.setattr(
        uis,
        "set_status",
        lambda *args, **kwargs: mutations.append(("set_status", args)),
    )
    monkeypatch.setattr(
        uis,
        "run",
        lambda *args, **kwargs: mutations.append(("run", args)),
    )

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["closable"] is True
    assert evidence["blocked"] is False
    assert mutations == []


def test_epic_reconcile_malformed_and_duplicate_children_block():
    import update_issue_status as uis

    with pytest.raises(uis.KernelError, match="malformed"):
        uis.parse_child_issues("## Child Issues\n- not-a-ref\n")

    with pytest.raises(uis.KernelError, match="duplicate"):
        uis.parse_child_issues("## Child Issues\n- #1\n- #1\n")


def test_epic_reconcile_duplicate_child_issues_section_blocks():
    import update_issue_status as uis

    body = (
        "## Child Issues\n- #1\n\n"
        "## Child Issues\n- #2\n\n"
        "epic-close-policy: children-only\n"
    )
    with pytest.raises(uis.KernelError, match="more than one ## Child Issues section"):
        uis.parse_child_issues(body)


def test_epic_reconcile_ambiguous_policy_blocks(monkeypatch):
    import update_issue_status as uis

    body = "## Child Issues\n- #91\n\nepic-close-policy: children-only\nepic-close-policy: manual\n"
    epic = {"number": 100, "body": body, "state": "OPEN", "labels": [{"name": "type:epic"}]}
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])

    evidence = uis.epic_reconcile_evidence(100)
    assert "epic-close-policy is ambiguous" in evidence["blockers"]


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
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {
            91: {
                "number": 91,
                "state": "CLOSED",
                "repository": "owner/repo",
                "labels": [{"name": "status:done"}],
            }
        },
    )

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
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {n: snapshots[n] for n in numbers},
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "Backlog" if record is epic else "Done",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)

    def fail_close(argv, **kwargs):
        commands.append(argv)
        if argv[:3] == ["gh", "issue", "close"]:
            raise uis.KernelError("close failed")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(uis, "run", fail_close)
    monkeypatch.setattr(
        uis,
        "_rollback_epic_reconciliation",
        lambda *args, **kwargs: commands.append(["rollback"]),
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
        uis.epic_close_policy(
            "epic-close-policy: children-only\nepic-close-policy: manual\n"
        )


def test_parse_child_issues_requires_bounded_unique_references():
    import update_issue_status as uis

    body = "## Child Issues\n- #9\n- #2\n"
    assert uis.parse_child_issues(body) == [2, 9]
    with pytest.raises(uis.KernelError, match="malformed"):
        uis.parse_child_issues("## Child Issues\n- other/repo#9\n")
    with pytest.raises(uis.KernelError, match="duplicate"):
        uis.parse_child_issues("## Child Issues\n- #9\n- #9\n")


def test_child_issue_snapshots_use_one_repository_graphql_query(monkeypatch):
    import update_issue_status as uis

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "i91": {
                        "number": 91,
                        "state": "CLOSED",
                        "repository": {"nameWithOwner": "owner/repo"},
                        "labels": {
                            "nodes": [{"name": "status:done"}],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                    "i92": {
                        "number": 92,
                        "state": "CLOSED",
                        "repository": {"nameWithOwner": "owner/repo"},
                        "labels": {
                            "nodes": [{"name": "status:done"}],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                }
            }
        }

    monkeypatch.setattr(uis, "gh_json", fake_gh_json)
    snapshots = uis.child_issue_snapshots([91, 92])
    assert set(snapshots) == {91, 92}
    assert len(calls) == 1
    assert calls[0][1] == uis.REPOSITORY_AUTH
    query = " ".join(calls[0][0])
    assert "i91: issue(number: 91)" in query
    assert "i92: issue(number: 92)" in query


def test_epic_reconcile_evidence_reports_exact_child_state(monkeypatch):
    import update_issue_status as uis

    epic = {
        "number": 100,
        "body": "## Child Issues\n- #91\n\nepic-close-policy: children-only\n",
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:backlog"}],
    }
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {
            91: {
                "number": 91,
                "state": "CLOSED",
                "repository": "owner/repo",
                "labels": [{"name": "status:done"}],
            }
        },
    )

    evidence = uis.epic_reconcile_evidence(100)
    assert evidence["closable"] is True
    assert evidence["children"]["91"]["status"] == "Done"


def test_apply_epic_reconciliation_read_back_fails_closed(monkeypatch):
    import update_issue_status as uis

    epic = {
        "number": 100,
        "body": "## Child Issues\n- #91\n\nepic-close-policy: children-only\n",
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:backlog"}],
    }
    final = {
        **epic,
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:done"}],
    }
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {
            91: {
                "number": 91,
                "state": "CLOSED",
                "repository": "owner/repo",
                "labels": [{"name": "status:done"}],
            }
        },
    )
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "Backlog" if record.get("state") == "OPEN" else "Done",
    )
    monkeypatch.setattr(uis, "set_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "Done")
    readbacks = iter([epic, final])
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(readbacks))
    rollback = []
    monkeypatch.setattr(
        uis,
        "_rollback_epic_reconciliation",
        lambda *args, **kwargs: rollback.append(kwargs),
    )

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert rollback and rollback[0]["closed_issue"] is True


def test_epic_rollback_reopens_and_reverts_status_when_close_settled_but_card_did_not(
    monkeypatch,
):
    """Exercise the real rollback function end-to-end, not a stub."""
    import update_issue_status as uis

    epic = {
        "number": 100,
        "body": "## Child Issues\n- #91\n\nepic-close-policy: children-only\n",
        "state": "OPEN",
        "labels": [{"name": "type:epic"}, {"name": "status:in-review"}],
    }
    final = {
        **epic,
        "state": "CLOSED",
        "labels": [{"name": "type:epic"}, {"name": "status:done"}],
    }
    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {
            91: {
                "number": 91,
                "state": "CLOSED",
                "repository": "owner/repo",
                "labels": [{"name": "status:done"}],
            }
        },
    )
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "In Review" if record.get("state") == "OPEN" else "Done",
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
    # The Project card silently stays on the pre-mutation option even though
    # the issue itself closed; the real rollback must still be triggered.
    monkeypatch.setattr(uis, "project_item_status", lambda number, cwd=None: "In Review")
    readbacks = iter([epic, final])
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: next(readbacks))

    with pytest.raises(uis.KernelError, match="did not settle"):
        uis.apply_epic_reconciliation(100)

    assert ["gh", "issue", "reopen", "100"] in run_calls
    assert (100, "In Review") in set_status_calls


def test_epic_rollback_does_not_reopen_when_close_never_succeeded(monkeypatch):
    """Exercise the real rollback function when the close mutation itself failed."""
    import update_issue_status as uis

    epic = epic_record(labels=["type:epic", "status:in-review"])
    snapshots = {91: child_snapshot(91), 92: child_snapshot(92)}

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "issue", lambda number, cwd=None: epic)
    monkeypatch.setattr(
        uis,
        "child_issue_snapshots",
        lambda numbers, cwd=None: {n: snapshots[n] for n in numbers},
    )
    monkeypatch.setattr(uis, "unresolved_dependencies", lambda record, cwd=None: [])
    monkeypatch.setattr(
        uis,
        "status_of",
        lambda record: "In Review" if record is epic else "Done",
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
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(uis, "run", fail_close)

    with pytest.raises(uis.KernelError, match="close failed"):
        uis.apply_epic_reconciliation(100)

    assert not any(argv[:3] == ["gh", "issue", "reopen"] for argv in run_calls)
    # set_status is called once to move to Done, and again by rollback to
    # revert to the pre-mutation status.
    assert set_status_calls == [(100, "Done"), (100, "In Review")]
