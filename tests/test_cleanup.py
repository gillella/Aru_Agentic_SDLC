from __future__ import annotations

import subprocess
import sys

import pytest

import cleanup_worktrees
from common import KernelError


def test_parse_worktrees_preserves_records():
    raw = """worktree /repo
HEAD aaaa
branch refs/heads/main

worktree /repo/.worktrees/feat-issue-1-one
HEAD bbbb
branch refs/heads/feat/issue-1-one

"""
    records = cleanup_worktrees.parse_worktrees(raw)
    assert records[0]["branch"] == "refs/heads/main"
    assert records[1]["worktree"].endswith("feat-issue-1-one")


def test_factory_branch_scope_is_narrow():
    assert cleanup_worktrees.FACTORY_BRANCH.search("feat/issue-1-one")
    assert cleanup_worktrees.FACTORY_BRANCH.search("codex/minimal-reset")
    assert not cleanup_worktrees.FACTORY_BRANCH.search("personal/experiment")


@pytest.fixture
def factory(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args, cwd=root):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Cleanup test")
    git("config", "user.email", "cleanup@example.invalid")
    (root / ".gitignore").write_text("*.cache\n")
    git("add", ".gitignore")
    git("commit", "-qm", "base")
    monkeypatch.chdir(root)
    monkeypatch.setattr(cleanup_worktrees, "primary_worktree", lambda: root)
    monkeypatch.setattr(cleanup_worktrees, "pr_for_branch", lambda branch: {
        "state": "MERGED", "mergedAt": "2026-09-11", "headRefOid": git("rev-parse", branch),
    })

    def create(number):
        branch = f"fix/issue-{number}-cleanup"
        path = root / ".worktrees" / branch.replace("/", "-")
        git("worktree", "add", "-q", "-b", branch, str(path))
        return path, branch

    return root, git, create


@pytest.mark.parametrize("reason", [None, "worker still active"])
def test_locked_worktree_is_retained_in_dry_and_real_runs(factory, reason):
    _, git, create = factory
    path, _ = create(1)
    git("worktree", "lock", *(["--reason", reason] if reason else []), str(path))
    dry = cleanup_worktrees.sweep(dry_run=True)
    assert cleanup_worktrees.sweep() == dry
    assert not dry["removed"] and not dry["failed"]
    assert "locked:" in dry["retained"][0]
    assert path.exists()


def test_ignored_data_blocks_removal_and_is_preserved(factory):
    _, _, create = factory
    path, _ = create(1)
    data = path / "unique.cache"
    data.write_text("irreplaceable ignored contents")
    dry = cleanup_worktrees.sweep(dry_run=True)
    assert cleanup_worktrees.sweep() == dry
    assert "ignored files" in dry["retained"][0]
    assert data.read_text() == "irreplaceable ignored contents"


@pytest.mark.parametrize("stage", ["status", "worktree", "branch"])
def test_one_failure_does_not_hide_other_cleanup_or_partial_progress(factory, monkeypatch, stage):
    _, git, create = factory
    first, branch = create(1)
    second, _ = create(2)
    real_git = cleanup_worktrees.git

    def fail_one(args, *, cwd=None):
        target = (stage == "status" and args[0] == "status" and cwd == first
                  or stage == "worktree" and args[:2] == ["worktree", "remove"]
                  and args[-1] == str(first)
                  or stage == "branch" and args[0] == "branch" and args[-1] == branch)
        if target:
            raise KernelError("injected Git failure")
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(cleanup_worktrees, "git", fail_one)
    result = cleanup_worktrees.sweep()
    assert len(result["failed"]) == 1
    assert str(second) in result["removed"] and not second.exists()
    assert git("rev-parse", branch)
    if stage == "branch":
        assert str(first) in result["removed"] and not first.exists()
        assert "worktree already removed" in result["failed"][0]
    else:
        assert str(first) not in result["removed"] and first.exists()


def test_dry_run_keeps_eligible_worktrees_and_local_branches(factory):
    _, git, create = factory
    path, branch = create(1)
    result = cleanup_worktrees.sweep(dry_run=True)
    assert result == {"removed": [str(path)], "retained": [], "failed": []}
    assert path.exists() and git("rev-parse", branch)


@pytest.mark.parametrize("state", ["dirty", "untracked", "open", "head-drift"])
def test_preservation_guards_still_retain_work(factory, monkeypatch, state):
    _, _, create = factory
    path, _ = create(1)
    if state == "dirty":
        (path / ".gitignore").write_text("changed\n")
    elif state == "untracked":
        (path / "new-source.txt").write_text("unpublished")
    else:
        monkeypatch.setattr(cleanup_worktrees, "pr_for_branch", lambda _: {
            "state": "OPEN" if state == "open" else "MERGED",
            "mergedAt": None if state == "open" else "2026-09-11",
            "headRefOid": "wrong-head",
        })
    result = cleanup_worktrees.sweep()
    assert path.exists() and result["retained"] and not result["removed"]


def test_cli_reports_partial_failures_with_nonzero_status(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cleanup_worktrees.py", "--json"])
    monkeypatch.setattr(cleanup_worktrees, "sweep", lambda **_: {
        "removed": ["/removed"], "retained": [], "failed": ["/kept: inspect: failed"],
    })
    assert cleanup_worktrees.main() == 1
    assert '"failed"' in capsys.readouterr().out
