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


def _init_clone(root: Path, default_branch: str = "main") -> Path:
    seed = root / "seed"
    origin = root / "origin.git"
    clone = root / "clone"
    seed.mkdir()
    _git(seed, "init", "-b", default_branch)
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


class GitdirTargetTests(unittest.TestCase):
    def test_relative_gitdir_resolves_against_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker_dir = root / "linked"
            marker_dir.mkdir()
            target = root / "repo" / ".git" / "worktrees" / "linked"
            target.mkdir(parents=True)
            (marker_dir / ".git").write_text(
                "gitdir: ../repo/.git/worktrees/linked\n"
            )
            resolved = cleanup_worktrees.gitdir_target(str(marker_dir))
            self.assertEqual(resolved, os.path.realpath(target))


class PorcelainPruneTests(unittest.TestCase):
    def test_empty_status_is_clean(self):
        self.assertFalse(cleanup_worktrees.porcelain_blocks_prune(""))

    def test_untracked_and_tracked_changes_block(self):
        self.assertTrue(cleanup_worktrees.porcelain_blocks_prune("?? scratch.txt\n"))
        self.assertTrue(cleanup_worktrees.porcelain_blocks_prune(" M scripts/tool.py\n"))

    def test_known_cache_ignored_paths_do_not_block(self):
        status = (
            "!! tests/__pycache__/mod.cpython-314.pyc\n"
            "!! .pytest_cache/\n"
            "!! .ruff_cache/CACHEDIR.TAG\n"
        )
        self.assertFalse(cleanup_worktrees.porcelain_blocks_prune(status))

    def test_ignored_non_cache_path_blocks(self):
        self.assertTrue(cleanup_worktrees.porcelain_blocks_prune("!! .env\n"))


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

    def _commit_on(self, path: Path, name: str) -> None:
        (path / name).write_text(f"{name}\n")
        _git(path, "add", name)
        _git(path, "commit", "-m", name)

    def test_unpushed_worktree_is_kept(self):
        path = _add_worktree(self.clone, "feat/issue-9-local")
        self._commit_on(path, "feature.txt")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("in-flight", message)

    def test_remote_deleted_unmerged_worktree_is_kept(self):
        path = _add_worktree(self.clone, "feat/issue-9-gone")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-9-gone")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-9-gone")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("in-flight", message)

    def test_unpushed_clean_worktree_at_default_is_kept(self):
        path = _add_worktree(self.clone, "feat/issue-10-empty")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("in-flight", message)

    def test_ls_remote_failure_keeps_merged_worktree(self):
        path = _add_worktree(self.clone, "feat/issue-12-net")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-12-net")
        _git(self.clone, "merge", "--no-ff", "-m", "merge feature", "feat/issue-12-net")
        _git(self.clone, "push", "origin", "main")
        with patch.object(cleanup_worktrees, "remote_branch_absent", return_value=None):
            ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("in-flight", message)

    def test_merged_worktree_is_removed(self):
        path = _add_worktree(self.clone, "feat/issue-9-merged")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-9-merged")
        _git(self.clone, "merge", "--no-ff", "-m", "merge feature", "feat/issue-9-merged")
        _git(self.clone, "push", "origin", "main")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-9-merged")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        self.assertFalse(path.exists())
        self.assertIn("removed", message)

    def test_dirty_unmerged_worktree_is_kept_as_dirty(self):
        path = _add_worktree(self.clone, "feat/issue-8-dirty")
        (path / "scratch.txt").write_text("keep me\n")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertEqual((path / "scratch.txt").read_text(), "keep me\n")
        listed = [os.path.realpath(item) for item in self._worktree_paths()]
        self.assertIn(os.path.realpath(path), listed)
        self.assertIn("dirty", message)

    def test_dirty_merged_worktree_is_refused(self):
        path = _add_worktree(self.clone, "feat/issue-8-merged-dirty")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-8-merged-dirty")
        _git(self.clone, "merge", "--no-ff", "-m", "merge feature", "feat/issue-8-merged-dirty")
        _git(self.clone, "push", "origin", "main")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-8-merged-dirty")
        (path / "scratch.txt").write_text("keep me\n")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertEqual((path / "scratch.txt").read_text(), "keep me\n")
        self.assertIn("dirty", message)

    def test_ignored_cache_on_merged_worktree_is_pruned(self):
        path = _add_worktree(self.clone, "feat/issue-8-cache")
        (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ruff_cache/\n")
        _git(path, "add", ".gitignore")
        _git(path, "commit", "-m", "ignore caches")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-8-cache")
        _git(self.clone, "merge", "--no-ff", "-m", "merge feature", "feat/issue-8-cache")
        _git(self.clone, "push", "origin", "main")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-8-cache")
        cache = path / "tests" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "test.cpython-314.pyc").write_bytes(b"\0")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        self.assertFalse(path.exists())
        self.assertIn("removed", message)

    def test_stale_origin_main_is_fetched_before_ancestor_check(self):
        path = _add_worktree(self.clone, "feat/issue-8-github-merge")
        self._commit_on(path, "feature.txt")
        _git(self.clone, "push", "-u", "origin", "feat/issue-8-github-merge")
        origin = self.root / "origin.git"
        subprocess.run(
            [
                "git", "--git-dir", str(origin),
                "fetch", str(self.clone),
                "feat/issue-8-github-merge:main",
            ],
            check=True, capture_output=True, text=True,
        )
        _git(self.clone, "push", "origin", "--delete", "feat/issue-8-github-merge")
        before = subprocess.check_output(
            ["git", "rev-parse", "refs/remotes/origin/main"],
            cwd=self.clone, text=True,
        ).strip()
        merged = subprocess.check_output(
            ["git", "--git-dir", str(origin), "rev-parse", "main"],
            text=True,
        ).strip()
        self.assertNotEqual(before, merged)
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("removed", message)

    def test_retained_copy_after_prune_worktree_is_swept(self):
        path = _add_worktree(self.clone, "feat/issue-8-retain")
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
        ok, message = merge_pr.prune_worktree(
            str(self.clone), "feat/issue-8-retain", sha
        )
        self.assertTrue(ok, message)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        retained_root = self.clone / ".worktrees" / ".retained"
        leftovers = [item for item in retained_root.iterdir() if item.is_dir()]
        self.assertTrue(leftovers)
        sweep_ok, sweep_msg = cleanup_worktrees.sweep(
            str(self.clone), include_labels=False
        )
        self.assertTrue(sweep_ok)
        remaining = (
            [item for item in retained_root.iterdir() if item.is_dir()]
            if retained_root.is_dir() else []
        )
        self.assertFalse(remaining)
        self.assertIn("removed retained", sweep_msg)

    def test_dirty_registered_retained_worktree_survives(self):
        retained = self.clone / ".worktrees" / ".retained"
        retained.mkdir(parents=True)
        path = retained / "feat-issue-8-dirty-retained"
        _git(
            self.clone, "worktree", "add", "-b",
            "feat/issue-8-dirty-retained", str(path),
        )
        (path / "scratch.txt").write_text("keep me\n")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        self.assertTrue(path.exists())
        self.assertEqual((path / "scratch.txt").read_text(), "keep me\n")
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("dirty", message)

    def test_already_clean_is_noop(self):
        first = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        second = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertEqual(first, (True, "already clean"))
        self.assertEqual(second, (True, "already clean"))

    def test_merged_local_branch_without_upstream_is_kept(self):
        _git(self.clone, "checkout", "-b", "feat/issue-5-local")
        (self.clone / "extra.txt").write_text("n\n")
        _git(self.clone, "add", "extra.txt")
        _git(self.clone, "commit", "-m", "extra")
        _git(self.clone, "checkout", "main")
        _git(self.clone, "merge", "--ff-only", "feat/issue-5-local")
        _git(self.clone, "push", "origin", "main")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        refs = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=self.clone, text=True,
        )
        self.assertIn("feat/issue-5-local", refs.split())
        self.assertNotIn("deleted local branch feat/issue-5-local", message)

    def test_merged_local_branch_with_deleted_remote_is_deleted(self):
        _git(self.clone, "checkout", "-b", "feat/issue-5-merged")
        (self.clone / "extra.txt").write_text("n\n")
        _git(self.clone, "add", "extra.txt")
        _git(self.clone, "commit", "-m", "extra")
        _git(self.clone, "push", "-u", "origin", "feat/issue-5-merged")
        _git(self.clone, "checkout", "main")
        _git(self.clone, "merge", "--ff-only", "feat/issue-5-merged")
        _git(self.clone, "push", "origin", "main")
        _git(self.clone, "push", "origin", "--delete", "feat/issue-5-merged")
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(ok)
        refs = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=self.clone, text=True,
        )
        self.assertNotIn("feat/issue-5-merged", refs.split())
        self.assertIn("deleted local branch feat/issue-5-merged", message)

    def test_merged_worktree_on_origin_master_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = _init_clone(Path(tmp), default_branch="master")
            path = _add_worktree(clone, "feat/issue-11-master")
            self._commit_on(path, "feature.txt")
            _git(clone, "push", "-u", "origin", "feat/issue-11-master")
            _git(clone, "merge", "--no-ff", "-m", "merge feature", "feat/issue-11-master")
            _git(clone, "push", "origin", "master")
            _git(clone, "push", "origin", "--delete", "feat/issue-11-master")
            ok, message = cleanup_worktrees.sweep(str(clone), include_labels=False)
            self.assertTrue(ok)
            listing = subprocess.check_output(
                ["git", "worktree", "list", "--porcelain"],
                cwd=clone, text=True,
            )
            listed = [
                os.path.realpath(line.split(" ", 1)[1])
                for line in listing.splitlines()
                if line.startswith("worktree ")
            ]
            self.assertNotIn(os.path.realpath(path), listed)
            self.assertIn("removed", message)

    @patch.object(cleanup_worktrees, "merger_claim_still_needed", return_value=False)
    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "PR #3 merger:claims cleared"))
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "PR #3 reviewer:claims cleared"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "Issue #2 agent:claims cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_stale_claim_labels_are_cleared(
        self, gh_json, issue_clear, review_clear, merger_clear, _needed
    ):
        def fake_gh(cmd, cwd=None):
            if cmd[:3] == ["gh", "issue", "list"]:
                return [{"number": 2, "labels": [{"name": "agent:cursor-1"}]}]
            if cmd[:3] == ["gh", "pr", "list"]:
                return [{
                    "number": 3,
                    "labels": [{"name": "reviewer:agent-2"}, {"name": "merger:agent-3"}],
                }]
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        issue_clear.assert_called_once_with(2, cwd=str(self.clone))
        review_clear.assert_called_once_with(3, cwd=str(self.clone))
        merger_clear.assert_called_once_with(3, cwd=str(self.clone))
        self.assertTrue(notes)

    @patch.object(cleanup_worktrees, "merger_claim_still_needed", return_value=True)
    @patch.object(merge_pr, "clear_merger_claims")
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "PR #3 reviewer:claims cleared"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "Issue #2 agent:claims cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_merger_label_kept_when_closeout_incomplete(
        self, gh_json, issue_clear, review_clear, merger_clear, _needed
    ):
        def fake_gh(cmd, cwd=None):
            if cmd[:3] == ["gh", "issue", "list"]:
                return [{"number": 2, "labels": [{"name": "agent:cursor-1"}]}]
            if cmd[:3] == ["gh", "pr", "list"]:
                return [{
                    "number": 3,
                    "labels": [{"name": "reviewer:agent-2"}, {"name": "merger:agent-3"}],
                }]
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        merger_clear.assert_not_called()
        self.assertTrue(any("kept merger claim" in note for note in notes))
        issue_clear.assert_called_once_with(2, cwd=str(self.clone))
        review_clear.assert_called_once_with(3, cwd=str(self.clone))

    @patch.object(merge_pr, "clear_merger_claims")
    @patch.object(merge_pr, "_gh_json")
    def test_merger_claim_kept_when_linked_issue_open(self, gh_json, merger_clear):
        def fake_gh(cmd, cwd=None):
            if cmd[:3] == ["gh", "pr", "list"]:
                return [{"number": 3, "labels": [{"name": "merger:agent-3"}]}]
            if cmd[:3] == ["gh", "pr", "view"]:
                return {
                    "number": 3,
                    "state": "MERGED",
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "headRefName": "feat/issue-3-open",
                    "body": "Closes #2",
                }
            if cmd[:3] == ["gh", "issue", "view"]:
                return {"state": "OPEN", "labels": []}
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        merger_clear.assert_not_called()
        self.assertTrue(any("kept merger claim on PR #3" in item for item in notes))

    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_retain_merger_pr_skips_current_closeout(self, gh_json, merger_clear):
        def fake_gh(cmd, cwd=None):
            if cmd[:3] == ["gh", "pr", "list"]:
                return [{"number": 9, "labels": [{"name": "merger:agent-3"}]}]
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels(
            str(self.clone), retain_merger_pr=9
        )
        merger_clear.assert_not_called()
        self.assertTrue(any("current close-out incomplete" in item for item in notes))

    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_claim_scan_records_page_limit(self, gh_json, _clear):
        full = [
            {"number": i, "labels": [{"name": "agent:x"}]}
            for i in range(1, cleanup_worktrees.CLAIM_LIST_LIMIT + 1)
        ]

        def fake_gh(cmd, cwd=None):
            if cmd[:3] == ["gh", "issue", "list"]:
                self.assertIn(str(cleanup_worktrees.CLAIM_LIST_LIMIT), cmd)
                return full
            return []

        gh_json.side_effect = fake_gh
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertTrue(
            any("closed-issue claim scan hit the page limit" in item for item in notes)
        )

    @patch.object(merge_pr, "_gh_json", return_value=None)
    def test_unreadable_claim_list_is_recorded(self, _gh):
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertTrue(any("claim scan failed" in item for item in notes))

    @patch.object(merge_pr, "_gh_json", return_value=[])
    def test_claim_scan_runs_gh_in_repo_root(self, gh_json):
        notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertTrue(gh_json.call_args_list)
        for call in gh_json.call_args_list:
            self.assertEqual(call.kwargs.get("cwd"), str(self.clone))
        self.assertEqual(notes, [])


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
            janitor.assert_called_once_with("/repo", retain_merger_pr=None)

    def test_failed_closeout_retains_current_merger_claim(self):
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(False, "w")), \
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
            self.assertFalse(merge_pr.run_closeout(pr, [7], "/repo"))
            janitor.assert_called_once_with("/repo", retain_merger_pr=9)


if __name__ == "__main__":
    unittest.main()
