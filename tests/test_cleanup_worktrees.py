import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cleanup_worktrees
import merge_pr


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_clone(root: Path) -> Path:
    seed = root / "seed"
    origin = root / "origin.git"
    clone = root / "clone"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "janitor@example.com")
    _git(seed, "config", "user.name", "Janitor")
    (seed / "README").write_text("main\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "init")
    _git(seed, "clone", "--bare", str(seed), str(origin))
    _git(root, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "janitor@example.com")
    _git(clone, "config", "user.name", "Janitor")
    return clone


def _add_worktree(clone: Path, branch: str) -> Path:
    path = clone / ".worktrees" / branch.replace("/", "-")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(clone, "worktree", "add", "-b", branch, str(path))
    return path


class CleanupWorktreesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clone = _init_clone(self.root)
        self._gh_patch = patch.object(merge_pr, "_gh_json", return_value=[])
        self._gh_patch.start()
        self.addCleanup(self._gh_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def _worktree_paths(self):
        listing = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.clone, text=True,
        )
        return [
            os.path.realpath(line.split(" ", 1)[1])
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]

    def test_clean_orphan_worktree_is_removed(self):
        path = _add_worktree(self.clone, "feat/issue-9-gone")
        _git(self.clone, "push", "-u", "origin", "feat/issue-9-gone")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-9-gone")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        self.assertFalse(path.exists())
        self.assertIn("removed", message)

    def test_dirty_worktree_is_refused(self):
        path = _add_worktree(self.clone, "feat/issue-8-dirty")
        (path / "scratch.txt").write_text("keep me\n")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertEqual((path / "scratch.txt").read_text(), "keep me\n")
        listed = [os.path.realpath(item) for item in self._worktree_paths()]
        self.assertIn(os.path.realpath(path), listed)
        self.assertIn("dirty", message)

    def test_already_clean_is_noop(self):
        first = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        second = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertEqual(first, (True, "already clean"))
        self.assertEqual(second, (True, "already clean"))

    def test_merged_local_branch_without_worktree_is_deleted(self):
        _git(self.clone, "checkout", "-b", "feat/issue-5-merged")
        (self.clone / "extra.txt").write_text("n\n")
        _git(self.clone, "add", "extra.txt")
        _git(self.clone, "commit", "-m", "extra")
        _git(self.clone, "checkout", "main")
        _git(self.clone, "merge", "--ff-only", "feat/issue-5-merged")
        _git(self.clone, "push", "origin", "main")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        refs = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=self.clone, text=True,
        )
        self.assertNotIn("feat/issue-5-merged", refs.split())
        self.assertIn("deleted local branch feat/issue-5-merged", message)

    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "PR #3 merger:claims cleared"))
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "PR #3 reviewer:claims cleared"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "Issue #2 agent:claims cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_stale_claim_labels_are_cleared(self, gh_json, issue_clear, review_clear, merger_clear):
        def fake_gh(cmd):
            if cmd[:3] == ["gh", "issue", "list"]:
                return [{"number": 2, "labels": [{"name": "agent:cursor-1"}]}]
            if cmd[:3] == ["gh", "pr", "list"]:
                return [{
                    "number": 3,
                    "labels": [{"name": "reviewer:agent-2"}, {"name": "merger:agent-3"}],
                }]
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels()
        issue_clear.assert_called_once_with(2)
        review_clear.assert_called_once_with(3)
        merger_clear.assert_called_once_with(3)
        self.assertTrue(notes)


class CloseoutJanitorHookTests(unittest.TestCase):
    def test_run_closeout_calls_sweep_leftovers(self):
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(True, "w")), \
             patch.object(merge_pr, "retain_local_branch", return_value=(True, "l")), \
             patch.object(merge_pr, "delete_remote_branch", return_value=(True, "r")), \
             patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "c")), \
             patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "d")), \
             patch.object(merge_pr, "clear_issue_claims", return_value=(True, "i")), \
             patch.object(merge_pr, "clear_review_claims", return_value=(True, "v")), \
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")), \
             patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor")) as janitor:
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertTrue(merge_pr.run_closeout(pr, [7], "/repo"))
            janitor.assert_called_once_with("/repo")


if __name__ == "__main__":
    unittest.main()
