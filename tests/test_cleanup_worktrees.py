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


def _claim_gh(cmd, cwd=None, *, issues=None, pulls=None, extra=None):
    if cmd[:3] == ["gh", "repo", "view"]:
        return {"nameWithOwner": "o/r"}
    if cmd[:3] == ["gh", "api", "--paginate"]:
        path = cmd[3] if len(cmd) > 3 else ""
        if "/issues?" in path:
            return issues if issues is not None else []
        if "/pulls?" in path:
            return pulls if pulls is not None else []
    if extra:
        return extra(cmd)
    return []


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

    def test_empty_ignored_directory_does_not_block_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / ".worktrees"
            empty_dir.mkdir()
            status = "!! .worktrees/\n"
            self.assertFalse(cleanup_worktrees.porcelain_blocks_prune(status, base_path=tmp))

    def test_non_empty_ignored_directory_blocks_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            non_empty_dir = Path(tmp) / ".worktrees"
            non_empty_dir.mkdir()
            (non_empty_dir / "child.txt").write_text("content")
            status = "!! .worktrees/\n"
            self.assertTrue(cleanup_worktrees.porcelain_blocks_prune(status, base_path=tmp))

    def test_retain_manifest_lines_are_ignored(self):
        status = "?? .aru-retained-clean\n!! .aru-retained-clean.tmp\n"
        self.assertFalse(cleanup_worktrees.porcelain_dirty_except_manifest(status))
        self.assertTrue(
            cleanup_worktrees.porcelain_dirty_except_manifest("?? secret.txt\n")
        )
        self.assertIsNone(cleanup_worktrees.porcelain_dirty_except_manifest(None))


class WorktreeCleanupTests(unittest.TestCase):
    def test_empty_ignored_directory_does_not_block_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / ".worktrees"
            empty_dir.mkdir()
            status = "!! .worktrees/\n"
            self.assertFalse(cleanup_worktrees.porcelain_blocks_prune(status, base_path=tmp))

    def test_non_empty_ignored_directory_blocks_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            non_empty_dir = Path(tmp) / ".worktrees"
            non_empty_dir.mkdir()
            (non_empty_dir / "child.txt").write_text("content")
            status = "!! .worktrees/\n"
            self.assertTrue(cleanup_worktrees.porcelain_blocks_prune(status, base_path=tmp))

    def test_unreadable_nested_dir_fails_closed_and_blocks_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / ".worktrees"
            d.mkdir()
            sub = d / "unreadable"
            sub.mkdir()
            try:
                os.chmod(sub, 0o000)
            except OSError:
                self.skipTest("chmod 000 not supported in this environment")
            try:
                status = "!! .worktrees/\n"
                if not os.access(sub, os.R_OK):
                    self.assertTrue(cleanup_worktrees.porcelain_blocks_prune(status, base_path=tmp))
            finally:
                try:
                    os.chmod(sub, 0o755)
                except OSError:
                    pass

    def test_legacy_retained_copy_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = Path(tmp) / "legacy_wt"
            legacy_dir.mkdir()
            clean, note = cleanup_worktrees._retained_still_clean(str(legacy_dir), deregistered=True)
            self.assertFalse(clean)
            self.assertIn("cleanliness unverifiable", note)

    def test_legacy_retained_copies_do_not_fail_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_clone(Path(tmp))
            retained = repo / ".worktrees" / ".retained"
            retained.mkdir(parents=True)
            legacy_wt = retained / "12345678-feat-old"
            legacy_wt.mkdir()
            (legacy_wt / "README").write_text("old\n")
            (legacy_wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/nonexistent\n")
            ok, notes = cleanup_worktrees.prune_retained_copies(str(repo))
            self.assertTrue(ok)
            self.assertTrue(any("cleanliness unverifiable" in n for n in notes))

    def test_report_retained_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_clone(Path(tmp))
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            retained = repo / ".worktrees" / ".retained"
            retained.mkdir(parents=True)
            legacy_wt = retained / f"{sha[:12]}-feat-old"
            legacy_wt.mkdir()
            (legacy_wt / "README").write_text("main\n")
            (legacy_wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/nonexistent\n")
            stats, summary = cleanup_worktrees.report_retained(str(repo))
            self.assertEqual(stats["total_count"], 1)
            self.assertEqual(stats["legacy_count"], 1)
            self.assertEqual(stats["manifest_count"], 0)
            self.assertIn("Retained worktrees: 1 total", summary)
            self.assertGreater(stats["reclaimable_bytes"], 0)

    def test_legacy_copy_with_modifications_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_clone(Path(tmp))
            old_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            # Advance main with new commit so main != old_sha
            (repo / "new_on_main.txt").write_text("main advanced\n")
            _git(repo, "add", "new_on_main.txt")
            _git(repo, "commit", "-m", "advance main")

            retained = repo / ".worktrees" / ".retained"
            retained.mkdir(parents=True)
            dirty_wt = retained / f"{old_sha[:12]}-feat-dirty"
            dirty_wt.mkdir()
            (dirty_wt / "README").write_text("main\n")
            (dirty_wt / "uncommitted.txt").write_text("dirty work\n")
            (dirty_wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/nonexistent\n")

            clean_wt = retained / f"{old_sha[:12]}-feat-clean"
            clean_wt.mkdir()
            (clean_wt / "README").write_text("main\n")
            (clean_wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/nonexistent\n")

            ok, notes = cleanup_worktrees.purge_legacy_retained(str(repo))
            self.assertTrue(ok)
            self.assertTrue(dirty_wt.exists())
            self.assertFalse(clean_wt.exists())

    def test_aborted_legacy_claim_restores_original_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _init_clone(Path(tmp))
            old_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            retained = repo / ".worktrees" / ".retained"
            retained.mkdir(parents=True)

            wt = retained / f"{old_sha[:12]}-feat-restore"
            wt.mkdir()
            (wt / "README").write_text("main\n")
            (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/nonexistent\n")

            with patch.object(cleanup_worktrees, "_remove_claimed_retained", return_value=(False, "simulated failure")):
                with patch.object(cleanup_worktrees, "retain_manifest_payload", side_effect=OSError("snapshot error")):
                    ok, notes = cleanup_worktrees.purge_legacy_retained(str(repo))
                    self.assertFalse(ok)
                    self.assertTrue(wt.exists())




class RetainManifestWalkTests(unittest.TestCase):
    def test_symlink_to_device_is_recorded_without_following(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zero").symlink_to("/dev/zero")
            payload = cleanup_worktrees.retain_manifest_payload(str(root))
            self.assertEqual(
                payload["entries"]["zero"],
                {"type": "symlink", "target": "/dev/zero"},
            )

    def test_external_symlink_and_empty_dir_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.txt"
            outside.write_text("secret\n")
            tree = Path(tmp) / "tree"
            tree.mkdir()
            (tree / "alias").symlink_to(outside)
            (tree / "empty").mkdir()
            hidden = Path(tmp) / "realdir"
            hidden.mkdir()
            (hidden / "nested.txt").write_text("hidden\n")
            (tree / "dirlink").symlink_to(hidden)
            payload = cleanup_worktrees.retain_manifest_payload(str(tree))
            entries = payload["entries"]
            self.assertEqual(entries["alias"]["type"], "symlink")
            self.assertEqual(entries["alias"]["target"], str(outside))
            self.assertEqual(entries["empty"]["type"], "dir")
            self.assertEqual(entries["dirlink"]["type"], "symlink")
            self.assertNotIn("dirlink/nested.txt", entries)
            outside.write_text("changed\n")
            (hidden / "nested.txt").write_text("changed\n")
            self.assertTrue(
                cleanup_worktrees.retain_manifest_payload(str(tree))["entries"]
                == entries
            )

    def test_special_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            os.mkfifo(tree / "pipe")
            with self.assertRaises(OSError):
                cleanup_worktrees.retain_manifest_payload(str(tree))

    def test_added_empty_dir_blocks_retained_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "keep.txt").write_text("ok\n")
            cleanup_worktrees.write_retain_manifest(str(tree))
            (tree / "later").mkdir()
            self.assertIs(
                cleanup_worktrees.retain_manifest_allows_prune(str(tree)),
                False,
            )


class VerifiedRemoveTests(unittest.TestCase):
    """The validation-to-delete boundary must fail closed, not just be re-checked.

    Reproduces the reported P1: a worker holding a directory handle opened before
    the claim renames writes into the tree after the final cleanliness check.
    `shutil.rmtree` destroyed that write; entry-level verification must not.
    """

    def _snapshot(self, tree):
        return cleanup_worktrees.retain_manifest_payload(str(tree))["entries"]

    def test_write_after_final_validation_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            tree.mkdir()
            (tree / "keep.txt").write_text("recorded\n")
            (tree / "nested").mkdir()
            (tree / "nested" / "inner.txt").write_text("recorded\n")
            expected = self._snapshot(tree)

            # The post-validation write the reviewer reproduced on this head.
            (tree / "late-after-final-check.txt").write_text("unsaved agent output\n")

            ok, note = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertFalse(ok)
            self.assertIn("after validation", note)
            self.assertTrue(tree.exists())
            self.assertTrue((tree / "late-after-final-check.txt").exists())
            self.assertEqual(
                (tree / "late-after-final-check.txt").read_text(),
                "unsaved agent output\n",
            )

    def test_modified_file_after_final_validation_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            tree.mkdir()
            (tree / "keep.txt").write_text("recorded\n")
            expected = self._snapshot(tree)
            (tree / "keep.txt").write_text("edited after validation\n")

            ok, note = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertFalse(ok)
            self.assertTrue((tree / "keep.txt").exists())
            self.assertEqual(
                (tree / "keep.txt").read_text(), "edited after validation\n"
            )

    def test_late_write_in_subdirectory_keeps_its_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            (tree / "a" / "b").mkdir(parents=True)
            (tree / "a" / "b" / "recorded.txt").write_text("ok\n")
            expected = self._snapshot(tree)
            (tree / "a" / "b" / "late.txt").write_text("late\n")

            ok, _ = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertFalse(ok)
            # Every directory above the surprise must survive with it.
            self.assertTrue((tree / "a" / "b" / "late.txt").exists())
            self.assertTrue((tree / "a" / "b").is_dir())
            self.assertTrue((tree / "a").is_dir())
            self.assertTrue(tree.is_dir())

    def test_unchanged_tree_is_fully_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            (tree / "a" / "b").mkdir(parents=True)
            (tree / "a" / "b" / "recorded.txt").write_text("ok\n")
            (tree / "empty").mkdir()
            (tree / "top.txt").write_text("ok\n")
            expected = self._snapshot(tree)

            ok, note = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertTrue(ok, note)
            self.assertFalse(tree.exists())

    def test_manifest_file_is_not_treated_as_a_surprise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            tree.mkdir()
            (tree / "keep.txt").write_text("ok\n")
            expected = self._snapshot(tree)
            cleanup_worktrees.write_retain_manifest(str(tree))

            ok, note = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertTrue(ok, note)
            self.assertFalse(tree.exists())

    def test_git_metadata_directory_does_not_block_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "retained"
            tree.mkdir()
            (tree / "keep.txt").write_text("ok\n")
            expected = self._snapshot(tree)
            # .git is excluded from the snapshot by design, so it must not read
            # as an unexpected entry.
            (tree / ".git" / "objects").mkdir(parents=True)
            (tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

            ok, note = cleanup_worktrees._remove_claimed_retained(str(tree), expected)
            self.assertTrue(ok, note)
            self.assertFalse(tree.exists())


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
        self.assertFalse(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("could not inspect remote", message)

    def test_unreadable_status_fails_sweep(self):
        path = _add_worktree(self.clone, "feat/issue-12-status")
        with patch.object(cleanup_worktrees, "dirty_status", return_value=None):
            ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertFalse(ok)
        self.assertTrue(path.exists())
        self.assertIn(os.path.realpath(path), self._worktree_paths())
        self.assertIn("status unreadable", message)

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

    def test_prune_worktree_retains_worktree_with_empty_ignored_worktrees_dir(self):
        path = _add_worktree(self.clone, "feat/issue-8-empty-worktrees")
        (path / ".gitignore").write_text(".worktrees/\n")
        _git(path, "add", ".gitignore")
        _git(path, "commit", "-m", "ignore worktrees")
        (path / ".worktrees").mkdir()
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
        ok, message = merge_pr.prune_worktree(
            str(self.clone), "feat/issue-8-empty-worktrees", sha
        )
        self.assertTrue(ok, message)
        self.assertIn("Retained worktree", message)
        self.assertNotIn(os.path.realpath(path), self._worktree_paths())
        retained_root = self.clone / ".worktrees" / ".retained"
        leftovers = [item for item in retained_root.iterdir() if item.is_dir()]
        self.assertTrue(leftovers)

    def test_dirty_deregistered_retained_copy_is_kept(self):
        path = _add_worktree(self.clone, "feat/issue-8-retain-dirty")
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
        ok, message = merge_pr.prune_worktree(
            str(self.clone), "feat/issue-8-retain-dirty", sha
        )
        self.assertTrue(ok, message)
        retained_root = self.clone / ".worktrees" / ".retained"
        leftovers = [item for item in retained_root.iterdir() if item.is_dir()]
        self.assertEqual(len(leftovers), 1)
        scratch = leftovers[0] / "scratch.txt"
        scratch.write_text("keep me\n")
        sweep_ok, sweep_msg = cleanup_worktrees.sweep(
            str(self.clone), include_labels=False
        )
        self.assertFalse(sweep_ok)
        self.assertTrue(scratch.exists())
        self.assertEqual(scratch.read_text(), "keep me\n")
        self.assertIn("dirty after retention", sweep_msg)

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

    def test_late_file_after_manifest_check_is_not_deleted(self):
        path = _add_worktree(self.clone, "feat/issue-8-retain-race")
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
        ok, message = merge_pr.prune_worktree(
            str(self.clone), "feat/issue-8-retain-race", sha
        )
        self.assertTrue(ok, message)
        real = cleanup_worktrees.retain_manifest_allows_prune
        calls = {"n": 0}

        def inject(tree):
            result = real(tree)
            calls["n"] += 1
            if calls["n"] == 1:
                (Path(tree) / "late.txt").write_text("injected\n")
            return result

        with patch.object(cleanup_worktrees, "retain_manifest_allows_prune", inject):
            sweep_ok, sweep_msg = cleanup_worktrees.sweep(
                str(self.clone), include_labels=False
            )
        leftovers = list((self.clone / ".worktrees" / ".retained").rglob("late.txt"))
        self.assertTrue(leftovers, sweep_msg)
        self.assertEqual(leftovers[0].read_text(), "injected\n")
        self.assertFalse(sweep_ok)
        self.assertIn("dirty after retention", sweep_msg)

    def test_symlinked_retained_root_does_not_delete_external_worktree(self):
        outside = self.root / "external-retained"
        outside.mkdir()
        victim = outside / "feat-issue-8-external"
        _git(
            self.clone, "worktree", "add", "-b",
            "feat/issue-8-external", str(victim),
        )
        retained = self.clone / ".worktrees" / ".retained"
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.symlink_to(outside)
        ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertTrue(victim.exists())
        self.assertTrue((victim / ".git").is_file())
        self.assertFalse(ok)
        self.assertIn("symlink", message)

    def test_manifest_temp_symlink_does_not_clobber_outside_file(self):
        path = _add_worktree(self.clone, "feat/issue-8-manifest-tmp")
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
        victim = self.root / "outside-victim.txt"
        victim.write_text("secret\n")
        real_open = cleanup_worktrees.os.open

        def injecting_open(name, flags, *args, **kwargs):
            target = str(name)
            suffix = cleanup_worktrees.RETAIN_MANIFEST + ".tmp"
            if target.endswith(suffix) and not os.path.lexists(target):
                os.symlink(str(victim), target)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(cleanup_worktrees.os, "open", injecting_open):
            ok, message = merge_pr.prune_worktree(
                str(self.clone), "feat/issue-8-manifest-tmp", sha
            )
        self.assertEqual(victim.read_text(), "secret\n")
        self.assertFalse(ok)
        self.assertIn("Could not snapshot", message)

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
            return _claim_gh(
                cmd, cwd,
                issues=[{"number": 2, "labels": [{"name": "agent:cursor-1"}]}],
                pulls=[{
                    "number": 3,
                    "labels": [{"name": "reviewer:agent-2"}, {"name": "merger:agent-3"}],
                    "merged_at": "2026-01-01T00:00:00Z",
                }],
            )

        gh_json.side_effect = fake_gh
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        issue_clear.assert_called_once_with(2, cwd=str(self.clone))
        review_clear.assert_called_once_with(3, cwd=str(self.clone))
        merger_clear.assert_called_once_with(3, cwd=str(self.clone))
        self.assertTrue(ok)
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
            return _claim_gh(
                cmd, cwd,
                issues=[{"number": 2, "labels": [{"name": "agent:cursor-1"}]}],
                pulls=[{
                    "number": 3,
                    "labels": [{"name": "reviewer:agent-2"}, {"name": "merger:agent-3"}],
                    "merged_at": "2026-01-01T00:00:00Z",
                }],
            )

        gh_json.side_effect = fake_gh
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        merger_clear.assert_not_called()
        self.assertTrue(ok)
        self.assertTrue(any("kept merger claim" in note for note in notes))
        issue_clear.assert_called_once_with(2, cwd=str(self.clone))
        review_clear.assert_called_once_with(3, cwd=str(self.clone))

    @patch.object(merge_pr, "clear_merger_claims")
    @patch.object(merge_pr, "_gh_json")
    def test_merger_claim_kept_when_linked_issue_open(self, gh_json, merger_clear):
        def fake_gh(cmd, cwd=None):
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
            return _claim_gh(
                cmd, cwd,
                pulls=[{
                    "number": 3,
                    "labels": [{"name": "merger:agent-3"}],
                    "merged_at": "2026-01-01T00:00:00Z",
                }],
            )

        gh_json.side_effect = fake_gh
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        merger_clear.assert_not_called()
        self.assertTrue(ok)
        self.assertTrue(any("kept merger claim on PR #3" in item for item in notes))

    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_retain_merger_pr_skips_current_closeout(self, gh_json, merger_clear):
        def fake_gh(cmd, cwd=None):
            return _claim_gh(
                cmd, cwd,
                pulls=[{
                    "number": 9,
                    "labels": [{"name": "merger:agent-3"}],
                    "merged_at": "2026-01-01T00:00:00Z",
                }],
            )

        gh_json.side_effect = fake_gh
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(
            str(self.clone), retain_merger_pr=9
        )
        merger_clear.assert_not_called()
        self.assertTrue(ok)
        self.assertTrue(any("current close-out incomplete" in item for item in notes))

    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "cleared"))
    @patch.object(merge_pr, "_gh_json")
    def test_claim_scan_paginates_github_lists(self, gh_json, _clear):
        seen = []

        def fake_gh(cmd, cwd=None):
            seen.append(cmd)
            return _claim_gh(cmd, cwd)

        gh_json.side_effect = fake_gh
        cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        paths = [
            cmd[3] for cmd in seen
            if cmd[:3] == ["gh", "api", "--paginate"]
        ]
        self.assertEqual(len(paths), 3)
        self.assertEqual(sum("/issues?" in path for path in paths), 1)
        self.assertEqual(sum("/pulls?" in path for path in paths), 2)

    @patch.object(merge_pr, "_gh_json", return_value=None)
    def test_unreadable_claim_list_is_recorded(self, _gh):
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertFalse(ok)
        self.assertTrue(any("claim scan failed" in item for item in notes))

    @patch.object(merge_pr, "clear_issue_claims",
                  return_value=(False, "Could not remove agent:cursor-1 from issue #2"))
    @patch.object(merge_pr, "_gh_json")
    def test_label_clear_failure_fails_closed(self, gh_json, _clear):
        def fake_gh(cmd, cwd=None):
            return _claim_gh(
                cmd, cwd,
                issues=[{"number": 2, "labels": [{"name": "agent:cursor-1"}]}],
            )

        gh_json.side_effect = fake_gh
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertFalse(ok)
        self.assertTrue(any("issue #2:" in item for item in notes))
        sweep_ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=True)
        self.assertFalse(sweep_ok)
        self.assertIn("issue #2:", message)

    @patch.object(merge_pr, "_gh_json")
    def test_claim_scan_runs_gh_in_repo_root(self, gh_json):
        gh_json.side_effect = lambda cmd, cwd=None: _claim_gh(cmd, cwd)
        ok, notes = cleanup_worktrees.clear_stale_claim_labels(str(self.clone))
        self.assertTrue(ok)
        self.assertTrue(gh_json.call_args_list)
        for call in gh_json.call_args_list:
            self.assertEqual(call.kwargs.get("cwd"), str(self.clone))
        self.assertEqual(notes, [])

    def test_fetch_failure_is_not_success(self):
        with patch.object(cleanup_worktrees, "refresh_origin", return_value=False):
            ok, message = cleanup_worktrees.sweep(str(self.clone), include_labels=False)
        self.assertFalse(ok)
        self.assertIn("fetch failed", message)


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
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")) as merger, \
             patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor")) as janitor, \
             patch.object(cleanup_worktrees, "local_ref_exists", return_value=False):
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertTrue(merge_pr.run_closeout(pr, [7], "/repo"))
            janitor.assert_called_once_with("/repo", retain_merger_pr=None)
            merger.assert_called_once()

    def test_failed_closeout_retains_current_merger_claim(self):
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(False, "w")), \
             patch.object(merge_pr, "retain_local_branch", return_value=(True, "l")), \
             patch.object(merge_pr, "delete_remote_branch", return_value=(True, "r")), \
             patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "c")), \
             patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "d")), \
             patch.object(merge_pr, "clear_issue_claims", return_value=(True, "i")), \
             patch.object(merge_pr, "clear_review_claims", return_value=(True, "v")), \
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")) as merger, \
             patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor")) as janitor, \
             patch.object(cleanup_worktrees, "local_ref_exists", return_value=False):
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertFalse(merge_pr.run_closeout(pr, [7], "/repo"))
            janitor.assert_called_once_with("/repo", retain_merger_pr=9)
            merger.assert_not_called()

    def test_closeout_retains_merger_when_local_branch_remains(self):
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(True, "w")), \
             patch.object(merge_pr, "retain_local_branch", return_value=(True, "l")), \
             patch.object(merge_pr, "delete_remote_branch", return_value=(True, "r")), \
             patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "c")), \
             patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "d")), \
             patch.object(merge_pr, "clear_issue_claims", return_value=(True, "i")), \
             patch.object(merge_pr, "clear_review_claims", return_value=(True, "v")), \
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")) as merger, \
             patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor")), \
             patch.object(cleanup_worktrees, "local_ref_exists", return_value=True):
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertFalse(merge_pr.run_closeout(pr, [7], "/repo"))
            merger.assert_not_called()

    def test_janitor_failure_is_recorded_in_failures(self):
        failures = []
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(True, "w")), \
             patch.object(merge_pr, "retain_local_branch", return_value=(True, "l")), \
             patch.object(merge_pr, "delete_remote_branch", return_value=(True, "r")), \
             patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "c")), \
             patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "d")), \
             patch.object(merge_pr, "clear_issue_claims", return_value=(True, "i")), \
             patch.object(merge_pr, "clear_review_claims", return_value=(True, "v")), \
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")), \
             patch.object(
                 merge_pr, "sweep_leftovers",
                 return_value=(False, "could not list worktrees"),
             ), \
             patch.object(cleanup_worktrees, "local_ref_exists", return_value=False):
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertFalse(
                merge_pr.run_closeout(pr, [7], "/repo", failures=failures)
            )
        self.assertEqual(failures, ["janitor: could not list worktrees"])

    def test_remaining_local_branch_is_recorded_in_failures(self):
        failures = []
        with patch.object(merge_pr.os, "chdir"), \
             patch.object(merge_pr, "prune_worktree", return_value=(True, "w")), \
             patch.object(merge_pr, "retain_local_branch", return_value=(True, "l")), \
             patch.object(merge_pr, "delete_remote_branch", return_value=(True, "r")), \
             patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "c")), \
             patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "d")), \
             patch.object(merge_pr, "clear_issue_claims", return_value=(True, "i")), \
             patch.object(merge_pr, "clear_review_claims", return_value=(True, "v")), \
             patch.object(merge_pr, "clear_merger_claims", return_value=(True, "m")), \
             patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor")), \
             patch.object(cleanup_worktrees, "local_ref_exists", return_value=True):
            pr = {
                "number": 9,
                "headRefName": "feat/x",
                "headRefOid": "abc",
                "headRepository": {"owner": {"login": "o"}, "name": "r"},
            }
            self.assertFalse(
                merge_pr.run_closeout(pr, [7], "/repo", failures=failures)
            )
        self.assertEqual(failures, ["local branch remaining: feat/x"])


if __name__ == "__main__":
    unittest.main()
