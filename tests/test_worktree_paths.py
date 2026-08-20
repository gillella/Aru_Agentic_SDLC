"""Agent-scoped worktree paths and ownership refusal (#305).

Split out of test_common.py to keep that module under the 400-line ceiling.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common  # noqa: E402


class WorktreePathScopingTests(unittest.TestCase):
    """Agent-scoped worktree paths (#305).

    Two agents sharing one clone previously derived the same directory from the
    branch name alone, so one agent's `git add` swept the other's uncommitted
    files into its commit -- a data-integrity defect, not a scheduling one.
    """

    def test_two_agents_get_different_directories_for_one_branch(self):
        a = common.worktree_path_for("feat/issue-3-risk-kernel", "claude-1")
        b = common.worktree_path_for("feat/issue-3-risk-kernel", "antigravity-1")
        self.assertNotEqual(a, b)

    def test_same_agent_resolves_the_same_directory_twice(self):
        # Resuming one's own work must reattach, so `resuming` claims keep working.
        branch = "feat/issue-3-risk-kernel"
        self.assertEqual(
            common.worktree_path_for(branch, "claude-1"),
            common.worktree_path_for(branch, "claude-1"),
        )

    def test_unscoped_path_is_unchanged_when_no_agent_is_named(self):
        # Worktrees created before this change must still resolve, not orphan.
        self.assertEqual(
            common.worktree_path_for("feat/issue-3-risk-kernel"),
            ".worktrees/feat-issue-3-risk-kernel",
        )

    def test_agent_component_is_filesystem_safe_and_short(self):
        path = common.worktree_path_for("feat/x", "agent/../../etc:passwd")
        self.assertNotIn("/", path[len(".worktrees/"):])
        self.assertNotIn("..", path)
        self.assertLessEqual(
            len(common.worktree_agent_component("a" * 200)),
            common.WORKTREE_AGENT_MAXLEN,
        )

    def test_agent_is_recoverable_from_the_path(self):
        path = common.worktree_path_for("feat/issue-3-thing", "claude-1")
        self.assertEqual(common.worktree_agent_of(path), "claude-1")

    def test_unscoped_path_reports_no_agent(self):
        self.assertEqual(
            common.worktree_agent_of(common.worktree_path_for("feat/issue-3-thing")), ""
        )


class CreateWorktreeOwnershipTests(unittest.TestCase):
    @staticmethod
    def _porcelain(path, branch):
        return f"worktree {path}\nHEAD abc123\nbranch refs/heads/{branch}\n"

    def test_refuses_a_worktree_held_by_another_agent(self):
        # The refusal is the fix: the old helper returned the path regardless,
        # handing the caller another agent's checkout to commit out of.
        held = common.worktree_path_for("feat/issue-3-thing", "antigravity-1")
        with patch.object(common, "run_cmd",
                          return_value=(0, self._porcelain(held, "feat/issue-3-thing"), "")):
            result = common.create_worktree("feat/issue-3-thing", agent="claude-1")
        self.assertIsNone(result)

    def test_refusal_names_the_conflicting_agent(self):
        held = common.worktree_path_for("feat/issue-3-thing", "antigravity-1")
        with patch.object(common, "run_cmd",
                          return_value=(0, self._porcelain(held, "feat/issue-3-thing"), "")), \
             patch("sys.stderr.write") as err:
            common.create_worktree("feat/issue-3-thing", agent="claude-1")
        written = "".join(call.args[0] for call in err.call_args_list)
        self.assertIn("antigravity-1", written)
        self.assertIn(held, written)

    def test_reattaches_to_its_own_existing_worktree(self):
        mine = common.worktree_path_for("feat/issue-3-thing", "claude-1")
        with patch.object(common, "run_cmd",
                          return_value=(0, self._porcelain(mine, "feat/issue-3-thing"), "")):
            self.assertEqual(
                common.create_worktree("feat/issue-3-thing", agent="claude-1"), mine)

    def test_unheld_branch_creates_the_scoped_worktree(self):
        calls = []

        def fake_run_cmd(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["git", "worktree", "list"]:
                return 0, "", ""
            return 0, "", ""

        with patch.object(common, "run_cmd", side_effect=fake_run_cmd), \
             patch("os.makedirs"):
            path = common.create_worktree("feat/issue-9-thing", agent="claude-1")
        self.assertEqual(path, common.worktree_path_for("feat/issue-9-thing", "claude-1"))
        self.assertIn(["git", "worktree", "add", "-b", "feat/issue-9-thing", path], calls)

    def test_returns_none_when_git_cannot_create_the_worktree(self):
        def fake_run_cmd(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "worktree", "list"]:
                return 0, "", ""
            return 1, "", "fatal: nope"

        with patch.object(common, "run_cmd", side_effect=fake_run_cmd), \
             patch("os.makedirs"), patch("sys.stderr.write"):
            self.assertIsNone(common.create_worktree("feat/issue-9-thing", agent="claude-1"))

    def test_unreadable_worktree_listing_does_not_block(self):
        # A transient `git worktree list` failure must not stall the fleet; git
        # itself still refuses a genuine double checkout.
        def fake_run_cmd(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "worktree", "list"]:
                return 128, "", "fatal: not a git repository"
            return 0, "", ""

        with patch.object(common, "run_cmd", side_effect=fake_run_cmd), \
             patch("os.makedirs"):
            self.assertIsNotNone(
                common.create_worktree("feat/issue-9-thing", agent="claude-1"))
