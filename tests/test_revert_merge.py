#!/usr/bin/env python3
"""
test_revert_merge.py - Unit tests for scripts/revert_merge.py.
"""

import os
import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

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

    def test_parse_all_github_closing_keyword_forms(self):
        body = "Close #1 Closes #2 Closed #3 Fix #4 Fixes #5 Fixed #6 Resolve #7 Resolves #8 Resolved #9"
        self.assertEqual(revert_merge.parse_linked_issues(body), list(range(1, 10)))

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

    @patch("revert_merge.run_cmd")
    def test_get_merge_commit_sha_multiple_tags_ambiguous(self, mock_run_cmd):
        pr_data = {"number": 10, "mergeCommit": None}
        mock_run_cmd.return_value = (0, "ckpt/10-abc1234\nckpt/10-def5678\n", "")
        sha = revert_merge.get_merge_commit_sha(pr_data)
        self.assertIsNone(sha)

    def test_revert_without_agent_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    def test_revert_without_family_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    def test_revert_with_invalid_family_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="unknown_family", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    def test_revert_without_revert_issue_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=None)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.get_issue", return_value=None)
    def test_revert_with_missing_revert_issue_fails(self, mock_get_issue):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=999)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.get_issue")
    def test_revert_with_closed_revert_issue_fails(self, mock_get_issue):
        mock_get_issue.return_value = {"number": 94, "state": "CLOSED", "labels": [{"name": "agent:gemini-1"}]}
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.claimed_by", return_value="other-agent")
    @patch("revert_merge.get_issue")
    def test_revert_with_unclaimed_revert_issue_fails(self, mock_get_issue, mock_claimed):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:other-agent"}]}
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    def test_revert_target_status_invalid_typo_rejected(self, mock_get_issue, mock_claimed):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        stderr = StringIO()
        with redirect_stderr(stderr):
            res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94, target_status="Redy")
        self.assertEqual(res, revert_merge.EXIT_ERROR)
        self.assertIn("'Redy' is not a valid target status", stderr.getvalue())

    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    def test_revert_target_status_done_rejected(self, mock_get_issue, mock_claimed):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        stderr = StringIO()
        with redirect_stderr(stderr):
            res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94, target_status="Done")
        self.assertEqual(res, revert_merge.EXIT_ERROR)
        self.assertIn("cannot set target issue status to 'Done'", stderr.getvalue())

    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_unmerged_pr_fails(self, mock_fetch, mock_get_issue, mock_claimed):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        mock_fetch.return_value = {
            "number": 15,
            "state": "OPEN",
            "mergedAt": None,
        }
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_dry_run_success(self, mock_fetch, mock_get_issue, mock_claimed):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        mock_fetch.return_value = {
            "number": 15,
            "title": "fix something",
            "body": "Closes #12",
            "baseRefName": "main",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "sha1234567"},
        }
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", revert_issue=94, dry_run=True)
        self.assertEqual(res, revert_merge.EXIT_OK)

    @patch("revert_merge.find_existing_revert_pr", return_value=None)
    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=True)
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_runs_when_log_contains_unrelated_revert_text(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_identity, mock_enqueue, mock_update_status, mock_get_issue, mock_claimed, mock_find_pr
    ):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        mock_fetch.return_value = {
            "number": 20,
            "title": "feat: add feature",
            "body": "Closes #30",
            "baseRefName": "main",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha123"},
        }

        mock_run_cmd.side_effect = [
            (0, "", ""),  # git fetch
            (0, "origin/main\n", ""),  # git rev-parse --verify
            (0, "", ""),  # git worktree add
            (0, "Revert workflow documentation\n", ""),  # branch log has unrelated text
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
            (0, "", ""),  # gh issue reopen
            (0, "", ""),  # gh issue comment
        ]
        mock_update_status.return_value = True

        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_OK)
        log_cmd = mock_run_cmd.call_args_list[3][0][0]
        self.assertEqual(log_cmd, ["git", "log", "--format=%B", "origin/main..HEAD"])
        self.assertTrue(
            any(call.args[0][:2] == ["git", "revert"] for call in mock_run_cmd.call_args_list),
            "an unrelated 'revert' in history must not skip git revert",
        )
        mock_update_status.assert_called_once_with(30, "Ready", require_board=True)
        mock_identity.assert_called_once_with("99", agent="gemini-1", family="google")

        # Verify PR creation body contains Reverts #20, Reopens #30, and Closes #94 (revert issue), but not Closes #30
        pr_cmd = mock_run_cmd.call_args_list[6][0][0]
        self.assertIn("gh", pr_cmd)
        body_idx = pr_cmd.index("--body") + 1
        pr_body = pr_cmd[body_idx]
        self.assertIn("Reverts #20", pr_body)
        self.assertIn("Reopens #30", pr_body)
        self.assertIn("Closes #94", pr_body)
        self.assertNotIn("Closes #30", pr_body)

    @patch("revert_merge.find_existing_revert_pr")
    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_resume_from_existing_pr(
        self, mock_fetch, mock_run_cmd, mock_identity, mock_enqueue, mock_update_status, mock_get_issue, mock_claimed, mock_find_pr
    ):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        mock_fetch.return_value = {
            "number": 20,
            "title": "feat: add feature",
            "body": "Closes #30",
            "baseRefName": "main",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha123"},
        }
        # Existing PR already created on GitHub
        mock_find_pr.return_value = {
            "number": 99,
            "url": "https://github.com/gillella/Aru_Agentic_SDLC/pull/99",
            "headRefName": "revert/pr-20-feat-add-feature",
            "baseRefName": "main",
        }

        mock_run_cmd.side_effect = [
            (0, "", ""),  # gh issue reopen 30
            (0, "", ""),  # gh issue comment 30
        ]
        mock_update_status.return_value = True

        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_OK)
        # Should not have called git fetch, worktree add, or pr create
        mock_identity.assert_called_once_with("99", agent="gemini-1", family="google")
        mock_enqueue.assert_called_once_with("99")
        mock_update_status.assert_called_once_with(30, "Ready", require_board=True)

    @patch("revert_merge.find_existing_revert_pr", return_value=None)
    @patch("revert_merge.claimed_by", return_value="gemini-1")
    @patch("revert_merge.get_issue")
    @patch("revert_merge.get_unmerged_files", return_value=["file1.py", "file2.py"])
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_conflict_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_unmerged, mock_get_issue, mock_claimed, mock_find_pr
    ):
        mock_get_issue.return_value = {"number": 94, "state": "OPEN", "labels": [{"name": "agent:gemini-1"}]}
        mock_fetch.return_value = {
            "number": 25,
            "title": "breaking change",
            "body": "Closes #40",
            "baseRefName": "develop",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha456"},
        }

        mock_run_cmd.side_effect = [
            (0, "", ""),  # git fetch
            (0, "origin/develop\n", ""),  # git rev-parse --verify
            (0, "", ""),  # git worktree add
            (1, "", ""),  # git log -1
            (1, "", "conflict error"),  # git revert
            (0, "", ""),  # git revert --abort
            (0, "", ""),  # git worktree remove
            (0, "", ""),  # git branch -D
        ]

        res = revert_merge.revert_merge_pr(25, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_CONFLICT)

    @patch("revert_merge.run_cmd")
    def test_get_unmerged_files(self, mock_run_cmd):
        mock_run_cmd.side_effect = [
            (0, "file1.py\nfile2.py\n", ""),  # git diff --name-only --diff-filter=U
            (0, "AA file2.py\nUD file3.py\nUU file4.py", ""),  # git status --porcelain
        ]
        files = revert_merge.get_unmerged_files("/tmp")
        self.assertEqual(files, ["file1.py", "file2.py", "file3.py", "file4.py"])


if __name__ == "__main__":
    unittest.main()
