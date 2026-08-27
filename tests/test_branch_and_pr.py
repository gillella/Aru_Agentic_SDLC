from __future__ import annotations

import pytest

import create_branch
import create_pr


def test_reviewer_assignment_is_stable_and_balanced():
    assert [create_pr.reviewer_for_issue(number) for number in range(6)] == [
        "coderabbit",
        "sourcery",
        "codeant",
        "coderabbit",
        "sourcery",
        "codeant",
    ]


def test_create_pr_binds_head_and_one_reviewer(monkeypatch):
    record = {"number": 6, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(
        create_pr,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "url": "https://example/pr/12",
            "headRefOid": "a" * 40,
            "labels": [{"name": "review:coderabbit"}],
        },
    )
    statuses = []
    monkeypatch.setattr(create_pr, "set_status", lambda number, status: statuses.append((number, status)))

    result = create_pr.create(6, "feat: small", "Summary", "codex-1")
    assert result["reviewer"] == "coderabbit"
    assert result["head"] == "a" * 40
    assert statuses == [(6, "In Review")]
    body = commands[0][commands[0].index("--body") + 1]
    assert body.count("Closes #6") == 1


def test_create_pr_rejects_caller_closing_directive(monkeypatch):
    record = {"number": 1, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    with pytest.raises(create_pr.KernelError, match="closing directive"):
        create_pr.create(1, "feat: bad", "Closes #99", "codex-1")


def test_branch_requires_exclusive_claim(monkeypatch):
    monkeypatch.setattr(
        create_branch,
        "issue",
        lambda _number: {
            "number": 5,
            "title": "change",
            "labels": [{"name": "agent:someone-else"}],
        },
    )
    monkeypatch.setattr(create_branch, "status_of", lambda _record: "In Progress")
    with pytest.raises(create_branch.KernelError, match="exclusive"):
        create_branch.create_worktree(5, "fix", "codex-1")
