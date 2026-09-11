from __future__ import annotations

import pytest

import create_pr

HEAD = "a" * 40


def install_creation(monkeypatch, *, number=6, created_head=HEAD):
    """Fake one In Progress claim, a published branch and the created-PR rereads."""
    lifecycle = ["In Progress"]
    commands, statuses = [], []
    monkeypatch.setattr(create_pr, "issue", lambda _n: {"number": number, "labels": [{"name": "agent:codex-1"}]})
    monkeypatch.setattr(create_pr, "status_of", lambda _record: lifecycle[0])
    monkeypatch.setattr(create_pr, "current_branch", lambda: f"feat/issue-{number}-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: HEAD)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["scripts/create_pr.py"])
    monkeypatch.setattr(create_pr, "run", commands.append)
    snapshots = iter([
        {"number": 12, "url": "https://example/pr/12", "state": "OPEN", "headRefOid": created_head},
        {"number": 12, "state": "CLOSED"},
    ])
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: next(snapshots))

    def transition(issue_number, status, **kwargs):
        kwargs["pre_mutation_check"]()
        statuses.append((issue_number, status, kwargs["expected_current"]))
        lifecycle[0] = status

    monkeypatch.setattr(create_pr, "set_status", transition)
    return commands, statuses


def test_create_pr_binds_the_published_head_and_moves_the_issue_to_review(monkeypatch):
    commands, statuses = install_creation(monkeypatch)
    outcome = create_pr.create(6, "feat: small", "## Summary\n\nSmall change", "codex-1")
    assert outcome == {
        "pr": 12, "url": "https://example/pr/12", "head": HEAD,
        "next_action": "await-approval-by-another-account",
    }
    assert statuses == [(6, "In Review", "In Progress")]
    [argv] = commands
    assert argv[argv.index("--body") + 1].count("Closes #6") == 1
    # No review labels or reviewer routing: GitHub's approval is the whole review state.
    assert "--label" not in argv and "--reviewer" not in argv


def test_create_pr_rejects_caller_closing_directive(monkeypatch):
    commands, _ = install_creation(monkeypatch, number=1)
    with pytest.raises(create_pr.KernelError, match="closing directive"):
        create_pr.create(1, "feat: bad", "Closes #99", "codex-1")
    assert commands == []


def test_create_pr_refuses_a_branch_that_belongs_to_another_issue(monkeypatch):
    commands, _ = install_creation(monkeypatch)
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-60-other-change")
    with pytest.raises(create_pr.KernelError, match="does not belong to the issue"):
        create_pr.create(6, "feat: small", "Summary", "codex-1")
    assert commands == []


def test_create_pr_closes_new_pr_when_post_create_head_is_wrong(monkeypatch):
    commands, statuses = install_creation(monkeypatch, number=1, created_head="b" * 40)
    with pytest.raises(create_pr.KernelError, match="published head"):
        create_pr.create(1, "docs: clarify", "Summary only", "codex-1")
    assert commands[-1][:4] == ["gh", "pr", "close", "12"]
    assert statuses == []
