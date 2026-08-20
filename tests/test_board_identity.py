"""GitHub-side agent id ownership (#304).

Split out of test_common.py to keep that module under the 400-line ceiling.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common  # noqa: E402


class BoardAgentIdentityTests(unittest.TestCase):
    """GitHub-side view of which agent ids are in use (#304)."""

    @staticmethod
    def _labelled(number, *names):
        return {"number": number, "labels": [{"name": n} for n in names]}

    def test_collects_every_identity_prefix_from_issues_and_prs(self):
        issues = [self._labelled(3, "agent:antigravity-1", "status:in-progress")]
        prs = [self._labelled(27, "author:antigravity-1", "reviewer:claude-1"),
               self._labelled(21, "merger:codex-1")]
        with patch.object(common, "run_gh_json", side_effect=[issues, prs]):
            holders, error = common.board_agent_identities()
        self.assertEqual(error, "")
        self.assertEqual(
            holders["antigravity-1"],
            ["issue #3 (agent:antigravity-1)", "PR #27 (author:antigravity-1)"])
        self.assertIn("claude-1", holders)
        self.assertIn("codex-1", holders)

    def test_unreadable_board_returns_none_so_callers_fail_closed(self):
        with patch.object(common, "run_gh_json", side_effect=[None, []]):
            holders, error = common.board_agent_identities()
        self.assertIsNone(holders)
        self.assertIn("issue", error)

    def test_a_pr_query_failure_also_fails_closed(self):
        with patch.object(common, "run_gh_json", side_effect=[[], None]):
            holders, error = common.board_agent_identities()
        self.assertIsNone(holders)
        self.assertIn("PR", error)

    def test_unrelated_and_empty_labels_are_ignored(self):
        issues = [self._labelled(1, "priority:p0", "agent:", "type:fix")]
        with patch.object(common, "run_gh_json", side_effect=[issues, []]):
            holders, _ = common.board_agent_identities()
        self.assertEqual(holders, {})

    def test_empty_board_yields_no_holders(self):
        with patch.object(common, "run_gh_json", side_effect=[[], []]):
            holders, error = common.board_agent_identities()
        self.assertEqual((holders, error), ({}, ""))
