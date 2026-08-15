import json
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
        # install_cursor_integration dynamically discovers all skills under skills/ that define SKILL.md
        self.assertTrue((self.root_dir / "skills" / "deploy-preview" / "SKILL.md").is_file())

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
            (0, "", ""),  # git fetch origin main
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
            (0, "", ""),  # git fetch origin main
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
        mock_run.side_effect = [
            (0, "", ""),  # git fetch origin main
            (1, "", "fatal: Not a valid object name"),  # rev-parse invalidsha
            (1, "", "fatal: could not fetch"),  # git fetch origin invalidsha
            (1, "", "fatal: Not a valid object name"),  # rev-parse retry
        ]
        is_merged, _ = dp.verify_commit_merged("invalidsha", default_branch="main")
        self.assertFalse(is_merged)

        # Unmerged commit (not an ancestor of main)
        mock_run.side_effect = [
            (0, "", ""),  # git fetch origin main
            (0, "fullsha123456789\n", ""),  # rev-parse unmergedsha
            (0, "mainsha123456789\n", ""),  # rev-parse origin/main
            (1, "", ""),  # merge-base failure
        ]
        is_merged, _ = dp.verify_commit_merged("unmergedsha", default_branch="main")
        self.assertFalse(is_merged)

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_correlates_new_run_id(self, mock_run):
        # Initial call: gh workflow run, gh run list with delayed new run, gh run view for correlation
        mock_run.side_effect = [
            (0, "", ""),  # gh workflow run
            (0, '[{"databaseId": 1001}, {"databaseId": 1002}]', ""),  # gh run list poll
            (0, json.dumps({"displayTitle": "Deploy Preview for abcdef123456 (tok123)", "name": "Deploy Preview", "headSha": "abcdef123456"}), ""),  # gh run view 1002
        ]
        pre_existing = {1001}
        run_id = dp.dispatch_cd_workflow(
            "abcdef123456",
            workflow_name="deploy-preview.yml",
            pre_existing_run_ids=pre_existing,
            default_branch="main",
            run_token="tok123",
            max_poll_attempts=1,
        )
        self.assertEqual(run_id, 1002)

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_fails_closed_when_run_query_fails_or_times_out(self, mock_run):
        # 1. gh run list fails during pre-existing run discovery
        mock_run.return_value = (1, "", "API rate limit")
        run_id = dp.dispatch_cd_workflow("abcdef123456", workflow_name="deploy-preview.yml", default_branch="main")
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
            default_branch="main",
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
        # 1. Successful creation and attachment
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list (no existing issue)
            (0, "https://github.com/owner/repo/issues/205\n", ""),  # gh issue create
            (0, "Attached to board", ""),  # update_issue_status.py --require-board
            (0, "", ""),  # gh issue comment notifying originating issue
        ]
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertEqual(new_id, 205)

        # 2. Board attachment failure on new creation fails closed
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list
            (0, "https://github.com/owner/repo/issues/206\n", ""),  # gh issue create
            (1, "", "Board attachment failed"),  # update_issue_status.py failure
        ]
        fail_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertIsNone(fail_id)

        # 3. Retry on existing unattached issue succeeds when board attachment succeeds
        mock_run.side_effect = [
            (0, '[{"number": 206, "title": "fix(deploy): preview deployment failed for commit abcdef1", "body": "commit abcdef123456"}]', ""),  # gh issue list finds 206
            (0, "Attached to board", ""),  # update_issue_status.py on existing issue 206
        ]
        retry_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertEqual(retry_id, 206)

        # 4. Retry on existing unattached issue fails closed when board attachment fails
        mock_run.side_effect = [
            (0, '[{"number": 206, "title": "fix(deploy): preview deployment failed for commit abcdef1", "body": "commit abcdef123456"}]', ""),  # gh issue list finds 206
            (1, "", "Board attachment still failing"),  # update_issue_status.py on existing issue 206
        ]
        fail_retry_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertIsNone(fail_retry_id)

        # 5. Issue creation command failure returns None
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list
            (1, "", "Failed to create issue"),  # gh issue create failure
        ]
        fail_create_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy timed out")
        self.assertIsNone(fail_create_id)

    def test_get_default_branch_preserves_slash_containing_branches(self):
        with patch("deploy_preview.run_cmd", return_value=(0, "refs/remotes/origin/release/v1.0\n", "")):
            branch = dp.get_default_branch()
            self.assertEqual(branch, "release/v1.0")

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_refreshes_origin_and_handles_slash_default_branch(self, mock_run):
        mock_run.side_effect = [
            (0, "", ""),  # git fetch origin release/v1.0
            (0, "fullsha123\n", ""),  # git rev-parse fullsha123^{commit}
            (0, "remoteheadsha\n", ""),  # git rev-parse origin/release/v1.0^{commit}
            (0, "", ""),  # git merge-base --is-ancestor
        ]
        is_merged, resolved = dp.verify_commit_merged("fullsha123", default_branch="release/v1.0")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, "fullsha123")
        mock_run.assert_any_call(["git", "fetch", "origin", "release/v1.0"], check=False)

    @patch("deploy_preview.run_cmd")
    def test_extract_preview_url_from_run_logs(self, mock_run):
        # 1. Extracted from gh run view --log (Pages output)
        mock_run.side_effect = [
            (0, '{"jobs": [{"steps": [{"name": "Deploy to GitHub Pages"}]}]}', ""),  # json jobs
            (0, "2026-08-14T20:00:00Z Page URL: https://gillella.github.io/Aru_Agentic_SDLC/\n", ""),  # log
        ]
        url = dp.extract_preview_url_from_run(12345)
        self.assertEqual(url, "https://gillella.github.io/Aru_Agentic_SDLC/")

        # 2. When run logs contain no preview URL -> returns None (no stale repository fallback)
        mock_run.side_effect = [
            (0, '{"jobs": []}', ""),  # json jobs
            (0, "No URL in logs", ""),  # log
        ]
        url2 = dp.extract_preview_url_from_run(12346)
        self.assertIsNone(url2)

    def test_is_valid_preview_url_validates_https_and_rejects_untrusted(self):
        self.assertTrue(dp.is_valid_preview_url("https://example.com/preview"))
        self.assertTrue(dp.is_valid_preview_url("https://gillella.github.io/Aru_Agentic_SDLC/"))
        self.assertFalse(dp.is_valid_preview_url("http://example.com/preview"))  # reject non-https
        self.assertFalse(dp.is_valid_preview_url("javascript:alert(1)"))
        self.assertFalse(dp.is_valid_preview_url("https://attacker.com/foo<script>"))
        self.assertFalse(dp.is_valid_preview_url(""))
        self.assertFalse(dp.is_valid_preview_url(None))

    def test_post_preview_comment_rejects_invalid_url(self):
        self.assertFalse(dp.post_preview_comment(109, "http://insecure.example.com", "abcdef1"))
        self.assertFalse(dp.post_preview_comment(109, "", "abcdef1"))

    @patch("deploy_preview.run_cmd")
    def test_dispatch_cd_workflow_correlates_matching_run_under_concurrent_dispatches(self, mock_run):
        # Two new runs appear in poll: 2001 (for commit A / token A) and 2002 (for commit B / token B)
        # Calling for commit A / token A must select 2001, not max(new_runs) (2002)
        def run_cmd_side_effect(cmd, check=False):
            if cmd[:3] == ["gh", "workflow", "run"]:
                return (0, "", "")
            if cmd[:3] == ["gh", "run", "list"]:
                return (0, '[{"databaseId": 1000}, {"databaseId": 2001}, {"databaseId": 2002}]', "")
            if cmd[:3] == ["gh", "run", "view"]:
                run_id = cmd[3]
                if run_id == "2002":
                    return (0, json.dumps({"displayTitle": "Deploy Preview for commitB (tokenB)", "name": "Deploy Preview", "headSha": "commitB"}), "")
                if run_id == "2001":
                    return (0, json.dumps({"displayTitle": "Deploy Preview for commitA (tokenA)", "name": "Deploy Preview", "headSha": "commitA"}), "")
            return (1, "", "unknown cmd")

        mock_run.side_effect = run_cmd_side_effect
        pre_existing = {1000}
        run_id = dp.dispatch_cd_workflow(
            "commitA",
            workflow_name="deploy-preview.yml",
            pre_existing_run_ids=pre_existing,
            default_branch="main",
            run_token="tokenA",
            max_poll_attempts=1,
        )
        self.assertEqual(run_id, 2001)

    @patch("deploy_preview.run_cmd")
    def test_file_remediation_issue_reuses_existing_issue(self, mock_run):
        # Existing open remediation issue #199 for commit abcdef1 attached to board
        mock_run.side_effect = [
            (0, json.dumps([{"number": 199, "title": "fix(deploy): preview deployment failed for commit abcdef1", "body": "details"}]), ""),
            (0, "Attached to board", ""),
        ]
        issue_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy failed")
        self.assertEqual(issue_id, 199)

    @patch("deploy_preview.run_cmd")
    def test_file_remediation_issue_returns_created_id_on_notification_warning(self, mock_run):
        # Issue created and attached, but originating comment fails -> durable partial success returns created ID
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list (no existing issue)
            (0, "https://github.com/owner/repo/issues/208\n", ""),  # gh issue create
            (0, "Attached to board", ""),  # update_issue_status.py
            (1, "", "Failed to comment on originating issue"),  # gh issue comment fails
        ]
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha="abcdef123456", error_details="Deploy failed")
        self.assertEqual(new_id, 208)

    def test_build_preview_artifact_with_visualizer_directory_fixture(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "project"
            out = Path(temp_dir) / "dist"
            viz = src / "sdlc_flow_visualizer"
            viz.mkdir(parents=True)
            (viz / "index.html").write_text("<!DOCTYPE html><html><body>Visualizer</body></html>")
            (viz / "styles.css").write_text("body { color: blue; }")
            (viz / "app.js").write_text("console.log('loaded');")

            success = bp.assemble_preview_artifact(str(src), str(out))
            self.assertTrue(success)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "styles.css").is_file())
            self.assertTrue((out / "app.js").is_file())
            self.assertEqual((out / "styles.css").read_text(), "body { color: blue; }")

    def test_build_preview_artifact_with_root_static_fixture(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "project"
            out = Path(temp_dir) / "dist"
            src.mkdir(parents=True)
            (src / "index.html").write_text("<!DOCTYPE html><html><body>Root</body></html>")
            (src / "styles.css").write_text("body { font-size: 14px; }")
            assets = src / "assets"
            assets.mkdir()
            (assets / "logo.svg").write_text("<svg></svg>")

            success = bp.assemble_preview_artifact(str(src), str(out))
            self.assertTrue(success)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "styles.css").is_file())
            self.assertTrue((out / "assets" / "logo.svg").is_file())

    def test_build_preview_artifact_fails_on_unsupported_project(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "project"
            out = Path(temp_dir) / "dist"
            src.mkdir(parents=True)
            # Only Python source without any HTML/preview entrypoint
            (src / "main.py").write_text("print('hello')")

            success = bp.assemble_preview_artifact(str(src), str(out))
            self.assertFalse(success)
            self.assertFalse((out / "index.html").exists())

    def test_build_preview_artifact_preserves_existing_dist_artifact(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "project"
            dist = src / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<!DOCTYPE html><html><body>Existing Dist</body></html>")
            (dist / "styles.css").write_text("body { color: green; }")

            # Source dist == output dist
            success = bp.assemble_preview_artifact(str(src), str(dist))
            self.assertTrue(success)
            self.assertTrue((dist / "index.html").is_file())
            self.assertTrue((dist / "styles.css").is_file())
            self.assertEqual((dist / "styles.css").read_text(), "body { color: green; }")


if __name__ == "__main__":
    unittest.main()
