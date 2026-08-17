import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import incident_intake as intake  # noqa: E402


SOURCE = "prometheus"
COMPONENT = "checkout-api"
ALERT = "error-rate"
EVIDENCE = "p99 error rate 12% for 10m"
FINGERPRINT = intake.incident_fingerprint(SOURCE, COMPONENT, ALERT)
MARKER = intake.incident_marker(FINGERPRINT)


def _issue(number, status="Backlog", state="OPEN", body=None, extra_labels=None):
    labels = []
    if status:
        labels.append({"name": f"status:{status.lower().replace(' ', '-')}"})
    for name in extra_labels or []:
        labels.append({"name": name})
    return {
        "number": number,
        "title": f"fix(ops): {ALERT} on {COMPONENT}",
        "body": body if body is not None else MARKER,
        "labels": labels,
        "state": state,
    }


class IncidentIntakeTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_case_insensitive(self):
        self.assertEqual(
            intake.incident_fingerprint("Prometheus", "Checkout-API", "Error-Rate"),
            FINGERPRINT,
        )
        self.assertEqual(len(FINGERPRINT), 16)

    def test_validate_identity_rejects_injection(self):
        with self.assertRaises(ValueError):
            intake.validate_identity("bad name", "source")
        with self.assertRaises(ValueError):
            intake.validate_identity("", "component")

    @patch("incident_intake.ensure_label", return_value=True)
    @patch("incident_intake.run_cmd")
    def test_new_alert_creates_governed_issue(self, mock_run, _ensure):
        mock_run.side_effect = [
            (0, "[]", ""),
            (0, "https://github.com/owner/repo/issues/301\n", ""),
            (0, "Attached to board", ""),
            (0, json.dumps([_issue(301)]), ""),
        ]
        new_id = intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE)
        self.assertEqual(new_id, 301)
        list_cmd = mock_run.call_args_list[0].args[0]
        self.assertIn("--search", list_cmd)
        self.assertIn(f"in:body fingerprint={FINGERPRINT}", list_cmd)
        create_cmd = mock_run.call_args_list[1].args[0]
        self.assertEqual(create_cmd[0:3], ["gh", "issue", "create"])
        self.assertIn(MARKER, create_cmd[create_cmd.index("--body") + 1])
        self.assertIn(f"incident-fingerprint: {FINGERPRINT}", create_cmd[create_cmd.index("--body") + 1])
        self.assertIn("pending-ops-triage", create_cmd[create_cmd.index("--body") + 1])
        self.assertIn("origin:incident", create_cmd[create_cmd.index("--label") + 1])
        attach_cmd = mock_run.call_args_list[2].args[0]
        self.assertEqual(attach_cmd[attach_cmd.index("--status") + 1], "Backlog")
        self.assertIn("--require-board", attach_cmd)

    @patch("incident_intake.ensure_label", return_value=True)
    @patch("incident_intake.run_cmd")
    def test_new_alert_board_attach_failure_fails_closed(self, mock_run, _ensure):
        mock_run.side_effect = [
            (0, "[]", ""),
            (0, "https://github.com/owner/repo/issues/302\n", ""),
            (1, "", "Board attachment failed"),
        ]
        self.assertIsNone(intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE))

    @patch("incident_intake.run_cmd", return_value=(1, "", "rate limit"))
    def test_query_failure_does_not_create(self, mock_run):
        self.assertIsNone(intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE))
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(mock_run.call_args.args[0][:3], ["gh", "issue", "list"])
        self.assertIn("--search", mock_run.call_args.args[0])

    @patch("incident_intake.run_cmd")
    def test_recurring_alert_updates_existing_issue(self, mock_run):
        mock_run.side_effect = [
            (0, json.dumps([_issue(199)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Attached", ""),
        ]
        self.assertEqual(
            intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE),
            199,
        )
        comment_cmd = mock_run.call_args_list[2].args[0]
        self.assertEqual(comment_cmd[0:3], ["gh", "issue", "comment"])
        self.assertIn(str(199), comment_cmd)
        self.assertFalse(
            any(call.args[0][:3] == ["gh", "issue", "create"] for call in mock_run.call_args_list)
        )

    @patch("incident_intake.run_cmd")
    def test_recurring_alert_does_not_regress_in_progress(self, mock_run):
        mock_run.side_effect = [
            (0, json.dumps([_issue(199, status="In Progress")]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Attached", ""),
        ]
        self.assertEqual(
            intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE),
            199,
        )
        attach_cmd = mock_run.call_args_list[3].args[0]
        self.assertEqual(attach_cmd[attach_cmd.index("--status") + 1], "In Progress")

    @patch("incident_intake.run_cmd")
    def test_recurring_duplicate_occurrence_is_idempotent(self, mock_run):
        event = intake.event_marker(FINGERPRINT, EVIDENCE)
        mock_run.side_effect = [
            (0, json.dumps([_issue(199, body=f"{MARKER}\n{event}")]), ""),
            (0, "Attached", ""),
        ]
        self.assertEqual(
            intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE),
            199,
        )
        self.assertFalse(
            any(call.args[0][:3] == ["gh", "issue", "comment"] for call in mock_run.call_args_list)
        )

    @patch("incident_intake.run_cmd")
    def test_duplicate_open_markers_keep_oldest(self, mock_run):
        mock_run.side_effect = [
            (0, json.dumps([_issue(200), _issue(199)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Attached", ""),
        ]
        self.assertEqual(
            intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE),
            199,
        )
        self.assertFalse(
            any(call.args[0][:3] == ["gh", "issue", "create"] for call in mock_run.call_args_list)
        )

    @patch("incident_intake.run_cmd")
    def test_resolved_unclaimed_alert_closes_issue_and_prunes(self, mock_run):
        porcelain = (
            "worktree /tmp/repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /tmp/repo/.worktrees/feat-issue-199-ops\n"
            "HEAD def\n"
            "branch refs/heads/feat/issue-199-ops\n"
        )
        mock_run.side_effect = [
            (0, json.dumps([_issue(199)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Moved to Done", ""),
            (0, "", ""),
            (0, "/tmp/repo/.git", ""),
            (0, porcelain, ""),
            (0, "", ""),
            (0, "", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 199)
        close_cmd = mock_run.call_args_list[4].args[0]
        self.assertEqual(close_cmd[:3], ["gh", "issue", "close"])
        prune_cmd = mock_run.call_args_list[8].args[0]
        self.assertEqual(prune_cmd[:3], ["git", "worktree", "remove"])
        self.assertNotIn("--force", prune_cmd)
        self.assertIn("feat-issue-199-ops", prune_cmd[-1])

    @patch("incident_intake.run_cmd")
    def test_resolved_skips_dirty_worktree_without_force(self, mock_run):
        porcelain = (
            "worktree /tmp/repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /tmp/repo/.worktrees/feat-issue-199-ops\n"
            "HEAD def\n"
            "branch refs/heads/feat/issue-199-ops\n"
        )
        mock_run.side_effect = [
            (0, json.dumps([_issue(199)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Moved to Done", ""),
            (0, "", ""),
            (0, "/tmp/repo/.git", ""),
            (0, porcelain, ""),
            (0, " M scripts/foo.py\n", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 199)
        self.assertFalse(
            any(
                call.args[0][:3] == ["git", "worktree", "remove"]
                for call in mock_run.call_args_list
            )
        )

    @patch("incident_intake.run_cmd")
    def test_resolved_in_progress_comments_only(self, mock_run):
        mock_run.side_effect = [
            (0, json.dumps([_issue(199, status="In Progress")]), ""),
            (0, "", ""),
            (0, "Commented", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 199)
        self.assertFalse(
            any(call.args[0][:3] == ["gh", "issue", "close"] for call in mock_run.call_args_list)
        )
        self.assertFalse(
            any(
                call.args[0][:3] == ["git", "worktree", "remove"]
                for call in mock_run.call_args_list
            )
        )
        comment_cmd = mock_run.call_args_list[2].args[0]
        self.assertIn(intake.resolved_marker(FINGERPRINT), comment_cmd[comment_cmd.index("--body") + 1])

    @patch("incident_intake.run_cmd")
    def test_resolved_done_issue_prunes_leftover_worktrees(self, mock_run):
        porcelain = (
            "worktree /tmp/repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /tmp/repo/.worktrees/feat-issue-199-ops\n"
            "HEAD def\n"
            "branch refs/heads/feat/issue-199-ops\n"
        )
        mock_run.side_effect = [
            (0, "[]", ""),
            (0, json.dumps([_issue(199, status="Done", state="CLOSED")]), ""),
            (0, "/tmp/repo/.git", ""),
            (0, porcelain, ""),
            (0, "", ""),
            (0, "", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 199)
        self.assertFalse(
            any(call.args[0][:3] == ["gh", "issue", "comment"] for call in mock_run.call_args_list)
        )
        prune_cmd = mock_run.call_args_list[5].args[0]
        self.assertEqual(prune_cmd[:3], ["git", "worktree", "remove"])
        self.assertNotIn("--force", prune_cmd)
        self.assertIn("feat-issue-199-ops", prune_cmd[-1])

    @patch("incident_intake.run_cmd")
    def test_resolved_with_no_match_is_success(self, mock_run):
        mock_run.side_effect = [
            (0, "[]", ""),
            (0, "[]", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 0)

    @patch("incident_intake.prune_issue_worktrees", return_value=False)
    @patch("incident_intake.run_cmd")
    def test_resolved_prune_failure_fails_closed(self, mock_run, _prune):
        mock_run.side_effect = [
            (0, json.dumps([_issue(199)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
            (0, "Moved to Done", ""),
            (0, "", ""),
        ]
        self.assertIsNone(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE))

    def test_dry_run_does_not_call_github(self):
        with patch("incident_intake.run_cmd") as mock_run:
            code = intake.main(
                [
                    "--signal", "firing",
                    "--source", SOURCE,
                    "--component", COMPONENT,
                    "--alert-name", ALERT,
                    "--evidence", EVIDENCE,
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        mock_run.assert_not_called()

    def test_main_rejects_invalid_identity(self):
        code = intake.main(
            [
                "--signal", "firing",
                "--source", "not a valid source",
                "--component", COMPONENT,
                "--alert-name", ALERT,
            ]
        )
        self.assertEqual(code, 1)

    def test_sanitize_evidence_strips_controls_and_markup(self):
        cleaned = intake.sanitize_evidence("boom <script>\x00alert`x` token=abc123")
        self.assertNotIn("<", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_sanitize_evidence_redacts_common_credentials(self):
        jwt_body = "eyJhbGciOiJub25yZWFsLXNlY3JldA"
        gh_token = "ghp_" + "1234567890abcdefghij"
        aws_key = "AK" + "IA1234567890ABCDEF"
        cleaned = intake.sanitize_evidence(
            f"Authorization: Bearer {jwt_body} {gh_token} {aws_key} credential=super-secret"
        )
        self.assertNotIn(jwt_body, cleaned)
        self.assertNotIn(gh_token, cleaned)
        self.assertNotIn(aws_key, cleaned)
        self.assertNotIn("super-secret", cleaned)
        self.assertIn("[redacted]", cleaned)

    @patch("incident_intake.run_cmd")
    def test_recurrence_skips_board_when_status_unknown(self, mock_run):
        mock_run.side_effect = [
            (0, json.dumps([_issue(199, status=None)]), ""),
            (0, "", ""),
            (0, "Commented", ""),
        ]
        self.assertEqual(
            intake.intake_firing(SOURCE, COMPONENT, ALERT, "p1", EVIDENCE),
            199,
        )
        self.assertFalse(
            any(
                "update_issue_status.py" in " ".join(str(part) for part in call.args[0])
                for call in mock_run.call_args_list
            )
        )

    @patch("incident_intake.run_cmd")
    def test_resolved_open_done_issue_retries_close(self, mock_run):
        porcelain = (
            "worktree /tmp/repo\nHEAD abc\nbranch refs/heads/main\n\n"
            "worktree /tmp/repo/.worktrees/feat-issue-199-ops\n"
            "HEAD def\n"
            "branch refs/heads/feat/issue-199-ops\n"
        )
        mock_run.side_effect = [
            (0, json.dumps([_issue(199, status="Done", state="OPEN")]), ""),
            (0, "Moved to Done", ""),
            (0, "", ""),
            (0, "/tmp/repo/.git", ""),
            (0, porcelain, ""),
            (0, "", ""),
            (0, "", ""),
        ]
        self.assertEqual(intake.intake_resolved(SOURCE, COMPONENT, ALERT, EVIDENCE), 199)
        self.assertTrue(
            any(call.args[0][:3] == ["gh", "issue", "close"] for call in mock_run.call_args_list)
        )

    def test_evidence_file_success_and_missing(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write(EVIDENCE)
            path = handle.name
        try:
            with patch("incident_intake.run_cmd") as mock_run:
                code = intake.main(
                    [
                        "--signal", "firing",
                        "--source", SOURCE,
                        "--component", COMPONENT,
                        "--alert-name", ALERT,
                        "--evidence-file", path,
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0)
            mock_run.assert_not_called()
        finally:
            os.unlink(path)
        missing = intake.main(
            [
                "--signal", "firing",
                "--source", SOURCE,
                "--component", COMPONENT,
                "--alert-name", ALERT,
                "--evidence-file", "/no/such/evidence.txt",
            ]
        )
        self.assertEqual(missing, 1)


if __name__ == "__main__":
    unittest.main()
