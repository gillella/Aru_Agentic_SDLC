#!/usr/bin/env python3
"""
test_revert_merge.py - Unit tests for scripts/revert_merge.py.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add scripts directory to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import revert_merge


class TestRevertMerge(unittest.TestCase):

    def test_parse_linked_issues(self):
        body = """
        ## Summary
        Fixes #89 and closes #90.
        Resolves #91.
        Duplicate Closes #89.
        """
        issues = revert_merge.parse_linked_issues(body)
        self.assertEqual(issues, [89, 90, 91])

    def test_parse_linked_issues_empty(self):
        self.assertEqual(revert_merge.parse_linked_issues("No issues linked"), [])
        self.assertEqual(revert_merge.parse_linked_issues(""), [])

    def test_get_merge_commit_sha_from_pr_dict(self):
        pr_data = {"number": 10, "mergeCommit": {"oid": "abc1234567890def"}}
        self.assertEqual(revert_merge.get_merge_commit_sha(pr_data), "abc1234567890def")

    @patch("revert_merge.run_cmd")
    def test_get_merge_commit_sha_from_tag(self, mock_run_cmd):
        pr_data = {"number": 10, "mergeCommit": None}
        mock_run_cmd.side_effect = [
            (0, "ckpt/10-abc1234\n", ""),
            (0, "abc1234567890def\n", ""),
        ]
        sha = revert_merge.get_merge_commit_sha(pr_data)
        self.assertEqual(sha, "abc1234567890def")

    @patch("revert_merge.fetch_pr_details")
    def test_revert_unmerged_pr_fails(self, mock_fetch):
        mock_fetch.return_value = {
            "number": 15,
            "state": "OPEN",
            "mergedAt": None,
        }
        res = revert_merge.revert_merge_pr(15)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.fetch_pr_details")
    def test_revert_dry_run_success(self, mock_fetch):
        mock_fetch.return_value = {
            "number": 15,
            "title": "fix something",
            "body": "Closes #12",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "sha1234567"},
        }
        res = revert_merge.revert_merge_pr(15, dry_run=True)
        self.assertEqual(res, revert_merge.EXIT_OK)

    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity")
    @patch("revert_merge.create_worktree")
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_clean_success(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_worktree, mock_identity, mock_enqueue, mock_update_status
    ):
        mock_fetch.return_value = {
            "number": 20,
            "title": "feat: add feature",
            "body": "Closes #30",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha123"},
        }
        mock_worktree.return_value = ".worktrees/revert-pr-20-feat-add-feature"

        # Mock run_cmd results:
        # 1. git revert -m 1 -> success
        # 2. git push -> success
        # 3. gh pr create -> success
        # 4. gh issue comment -> success
        mock_run_cmd.side_effect = [
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
            (0, "", ""),  # gh issue comment
        ]

        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_OK)
        mock_update_status.assert_called_once_with(30, "Ready")
        mock_identity.assert_called_once_with("99", agent="gemini-1", family="google")

    @patch("revert_merge.create_worktree")
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_conflict_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_worktree
    ):
        mock_fetch.return_value = {
            "number": 25,
            "title": "breaking change",
            "body": "Closes #40",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha456"},
        }
        mock_worktree.return_value = ".worktrees/revert-pr-25-breaking-change"

        # Mock run_cmd results:
        # 1. git revert -m 1 -> fail (code 1)
        # 2. git status --porcelain -> conflicts
        # 3. git revert --abort -> success
        # 4. git worktree remove -> success
        mock_run_cmd.side_effect = [
            (1, "", "conflict error"),  # git revert
            (0, "UU file1.py\nUU file2.py", ""),  # git status
            (0, "", ""),  # git revert --abort
            (0, "", ""),  # git worktree remove
        ]

        res = revert_merge.revert_merge_pr(25)
        self.assertEqual(res, revert_merge.EXIT_CONFLICT)


if __name__ == "__main__":
    unittest.main()
