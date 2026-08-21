"""Fail-closed contract tests for release checkpoint evidence."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_checkpoint as checkpoint  # noqa: E402


SHA = "a" * 40
REPO = "gillella/Aru_Agentic_SDLC"
RUN_ID = 12345
WORKFLOW_ID = 67890
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"


def run_data(**overrides):
    data = {
        "databaseId": RUN_ID,
        "conclusion": "success",
        "event": "workflow_dispatch",
        "workflowDatabaseId": WORKFLOW_ID,
        "url": RUN_URL,
        "headSha": SHA,
    }
    data.update(overrides)
    return data


class RunUrlTests(unittest.TestCase):
    def test_only_canonical_repository_run_url_is_accepted(self):
        self.assertEqual(checkpoint.run_id_from_url(RUN_URL, REPO), RUN_ID)
        for bad in (
            f"http://github.com/{REPO}/actions/runs/{RUN_ID}",
            f"https://github.com/other/repo/actions/runs/{RUN_ID}",
            f"https://github.com/{REPO}/actions/runs/0",
            f"https://github.com/{REPO}/actions/runs/01",
            f"https://github.com/{REPO}/actions/runs/{RUN_ID}?x=1",
            f"https://user@github.com/{REPO}/actions/runs/{RUN_ID}",
        ):
            with self.subTest(url=bad):
                self.assertIsNone(checkpoint.run_id_from_url(bad, REPO))


class CheckpointValidationTests(unittest.TestCase):
    def validate(self, data=None, default_head=SHA, target=SHA, url=RUN_URL):
        with patch.object(
            checkpoint, "_read_checkpoint_run", return_value=data or run_data()
        ), patch.object(
            checkpoint, "resolve_checkpoint_workflow_id", return_value=WORKFLOW_ID
        ), patch.object(
            checkpoint, "resolve_default_head", return_value=default_head
        ):
            return checkpoint.validate_release_checkpoint(url, target, REPO)

    def test_success_binds_run_requested_commit_and_live_default_head(self):
        evidence = self.validate()
        self.assertEqual(evidence["commit_sha"], SHA)
        self.assertEqual(evidence["run_id"], RUN_ID)
        self.assertEqual(evidence["run_url"], RUN_URL)
        self.assertEqual(evidence["workflow_database_id"], WORKFLOW_ID)
        self.assertEqual(
            evidence["workflow_path"], ".github/workflows/ci.yml"
        )

    def test_schedule_is_also_an_authorized_checkpoint_event(self):
        evidence = self.validate(run_data(event="schedule"))
        self.assertEqual(evidence["event"], "schedule")

    def test_missing_or_malformed_identity_fails_closed(self):
        for target, repo in (("short", REPO), (SHA, ""), (SHA, "not-a-repo")):
            with self.subTest(target=target, repo=repo), \
                 self.assertRaises(checkpoint.ReleaseCheckpointError):
                checkpoint.validate_release_checkpoint(RUN_URL, target, repo)

    def test_unreadable_pending_failed_or_cancelled_run_is_refused(self):
        with patch.object(
            checkpoint, "resolve_checkpoint_workflow_id", return_value=WORKFLOW_ID
        ), patch.object(checkpoint, "_read_checkpoint_run", return_value=None):
            with self.assertRaises(checkpoint.ReleaseCheckpointError):
                checkpoint.validate_release_checkpoint(RUN_URL, SHA, REPO)
        for conclusion in (None, "", "failure", "cancelled", "in_progress"):
            with self.subTest(conclusion=conclusion), \
                 self.assertRaises(checkpoint.ReleaseCheckpointError):
                self.validate(run_data(conclusion=conclusion))

    def test_wrong_workflow_identity_event_run_identity_or_url_is_refused(self):
        cases = (
            {"workflowDatabaseId": WORKFLOW_ID + 1},
            {"event": "pull_request"},
            {"databaseId": RUN_ID + 1},
            {"url": f"https://github.com/{REPO}/actions/runs/999"},
        )
        for changes in cases:
            with self.subTest(changes=changes), \
                 self.assertRaises(checkpoint.ReleaseCheckpointError):
                self.validate(run_data(**changes))

    def test_unresolvable_workflow_identity_is_refused(self):
        with patch.object(
            checkpoint, "resolve_checkpoint_workflow_id", return_value=None
        ):
            with self.assertRaises(checkpoint.ReleaseCheckpointError):
                checkpoint.validate_release_checkpoint(RUN_URL, SHA, REPO)

    def test_stale_run_or_noncurrent_target_is_refused(self):
        with self.assertRaises(checkpoint.ReleaseCheckpointError):
            self.validate(run_data(headSha="b" * 40))
        with self.assertRaises(checkpoint.ReleaseCheckpointError):
            self.validate(default_head="b" * 40)
        with self.assertRaises(checkpoint.ReleaseCheckpointError):
            self.validate(default_head="")


class DefaultHeadTests(unittest.TestCase):
    @patch.object(checkpoint, "get_default_branch", return_value="release/v1")
    @patch.object(checkpoint, "run_cmd")
    def test_live_default_head_uses_argv_and_supports_slash_branch(self, run_cmd, _branch):
        run_cmd.return_value = (0, SHA, "")
        self.assertEqual(checkpoint.resolve_default_head(REPO), SHA)
        self.assertEqual(
            run_cmd.call_args.args[0],
            ["gh", "api", f"repos/{REPO}/commits/release%2Fv1", "--jq", ".sha"],
        )
        self.assertFalse(run_cmd.call_args.kwargs["check"])

    @patch.object(checkpoint, "get_default_branch", return_value="main")
    @patch.object(checkpoint, "run_cmd", return_value=(1, "", "network"))
    def test_live_default_head_failure_is_not_a_fallback(self, _run, _branch):
        self.assertEqual(checkpoint.resolve_default_head(REPO), "")


class WorkflowIdentityTests(unittest.TestCase):
    @patch.object(checkpoint, "run_cmd")
    def test_workflow_id_is_resolved_from_ci_file(self, run_cmd):
        run_cmd.return_value = (0, str(WORKFLOW_ID), "")
        self.assertEqual(
            checkpoint.resolve_checkpoint_workflow_id(REPO), WORKFLOW_ID
        )
        self.assertEqual(
            run_cmd.call_args.args[0],
            [
                "gh", "api",
                f"repos/{REPO}/actions/workflows/ci.yml",
                "--jq", ".id",
            ],
        )
        self.assertFalse(run_cmd.call_args.kwargs["check"])

    @patch.object(checkpoint, "run_cmd")
    def test_invalid_workflow_identity_is_not_a_fallback(self, run_cmd):
        for result in ((1, "", "network"), (0, "0", ""), (0, "01", "")):
            with self.subTest(result=result):
                run_cmd.return_value = result
                self.assertIsNone(
                    checkpoint.resolve_checkpoint_workflow_id(REPO)
                )

    @patch.object(checkpoint, "run_cmd")
    def test_run_read_requests_workflow_database_identity(self, run_cmd):
        run_cmd.return_value = (0, json.dumps(run_data()), "")
        self.assertEqual(
            checkpoint._read_checkpoint_run(RUN_ID, REPO), run_data()
        )
        self.assertEqual(
            run_cmd.call_args.args[0],
            [
                "gh", "run", "view", str(RUN_ID), "--json",
                "databaseId,conclusion,event,workflowDatabaseId,url,headSha",
                "--repo", REPO,
            ],
        )
        self.assertFalse(run_cmd.call_args.kwargs["check"])


class ReleaseProcedureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = (ROOT / "docs" / "releases.md").read_text(
            encoding="utf-8"
        )

    def test_documented_commands_include_checkpoint_evidence(self):
        self.assertIn("gh workflow run ci.yml", self.document)
        self.assertIn("--json url,headSha,workflowDatabaseId", self.document)
        self.assertGreaterEqual(self.document.count("--checkpoint-run-url"), 3)
        self.assertGreaterEqual(self.document.count("--commit"), 4)
