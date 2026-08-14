#!/usr/bin/env python3
"""
test_revert_merge.py - Unit tests for scripts/revert_merge.py.
"""

import os
import sys
import unittest
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
        res = revert_merge.revert_merge_pr(15, agent="", family="google")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    def test_revert_without_family_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    def test_revert_with_invalid_family_fails(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="unknown_family")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.fetch_pr_details")
    def test_revert_unmerged_pr_fails(self, mock_fetch):
        mock_fetch.return_value = {
            "number": 15,
            "state": "OPEN",
            "mergedAt": None,
        }
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.fetch_pr_details")
    def test_revert_dry_run_success(self, mock_fetch):
        mock_fetch.return_value = {
            "number": 15,
            "title": "fix something",
            "body": "Closes #12",
            "baseRefName": "main",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "sha1234567"},
        }
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", dry_run=True)
        self.assertEqual(res, revert_merge.EXIT_OK)

    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=True)
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_clean_success(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_identity, mock_enqueue, mock_update_status
    ):
        mock_fetch.return_value = {
            "number": 20,
            "title": "feat: add feature",
            "body": "Closes #30",
            "baseRefName": "main",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha123"},
        }

        # Mock run_cmd calls:
        # 1. git fetch origin main -> ok
        # 2. git rev-parse --verify origin/main -> ok
        # 3. git worktree add -> ok
        # 4. git revert -m 1 -> ok
        # 5. git push -> ok
        # 6. gh pr create -> ok (returns PR url)
        # 7. gh issue reopen 30 -> ok
        # 8. gh issue comment 30 -> ok
        mock_run_cmd.side_effect = [
            (0, "", ""),  # git fetch
            (0, "origin/main\n", ""),  # git rev-parse --verify
            (0, "", ""),  # git worktree add
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
            (0, "", ""),  # gh issue reopen
            (0, "", ""),  # gh issue comment
        ]
        mock_update_status.return_value = True

        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_OK)
        mock_update_status.assert_called_once_with(30, "Ready", require_board=True)
        mock_identity.assert_called_once_with("99", agent="gemini-1", family="google")

        # Verify PR creation body contains Reverts #20 and Reopens #30, but not Closes #30
        pr_cmd = mock_run_cmd.call_args_list[5][0][0]
        self.assertIn("gh", pr_cmd)
        body_idx = pr_cmd.index("--body") + 1
        pr_body = pr_cmd[body_idx]
        self.assertIn("Reverts #20", pr_body)
        self.assertIn("Reopens #30", pr_body)
        self.assertNotIn("Closes #30", pr_body)

    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=True)
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_with_explicit_revert_issue_adds_closure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_identity, mock_enqueue, mock_update_status
    ):
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
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
            (0, "", ""),  # gh issue reopen
            (0, "", ""),  # gh issue comment
        ]
        mock_update_status.return_value = True

        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google", revert_issue=94)
        self.assertEqual(res, revert_merge.EXIT_OK)

        pr_cmd = mock_run_cmd.call_args_list[5][0][0]
        body_idx = pr_cmd.index("--body") + 1
        pr_body = pr_cmd[body_idx]
        self.assertIn("Closes #94", pr_body)
        self.assertNotIn("Closes #30", pr_body)

    @patch("revert_merge.get_unmerged_files", return_value=["file1.py", "file2.py"])
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_conflict_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_unmerged
    ):
        mock_fetch.return_value = {
            "number": 25,
            "title": "breaking change",
            "body": "Closes #40",
            "baseRefName": "develop",
            "state": "MERGED",
            "mergedAt": "2026-08-13T00:00:00Z",
            "mergeCommit": {"oid": "mergecommitsha456"},
        }

        # Mock run_cmd calls:
        # 1. git fetch origin develop -> ok
        # 2. git rev-parse --verify origin/develop -> ok
        # 3. git worktree add -> ok
        # 4. git revert -m 1 -> fail (code 1)
        # 5. git revert --abort -> ok
        # 6. git worktree remove -> ok
        # 7. git branch -D -> ok
        mock_run_cmd.side_effect = [
            (0, "", ""),  # git fetch
            (0, "origin/develop\n", ""),  # git rev-parse --verify
            (0, "", ""),  # git worktree add
            (1, "", "conflict error"),  # git revert
            (0, "", ""),  # git revert --abort
            (0, "", ""),  # git worktree remove
            (0, "", ""),  # git branch -D
        ]

        res = revert_merge.revert_merge_pr(25, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_CONFLICT)

    def test_revert_target_status_done_rejected(self):
        res = revert_merge.revert_merge_pr(15, agent="gemini-1", family="google", target_status="Done")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.update_status")
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=False)
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_apply_identity_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_identity, mock_enqueue, mock_update_status
    ):
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
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
        ]
        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_ERROR)
        mock_enqueue.assert_not_called()
        mock_update_status.assert_not_called()

    @patch("revert_merge.get_unmerged_files", return_value=[])
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_git_non_conflict_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_unmerged
    ):
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
            (1, "", "fatal: bad object"),  # git revert
            (0, "", ""),  # git revert --abort
            (0, "", ""),  # git worktree remove
            (0, "", ""),  # git branch -D
        ]
        res = revert_merge.revert_merge_pr(25, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

    @patch("revert_merge.update_status", return_value=False)
    @patch("revert_merge.enqueue_review")
    @patch("revert_merge.apply_identity", return_value=True)
    @patch("revert_merge.is_merge_commit", return_value=True)
    @patch("revert_merge.run_cmd")
    @patch("revert_merge.fetch_pr_details")
    def test_revert_issue_restoration_failure(
        self, mock_fetch, mock_run_cmd, mock_is_merge, mock_identity, mock_enqueue, mock_update_status
    ):
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
            (0, "", ""),  # git revert -m 1
            (0, "", ""),  # git push
            (0, "https://github.com/gillella/Aru_Agentic_SDLC/pull/99", ""),  # gh pr create
            (0, "", ""),  # gh issue reopen 30
        ]
        res = revert_merge.revert_merge_pr(20, agent="gemini-1", family="google")
        self.assertEqual(res, revert_merge.EXIT_ERROR)

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
