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
        self.commit_sha = "a" * 40
        self.repo_slug = "gillella/Aru_Agentic_SDLC"
        self.preview_url = "https://gillella.github.io/Aru_Agentic_SDLC/"

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
            (0, "", ""),  # exact remote-default fetch
            (0, f"{self.commit_sha}\n", ""),  # rev-parse commit
            (0, "mainsha123456789\n", ""),  # rev-parse origin/main
            (0, "", ""),  # merge-base --is-ancestor
        ]
        is_merged, resolved = dp.verify_commit_merged(self.commit_sha[:12], default_branch="main")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, self.commit_sha)
        self.assertEqual(
            mock_run.call_args_list[0].args[0],
            ["git", "fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"],
        )

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_validates_against_refreshed_remote_default_branch(self, mock_run):
        # Stale local main does not contain commit, but origin/main contains commit
        mock_run.side_effect = [
            (0, "", ""),  # exact remote-default fetch
            (0, f"{self.commit_sha}\n", ""),  # rev-parse commit
            (0, "remoteheadsha\n", ""),  # rev-parse origin/main exists
            (0, "", ""),  # merge-base --is-ancestor commitsha123 origin/main
        ]
        is_merged, resolved = dp.verify_commit_merged(self.commit_sha[:12], default_branch="main")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, self.commit_sha)

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_rejects_unmerged_or_invalid_commit(self, mock_run):
        # Invalid / unknown commit
        mock_run.side_effect = [
            (0, "", ""),  # exact remote-default fetch
            (1, "", "fatal: Not a valid object name"),  # rev-parse invalidsha
        ]
        is_merged, _ = dp.verify_commit_merged("b" * 12, default_branch="main")
        self.assertFalse(is_merged)

        # Unmerged commit (not an ancestor of main)
        mock_run.side_effect = [
            (0, "", ""),  # exact remote-default fetch
            (0, f"{self.commit_sha}\n", ""),  # rev-parse unmergedsha
            (0, "mainsha123456789\n", ""),  # rev-parse origin/main
            (1, "", ""),  # merge-base failure
        ]
        is_merged, _ = dp.verify_commit_merged(self.commit_sha[:12], default_branch="main")
        self.assertFalse(is_merged)

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_fails_closed_on_fetch_failure(self, mock_run):
        mock_run.return_value = (1, "", "rate limited")
        self.assertEqual(
            dp.verify_commit_merged(self.commit_sha, default_branch="main"),
            (False, ""),
        )
        self.assertEqual(mock_run.call_count, 1)

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
            self.commit_sha,
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
        run_id = dp.dispatch_cd_workflow(self.commit_sha, workflow_name="deploy-preview.yml", default_branch="main")
        self.assertIsNone(run_id)

        # 2. Polling only sees existing run 1001 without new run appearing -> must NOT return 1001
        mock_run.side_effect = [
            (0, "", ""),  # gh workflow run
            (0, '[{"databaseId": 1001}]', ""),  # poll 1
        ]
        run_id = dp.dispatch_cd_workflow(
            self.commit_sha,
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
        self.assertIn("Checkout trusted control plane", content)
        self.assertIn("git merge-base --is-ancestor", content)
        self.assertIn("python3 control-plane/scripts/build_preview.py", content)
        self.assertIn("needs: build-preview", content)
        self.assertIn("Configure Pages", content)
        self.assertNotIn("enablement: true", content)
        self.assertNotIn("Checkout merged commit", content)
        self.assertNotIn('if [ -f "scripts/build_preview.py" ]', content)

        import init_project as ip
        self.assertEqual(content, ip.DEPLOY_PREVIEW_WORKFLOW)

    @patch("deploy_preview.get_repo_slug", return_value="gillella/Aru_Agentic_SDLC")
    @patch("deploy_preview.get_default_branch", return_value="main")
    @patch("deploy_preview.verify_commit_merged", return_value=(True, "a" * 40))
    @patch("deploy_preview.ensure_pages_enabled", return_value=True)
    @patch("deploy_preview.get_existing_run_ids", return_value=set())
    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=dp.RunOutcome(True, "success", "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345"))
    @patch("deploy_preview.extract_preview_url_from_run", return_value="https://gillella.github.io/Aru_Agentic_SDLC/")
    @patch("deploy_preview.post_preview_comment", return_value=True)
    def test_deploy_preview_success_workflow(
        self, mock_comment, mock_extract, mock_wait, mock_dispatch, mock_existing,
        mock_pages, mock_verify, mock_default, mock_repo,
    ):
        exit_code = dp.deploy_preview(commit_sha=self.commit_sha, issue_id=109, preview_url=self.preview_url)
        self.assertEqual(exit_code, 0)
        mock_verify.assert_called_once_with(self.commit_sha, default_branch="main")
        mock_dispatch.assert_called_once()
        mock_wait.assert_called_once_with(12345, dry_run=False)
        mock_extract.assert_called_once_with(
            12345, self.commit_sha, self.repo_slug, dry_run=False,
        )
        mock_comment.assert_called_once_with(
            109, self.preview_url, self.commit_sha, self.repo_slug, dry_run=False,
        )

    @patch("deploy_preview.get_repo_slug", return_value="gillella/Aru_Agentic_SDLC")
    @patch("deploy_preview.get_default_branch", return_value="main")
    @patch("deploy_preview.verify_commit_merged", return_value=(True, "a" * 40))
    @patch("deploy_preview.ensure_pages_enabled", return_value=True)
    @patch("deploy_preview.get_existing_run_ids", return_value=set())
    @patch("deploy_preview.dispatch_cd_workflow", return_value=12345)
    @patch("deploy_preview.wait_for_run", return_value=dp.RunOutcome(False, "timed-out", "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345"))
    @patch("deploy_preview.file_remediation_issue", return_value=201)
    def test_deploy_preview_failure_workflow(
        self, mock_remediate, mock_wait, mock_dispatch, mock_existing, mock_pages,
        mock_verify, mock_default, mock_repo,
    ):
        exit_code = dp.deploy_preview(commit_sha=self.commit_sha, issue_id=109)
        self.assertEqual(exit_code, 1)
        mock_remediate.assert_called_once()

    def test_deploy_preview_rejects_no_wait_without_any_mutation(self):
        with patch("deploy_preview.get_default_branch") as mock_default:
            self.assertEqual(
                dp.deploy_preview(self.commit_sha, issue_id=109, wait=False),
                1,
            )
        mock_default.assert_not_called()

    @patch("deploy_preview.run_cmd")
    def test_file_remediation_issue_attaches_to_board_or_fails_closed(self, mock_run):
        # 1. Successful creation and attachment
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list (no existing issue)
            (0, "https://github.com/owner/repo/issues/205\n", ""),  # gh issue create
            (0, "Attached to board", ""),  # update_issue_status.py --require-board
            (0, "", ""),  # gh issue comment notifying originating issue
        ]
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy timed out")
        self.assertEqual(new_id, 205)

        # 2. Board attachment failure on new creation fails closed
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list
            (0, "https://github.com/owner/repo/issues/206\n", ""),  # gh issue create
            (1, "", "Board attachment failed"),  # update_issue_status.py failure
        ]
        fail_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy timed out")
        self.assertIsNone(fail_id)

        # 3. Retry on existing unattached issue succeeds when board attachment succeeds
        mock_run.side_effect = [
            (0, json.dumps([{"number": 206, "title": "fix(deploy): preview deployment failed", "body": dp.REMEDIATION_MARKER.format(commit_sha=self.commit_sha)}]), ""),
            (0, "Attached to board", ""),  # update_issue_status.py on existing issue 206
        ]
        retry_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy timed out")
        self.assertEqual(retry_id, 206)

        # 4. Retry on existing unattached issue fails closed when board attachment fails
        mock_run.side_effect = [
            (0, json.dumps([{"number": 206, "title": "fix(deploy): preview deployment failed", "body": dp.REMEDIATION_MARKER.format(commit_sha=self.commit_sha)}]), ""),
            (1, "", "Board attachment still failing"),  # update_issue_status.py on existing issue 206
        ]
        fail_retry_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy timed out")
        self.assertIsNone(fail_retry_id)

        # 5. Issue creation command failure returns None
        mock_run.side_effect = [
            (0, "[]", ""),  # gh issue list
            (1, "", "Failed to create issue"),  # gh issue create failure
        ]
        fail_create_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy timed out")
        self.assertIsNone(fail_create_id)

    @patch("deploy_preview.run_cmd", return_value=(1, "", "rate limit"))
    def test_file_remediation_query_failure_does_not_create(self, mock_run):
        self.assertIsNone(
            dp.file_remediation_issue(109, self.commit_sha, "Deploy timed out")
        )
        self.assertEqual(mock_run.call_count, 1)

    def test_get_default_branch_preserves_slash_containing_branches(self):
        with patch("deploy_preview.run_cmd", return_value=(0, "release/v1.0\n", "")):
            branch = dp.get_default_branch()
            self.assertEqual(branch, "release/v1.0")

    @patch("deploy_preview.run_cmd")
    def test_ensure_pages_enabled_uses_operator_credential_for_missing_site(self, mock_run):
        mock_run.side_effect = [
            (1, "", "gh: Not Found (HTTP 404)"),
            (0, '{"build_type":"workflow"}', ""),
        ]
        self.assertTrue(dp.ensure_pages_enabled(self.repo_slug))
        self.assertEqual(
            mock_run.call_args_list[1].args[0],
            [
                "gh", "api", "--method", "POST",
                f"repos/{self.repo_slug}/pages", "-f", "build_type=workflow",
            ],
        )

    @patch("deploy_preview.run_cmd")
    def test_ensure_pages_enabled_fails_closed_on_api_error(self, mock_run):
        mock_run.return_value = (1, "", "gh: rate limit (HTTP 403)")
        self.assertFalse(dp.ensure_pages_enabled(self.repo_slug))
        self.assertEqual(mock_run.call_count, 1)

    @patch("deploy_preview.run_cmd")
    def test_ensure_pages_enabled_switches_legacy_site_to_workflow(self, mock_run):
        mock_run.side_effect = [
            (0, '{"build_type":"legacy"}', ""),
            (0, "", ""),
        ]
        self.assertTrue(dp.ensure_pages_enabled(self.repo_slug))
        self.assertIn("PUT", mock_run.call_args_list[1].args[0])

    @patch("deploy_preview.run_cmd")
    def test_verify_commit_merged_refreshes_origin_and_handles_slash_default_branch(self, mock_run):
        mock_run.side_effect = [
            (0, "", ""),  # exact remote-default fetch
            (0, f"{self.commit_sha}\n", ""),
            (0, "remoteheadsha\n", ""),  # git rev-parse origin/release/v1.0^{commit}
            (0, "", ""),  # git merge-base --is-ancestor
        ]
        is_merged, resolved = dp.verify_commit_merged(self.commit_sha[:12], default_branch="release/v1.0")
        self.assertTrue(is_merged)
        self.assertEqual(resolved, self.commit_sha)
        mock_run.assert_any_call(
            ["git", "fetch", "--no-tags", "origin", "refs/heads/release/v1.0:refs/remotes/origin/release/v1.0"],
            check=False,
        )

    @patch("deploy_preview.run_cmd")
    def test_extract_preview_url_requires_exact_run_metadata(self, mock_run):
        def download_metadata(cmd, check=False):
            output_dir = Path(cmd[cmd.index("--dir") + 1])
            (output_dir / "preview-metadata.json").write_text(json.dumps({
                "run_id": "12345",
                "commit_sha": self.commit_sha,
                "repository": self.repo_slug,
                "preview_url": self.preview_url,
            }))
            return (0, "", "")

        mock_run.side_effect = download_metadata
        self.assertEqual(
            dp.extract_preview_url_from_run(12345, self.commit_sha, self.repo_slug),
            self.preview_url,
        )

        def stale_metadata(cmd, check=False):
            output_dir = Path(cmd[cmd.index("--dir") + 1])
            (output_dir / "preview-metadata.json").write_text(json.dumps({
                "run_id": "99999",
                "commit_sha": self.commit_sha,
                "repository": self.repo_slug,
                "preview_url": self.preview_url,
            }))
            return (0, "", "")

        mock_run.side_effect = stale_metadata
        self.assertIsNone(
            dp.extract_preview_url_from_run(12345, self.commit_sha, self.repo_slug)
        )

    def test_is_valid_preview_url_validates_https_and_rejects_untrusted(self):
        self.assertTrue(dp.is_valid_preview_url(self.preview_url, self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url("https://example.com/preview", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url("https://gillella.github.io/wrong/", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url(f"{self.preview_url}?old=1", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url("javascript:alert(1)", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url("https://gillella.github.io/Aru_Agentic_SDLC/(bad)", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url("", self.repo_slug))
        self.assertFalse(dp.is_valid_preview_url(None, self.repo_slug))

    def test_post_preview_comment_rejects_invalid_url(self):
        self.assertFalse(dp.post_preview_comment(109, "http://insecure.example.com", self.commit_sha, self.repo_slug))
        self.assertFalse(dp.post_preview_comment(109, "", self.commit_sha, self.repo_slug))

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
                    return (0, json.dumps({"displayTitle": f"Deploy Preview for {'b' * 40} (tokenB)", "name": "Deploy Preview", "headSha": "b" * 40}), "")
                if run_id == "2001":
                    return (0, json.dumps({"displayTitle": f"Deploy Preview for {self.commit_sha} (tokenA)", "name": "Deploy Preview", "headSha": self.commit_sha}), "")
            return (1, "", "unknown cmd")

        mock_run.side_effect = run_cmd_side_effect
        pre_existing = {1000}
        run_id = dp.dispatch_cd_workflow(
            self.commit_sha,
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
            (0, json.dumps([{
                "number": 199,
                "title": "fix(deploy): preview deployment failed",
                "body": dp.REMEDIATION_MARKER.format(commit_sha=self.commit_sha),
            }]), ""),
            (0, "Attached to board", ""),
        ]
        issue_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy failed")
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
        new_id = dp.file_remediation_issue(issue_id=109, commit_sha=self.commit_sha, error_details="Deploy failed")
        self.assertEqual(new_id, 208)

    def test_build_preview_artifact_with_visualizer_directory_fixture(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "project"
            out = src / "dist"
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
            out = src / "dist"
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
            out = src / "dist"
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

    def test_workflow_validates_before_target_checkout_and_separates_privileges(self):
        content = (self.root_dir / ".github" / "workflows" / "deploy-preview.yml").read_text()
        validation = content.index("git merge-base --is-ancestor")
        target_checkout = content.index("git worktree add --detach ../target")
        trusted_build = content.index("python3 control-plane/scripts/build_preview.py")
        credentialed_job = content.index("  deploy-preview:")
        self.assertLess(validation, target_checkout)
        self.assertLess(target_checkout, trusted_build)
        self.assertLess(trusted_build, credentialed_job)
        build_job = content[content.index("  build-preview:"):credentialed_job]
        self.assertNotIn("pages: write", build_job)
        self.assertNotIn("id-token: write", build_job)

    @patch("deploy_preview.run_cmd")
    def test_wait_for_run_times_out_without_blocking_watch(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps({
                "status": "queued",
                "conclusion": "",
                "url": "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345",
            }),
            "",
        )
        with patch("deploy_preview.time.monotonic", return_value=10.0):
            outcome = dp.wait_for_run(12345, timeout_seconds=0, poll_interval=0)
        self.assertEqual(outcome.state, "timed-out")
        self.assertFalse(outcome.success)
        self.assertNotIn("watch", mock_run.call_args.args[0])

    def test_build_preview_rejects_output_escape_without_deleting_source(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "project"
            source.mkdir()
            (source / "index.html").write_text("safe")
            self.assertFalse(bp.assemble_preview_artifact(str(source), str(root)))
            self.assertEqual((source / "index.html").read_text(), "safe")

    def test_build_preview_rejects_output_and_asset_symlinks(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "project"
            source.mkdir()
            (source / "index.html").write_text("safe")
            external = root / "external"
            external.mkdir()
            (external / "sentinel.txt").write_text("keep")
            (source / "dist").symlink_to(external, target_is_directory=True)
            self.assertFalse(bp.assemble_preview_artifact(str(source), str(source / "dist")))
            self.assertEqual((external / "sentinel.txt").read_text(), "keep")

            (source / "dist").unlink()
            (source / "assets").symlink_to(external, target_is_directory=True)
            self.assertFalse(bp.assemble_preview_artifact(str(source), str(source / "dist")))
            self.assertFalse((source / "dist").exists())

    def test_build_preview_never_publishes_credential_json(self):
        import tempfile
        import build_preview as bp

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "project"
            source.mkdir()
            (source / "index.html").write_text("safe")
            (source / "credentials.json").write_text('{"token":"do-not-publish"}')
            output = source / "dist"
            self.assertTrue(bp.assemble_preview_artifact(str(source), str(output)))
            self.assertFalse((output / "credentials.json").exists())

    @patch("deploy_preview.run_cmd")
    def test_remediation_dedupe_uses_exact_marker_and_complete_limit(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps([{
                "number": 222,
                "title": f"unrelated mention {self.commit_sha[:7]}",
                "body": f"prose mentions {self.commit_sha} without the durable marker",
            }]),
            "",
        )
        self.assertEqual(dp.find_existing_remediation_issue(self.commit_sha), (True, None))
        self.assertIn("10000", mock_run.call_args.args[0])

    @patch("deploy_preview.run_cmd")
    def test_remediation_records_conservative_scope_stage_and_run(self, mock_run):
        run_url = "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345"
        mock_run.side_effect = [
            (0, "[]", ""),
            (0, "https://github.com/owner/repo/issues/223\n", ""),
            (0, "Attached", ""),
            (0, "Commented", ""),
        ]
        self.assertEqual(
            dp.file_remediation_issue(
                109,
                self.commit_sha,
                "run remained queued",
                failure_stage="workflow-run",
                run_url=run_url,
            ),
            223,
        )
        create_cmd = mock_run.call_args_list[1].args[0]
        body = create_cmd[create_cmd.index("--body") + 1]
        self.assertIn(dp.REMEDIATION_MARKER.format(commit_sha=self.commit_sha), body)
        self.assertIn("Stage: `workflow-run`", body)
        self.assertIn(run_url, body)
        self.assertIn("touches: .", body)
        self.assertIn("parallel-eligible: false", body)


if __name__ == "__main__":
    unittest.main()
