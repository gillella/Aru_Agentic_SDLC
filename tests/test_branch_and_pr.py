from __future__ import annotations

import pytest

import create_branch
import create_pr


def test_local_changed_paths_include_both_rename_sides(monkeypatch):
    monkeypatch.setattr(create_pr, "default_branch_name", lambda: "develop")
    monkeypatch.setattr(
        create_pr,
        "git",
        lambda argv: (
            "R100\thooks/old.py\tsrc/new.py\nM\tREADME.md"
            if argv[-1] == "origin/develop...HEAD"
            else pytest.fail(argv)
        ),
    )
    assert create_pr.local_changed_paths() == [
        "README.md",
        "hooks/old.py",
        "src/new.py",
    ]


def test_create_pr_revalidates_ownership_before_opening_the_pr(monkeypatch):
    records = iter(
        {"number": 6, "labels": [{"name": "status:in-progress"}, {"name": f"agent:{owner}"}]}
        for owner in ("codex-1", "another-agent")
    )
    monkeypatch.setattr(create_pr, "issue", lambda _number: next(records))
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["scripts/create_pr.py"])
    monkeypatch.setattr(
        create_pr,
        "run",
        lambda _argv: pytest.fail("stale owner must not create a pull request"),
    )
    with pytest.raises(create_pr.KernelError, match="ownership changed"):
        create_pr.create(
            6,
            "feat: small",
            "## Summary\n\nChange\n\n## Verification\n\nFocused checks passed.",
            "codex-1",
        )


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
