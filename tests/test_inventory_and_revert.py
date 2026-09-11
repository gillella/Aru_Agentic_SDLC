from __future__ import annotations

import os
import subprocess

import pytest

import common
import revert_merge


def test_issue_inventory_filters_labels_locally_and_refuses_truncation(monkeypatch):
    seen = []
    records = [{"number": 1, "labels": [{"name": "status:backlog"}]}, {"number": 2, "labels": []}]
    monkeypatch.setattr(common, "gh_json", lambda args, **_kwargs: seen.append(args) or records)
    assert [record["number"] for record in common.list_issues(label="status:backlog")] == [1]
    assert "--label" not in seen[0]  # gh sends label filters through the lagging search index
    monkeypatch.setattr(common, "gh_json", lambda _args, **_kwargs: [{"number": n, "labels": []} for n in range(1000)])
    with pytest.raises(common.KernelError, match="truncated"):
        common.list_issues()


def test_revert_opens_its_pr_from_the_new_worktree(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(revert_merge, "merged_pr", lambda _n: {"title": "t", "mergeCommit": {"oid": "c" * 40}})
    monkeypatch.setattr(revert_merge, "create_worktree", lambda *_args: {"path": str(tmp_path), "branch": "fix/issue-9-r"})
    monkeypatch.setattr(revert_merge, "git", lambda *_args, **_kwargs: "p1 p2")
    monkeypatch.setattr(revert_merge, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""))

    def create(*_args):
        seen["cwd"] = os.getcwd()
        return {"pr": 5, "head": "d" * 40}

    monkeypatch.setattr(revert_merge, "create", create)
    before = os.getcwd()
    assert revert_merge.create_revert(3, 9, "codex-1")["revert_pr"] == 5
    assert seen["cwd"] == os.path.realpath(tmp_path)
    assert os.getcwd() == before
