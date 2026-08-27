from __future__ import annotations

import cleanup_worktrees


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
