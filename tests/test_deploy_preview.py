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

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_uses_exact_ref(self, mock_run):
        mock_run.side_effect = [
            (0, "", ""),  # gh workflow run
            (0, '[{"databaseId": 98765}]', ""),  # gh run list
        ]
        run_id = dp.dispatch_cd_workflow("abcdef123456", workflow_name="deploy-preview.yml")
        self.assertEqual(run_id, 98765)
        first_call_cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("--ref", first_call_cmd)
        self.assertIn("abcdef123456", first_call_cmd)

    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=True)
    @patch("deploy_preview.post_preview_comment", return_value=True)
    def test_deploy_preview_success_workflow(self, mock_comment, mock_wait, mock_dispatch):
        exit_code = dp.deploy_preview(commit_sha="abcdef123456", issue_id=109, preview_url="https://preview.example.com")
        self.assertEqual(exit_code, 0)
        mock_dispatch.assert_called_once_with("abcdef123456", workflow_name="deploy-preview.yml", dry_run=False)
        mock_wait.assert_called_once_with(12345, dry_run=False)
        mock_comment.assert_called_once_with(109, "https://preview.example.com", "abcdef123456", dry_run=False)

    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=False)
    @patch("deploy_preview.file_remediation_issue", return_value=201)
    def test_deploy_preview_failure_workflow(self, mock_remediate, mock_wait, mock_dispatch):
        exit_code = dp.deploy_preview(commit_sha="abcdef123456", issue_id=109)
        self.assertEqual(exit_code, 1)
        mock_remediate.assert_called_once()
        args = mock_remediate.call_args[0]
        self.assertEqual(args[0], 109)
        self.assertEqual(args[1], "abcdef123456")

    @patch("deploy_preview.run_cmd")
    def test_file_remediation_issue_attaches_to_board(self, mock_run):
        mock_run.side_effect = [
            (0, "https://github.com/owner/repo/issues/205\n", ""),  # gh issue create
            (0, "Attached to board", ""),  # update_issue_status.py --require-board
            (0, "", ""),  # gh issue comment notifying originating issue
        ]
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertEqual(new_id, 205)
        # Check that update_issue_status was called with --require-board
        attach_call = mock_run.call_args_list[1][0][0]
        self.assertIn("--require-board", attach_call)
        self.assertIn("Ready", attach_call)


if __name__ == "__main__":
    unittest.main()
