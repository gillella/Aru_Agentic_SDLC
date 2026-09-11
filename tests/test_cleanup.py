from __future__ import annotations

import sys

import pytest

import cleanup_worktrees
from common import KernelError

HEAD = "b" * 40


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


def fake_repo(monkeypatch, tmp_path, worktrees, *, prs=None, status=None, fail=()):
    """Fake git and GitHub for sweep(); `worktrees` maps a name to extra porcelain lines."""
    root = tmp_path.resolve()
    prs, status = prs or {}, status or {}
    blocks = [f"worktree {root}\nHEAD {'a' * 40}\nbranch refs/heads/main"]
    for name, extra in worktrees.items():
        blocks.append("\n".join([f"worktree {root}/.worktrees/{name}", f"HEAD {HEAD}",
                                 f"branch refs/heads/feat/issue-1-{name}", *extra]))
    calls = []

    def git(args, cwd=None):
        calls.append(args)
        if args[:2] == ["worktree", "list"]:
            return "\n\n".join(blocks) + "\n"
        if args[0] == "status":
            return status.get(str(cwd).rsplit("/", 1)[-1], "")
        return HEAD if args[:2] == ["rev-parse", "HEAD"] else ""

    def pr_for_branch(branch):
        name = branch.removeprefix("feat/issue-1-")
        if name in fail:
            raise KernelError("ambiguous pull-request history")
        return prs.get(name, {"state": "MERGED", "mergedAt": "now", "headRefOid": HEAD})

    monkeypatch.setattr(cleanup_worktrees, "git", git)
    monkeypatch.setattr(cleanup_worktrees, "pr_for_branch", pr_for_branch)
    monkeypatch.setattr(cleanup_worktrees, "primary_worktree", lambda: root)
    monkeypatch.chdir(root)
    return root, calls


def removals(calls):
    return [args for args in calls if args[:2] == ["worktree", "remove"] or args[:1] in (["branch"], ["push"])]


@pytest.mark.parametrize("dry_run", [True, False])
def test_locked_worktree_is_retained_the_same_in_dry_and_real_runs(monkeypatch, tmp_path, dry_run):
    root, calls = fake_repo(monkeypatch, tmp_path, {"busy": ["locked m1@mini"], "idle": []})
    result = cleanup_worktrees.sweep(dry_run=dry_run)
    assert result == {"removed": [f"{root}/.worktrees/idle"],
                      "retained": [f"{root}/.worktrees/busy: locked (m1@mini)"], "failed": []}
    assert not any(str(args[-1]).endswith("busy") for args in removals(calls))
    assert bool(removals(calls)) is (not dry_run)


def test_one_failed_worktree_does_not_stop_the_sweep(monkeypatch, tmp_path, capsys):
    root, _ = fake_repo(monkeypatch, tmp_path, {"bad": [], "good": []}, fail={"bad"})
    monkeypatch.setattr(sys, "argv", ["cleanup_worktrees.py"])
    assert cleanup_worktrees.main() == 1
    out = capsys.readouterr().out
    assert f"failed {root}/.worktrees/bad: inspect: ambiguous pull-request history" in out
    assert f"removed {root}/.worktrees/good" in out


def test_ignored_data_is_kept_but_disposable_caches_are_not(monkeypatch, tmp_path):
    root, _ = fake_repo(monkeypatch, tmp_path, {"data": [], "caches": []}, status={
        "data": "!! .env\n!! .venv/\n!! __pycache__/\n",
        "caches": "!! __pycache__/\n!! src/.pytest_cache/\n!! build/module.pyc\n",
    })
    result = cleanup_worktrees.sweep()
    assert result["retained"] == [f"{root}/.worktrees/data: ignored data .env, .venv"]
    assert result["removed"] == [f"{root}/.worktrees/caches"]


@pytest.mark.parametrize("case,extra,expected", [
    ("dirty", [], "dirty"),
    ("open", [], "PR open or absent"),
    ("moved", [], "head differs from preserved PR"),
    ("gone", ["prunable gitdir file points to non-existent location"],
     "directory missing; inspect, then git worktree prune"),
])
def test_unsafe_worktrees_are_still_retained_and_nothing_is_deleted(monkeypatch, tmp_path, case, extra, expected):
    root, calls = fake_repo(
        monkeypatch, tmp_path, {case: extra}, status={"dirty": "?? notes.txt\n"},
        prs={"open": {"state": "OPEN", "mergedAt": None, "headRefOid": HEAD},
             "moved": {"state": "MERGED", "mergedAt": "now", "headRefOid": "c" * 40}},
    )
    result = cleanup_worktrees.sweep()
    assert result == {"removed": [], "retained": [f"{root}/.worktrees/{case}: {expected}"], "failed": []}
    assert removals(calls) == []


def test_branch_deletion_failure_still_reports_the_completed_removal(monkeypatch, tmp_path):
    root, calls = fake_repo(monkeypatch, tmp_path, {"merged": []})
    recorded = cleanup_worktrees.git

    def git(args, cwd=None):
        output = recorded(args, cwd=cwd)
        if args[:2] == ["branch", "-d"]:
            raise KernelError("not fully merged")
        return output

    monkeypatch.setattr(cleanup_worktrees, "git", git)
    result = cleanup_worktrees.sweep()
    assert result["removed"] == [f"{root}/.worktrees/merged"]
    assert result["failed"] == [
        f"{root}/.worktrees/merged: worktree removed, local branch feat/issue-1-merged kept: not fully merged"]
    assert [args[:2] for args in calls if args[0] in {"worktree", "branch"} and args[1] != "list"] == [
        ["worktree", "remove"], ["branch", "-d"]]
