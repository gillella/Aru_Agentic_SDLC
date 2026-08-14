import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_preview as dp


class DeployPreviewSkillTests(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parents[1]
        self.skill_file = self.root_dir / "skills" / "deploy-preview" / "SKILL.md"

    def test_deploy_preview_skill_file_exists(self):
        self.assertTrue(self.skill_file.exists(), f"Skill file {self.skill_file} missing")

    def test_deploy_preview_skill_frontmatter(self):
        content = self.skill_file.read_text()
        self.assertIn("name: deploy-preview", content)
        self.assertIn("triggers:", content)
        self.assertIn("deploy preview", content)

    def test_install_cursor_integration_includes_deploy_preview(self):
        installer = self.root_dir / "scripts" / "install_cursor_integration.sh"
        content = installer.read_text()
        self.assertIn("deploy-preview", content)

    def test_skill_requires_governed_helper_and_rejects_raw_gh_mutations(self):
        content = self.skill_file.read_text()
        self.assertIn("deploy_preview.py", content)
        self.assertNotIn("gh workflow run deploy-preview.yml", content)
        self.assertNotIn('gh issue comment <ISSUE_NUMBER> --body "🚀', content)

    def test_get_originating_issue_from_commit(self):
        with patch("deploy_preview.run_cmd", return_value=(0, "feat: thing\n\nCloses #109\n", "")):
            issue_id = dp.get_originating_issue("abcdef123456")
            self.assertEqual(issue_id, 109)

    def test_get_originating_issue_from_github_merge_commit(self):
        # Merge commit message with no Closes line, but references PR #159 which closes #100
        merge_msg = "Merge pull request #159 from gillella/feat/issue-100\n\nfeat(triage): require backtick verify"
        with patch("deploy_preview.run_cmd") as mock_run:
            mock_run.side_effect = [
                (0, merge_msg, ""),  # git log
                (0, '{"body": "## Summary\\nFixes triage contract.\\n\\nCloses #100"}', ""),  # gh pr view
            ]
            issue_id = dp.get_originating_issue("mergecommit123")
            self.assertEqual(issue_id, 100)

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_accepts_ancestor(self, mock_run):
        mock_run.side_effect = [
            (0, "fullsha123456789\n", ""),  # rev-parse commit
            (0, "mainsha123456789\n", ""),  # rev-parse origin/main
            (0, "", ""),  # merge-base --is-ancestor
        ]
        is_merged, resolved = dp.verify_commit_merged("fullsha123", default_branch="main")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, "fullsha123456789")

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_validates_against_refreshed_remote_default_branch(self, mock_run):
        # Stale local main does not contain commit, but origin/main contains commit
        mock_run.side_effect = [
            (0, "commitsha123\n", ""),  # rev-parse commit
            (0, "remoteheadsha\n", ""),  # rev-parse origin/main exists
            (0, "", ""),  # merge-base --is-ancestor commitsha123 origin/main
        ]
        is_merged, resolved = dp.verify_commit_merged("commitsha123", default_branch="main")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, "commitsha123")

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_rejects_unmerged_or_invalid_commit(self, mock_run):
        # Invalid / unknown commit
        mock_run.return_value = (1, "", "fatal: Not a valid object name")
        is_merged, _ = dp.verify_commit_merged("invalidsha", default_branch="main")
        self.assertFalse(is_merged)

        # Unmerged commit (not an ancestor of main)
        mock_run.side_effect = [
            (0, "fullsha123456789\n", ""),
            (0, "mainsha123456789\n", ""),
            (1, "", ""),  # merge-base failure
        ]
        is_merged, _ = dp.verify_commit_merged("unmergedsha", default_branch="main")
        self.assertFalse(is_merged)

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_correlates_new_run_id(self, mock_run):
        # Initial call: gh workflow run, then gh run list with delayed new run
        mock_run.side_effect = [
            (0, "", ""),  # gh workflow run
            (0, '[{"databaseId": 1001}, {"databaseId": 1002}]', ""),  # gh run list poll
        ]
        pre_existing = {1001}
        run_id = dp.dispatch_cd_workflow(
            "abcdef123456",
            workflow_name="deploy-preview.yml",
            pre_existing_run_ids=pre_existing,
            max_poll_attempts=1,
        )
        self.assertEqual(run_id, 1002)

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_fails_closed_when_run_query_fails_or_times_out(self, mock_run):
        # 1. gh run list fails during pre-existing run discovery
        mock_run.return_value = (1, "", "API rate limit")
        run_id = dp.dispatch_cd_workflow("abcdef123456", workflow_name="deploy-preview.yml")
        self.assertIsNone(run_id)

        # 2. Polling only sees existing run 1001 without new run appearing -> must NOT return 1001
        mock_run.side_effect = [
            (0, "", ""),  # gh workflow run
            (0, '[{"databaseId": 1001}]', ""),  # poll 1
        ]
        run_id = dp.dispatch_cd_workflow(
            "abcdef123456",
            workflow_name="deploy-preview.yml",
            pre_existing_run_ids={1001},
            max_poll_attempts=1,
            poll_interval=0,
        )
        self.assertIsNone(run_id)

    def test_deploy_preview_workflow_file_exists_and_init_project_renders_it(self):
        wf_path = self.root_dir / ".github" / "workflows" / "deploy-preview.yml"
        self.assertTrue(wf_path.exists(), f"{wf_path} must exist")
        content = wf_path.read_text()
        self.assertIn("name: Deploy Preview", content)
        self.assertIn("workflow_dispatch:", content)

        import init_project as ip
        self.assertIn("name: Deploy Preview", ip.DEPLOY_PREVIEW_WORKFLOW)

    @patch("deploy_preview.verify_commit_merged", return_value=(True, "abcdef123456"))
    @patch("deploy_preview.get_existing_run_ids", return_value=set())
    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=True)
    @patch("deploy_preview.post_preview_comment", return_value=True)
    def test_deploy_preview_success_workflow(self, mock_comment, mock_wait, mock_dispatch, mock_existing, mock_verify):
        exit_code = dp.deploy_preview(commit_sha="abcdef123456", issue_id=109, preview_url="https://preview.example.com")
        self.assertEqual(exit_code, 0)
        mock_verify.assert_called_once_with("abcdef123456")
        mock_dispatch.assert_called_once()
        mock_wait.assert_called_once_with(12345, dry_run=False)
        mock_comment.assert_called_once_with(109, "https://preview.example.com", "abcdef123456", dry_run=False)

    @patch("deploy_preview.verify_commit_merged", return_value=(True, "abcdef123456"))
    @patch("deploy_preview.get_existing_run_ids", return_value=set())
    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=False)
    @patch("deploy_preview.file_remediation_issue", return_value=201)
    def test_deploy_preview_failure_workflow(self, mock_remediate, mock_wait, mock_dispatch, mock_existing, mock_verify):
        exit_code = dp.deploy_preview(commit_sha="abcdef123456", issue_id=109)
        self.assertEqual(exit_code, 1)
        mock_remediate.assert_called_once()

    @patch("deploy_preview.run_cmd")
    def test_file_remediation_issue_attaches_to_board_or_fails_closed(self, mock_run):
        # Successful creation and attachment
        mock_run.side_effect = [
            (0, "https://github.com/owner/repo/issues/205\n", ""),  # gh issue create
            (0, "Attached to board", ""),  # update_issue_status.py --require-board
            (0, "", ""),  # gh issue comment notifying originating issue
        ]
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertEqual(new_id, 205)

        # Attachment failure fails closed
        mock_run.side_effect = [
            (0, "https://github.com/owner/repo/issues/206\n", ""),  # gh issue create
            (1, "", "Board attachment failed"),  # update_issue_status.py failure
        ]
        fail_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertIsNone(fail_id)


if __name__ == "__main__":
    unittest.main()
