# line-ceiling: 1072
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402
import agent_presence as ap  # noqa: E402


_PRESENCE_TEMPORARY = None
_PRESENCE_PATCHER = None


def setUpModule():
    global _PRESENCE_TEMPORARY, _PRESENCE_PATCHER
    _PRESENCE_TEMPORARY = tempfile.TemporaryDirectory()
    _PRESENCE_PATCHER = patch.object(
        ap,
        "DEFAULT_PRESENCE_PATH",
        Path(_PRESENCE_TEMPORARY.name) / "agent-presence.json",
    )
    _PRESENCE_PATCHER.start()


def tearDownModule():
    _PRESENCE_PATCHER.stop()
    _PRESENCE_TEMPORARY.cleanup()


def issue_with_labels(*names, author="owner", number=7):
    record = {
        "number": number,
        "labels": [{"name": name} for name in names],
    }
    if author is not None:
        record["author"] = {"login": author}
    return record


class ClaimProtocolTests(unittest.TestCase):
    def setUp(self):
        self.required_board_preflight = claim_issue._required_board_preflight
        owner = patch.object(
            claim_issue, "repository_owner_login", return_value="owner")
        trusted = patch.object(
            claim_issue, "repository_trusted_logins", return_value={"owner"})
        board = patch.object(
            claim_issue, "_required_board_preflight", return_value=True)
        self.addCleanup(owner.stop)
        self.addCleanup(trusted.stop)
        self.addCleanup(board.stop)
        owner.start()
        trusted.start()
        board.start()

    def test_unreadable_project_preflight_names_required_authority(self):
        stderr = io.StringIO()
        with patch.object(claim_issue, "get_repo_slug", return_value="octocat/widgets"), \
             patch.object(claim_issue, "query_issue_project_items", return_value=None), \
             patch("sys.stderr", stderr):
            self.assertFalse(self.required_board_preflight(7, "In Progress"))
        self.assertIn("read:project", stderr.getvalue())
        self.assertIn("project", stderr.getvalue())
        self.assertIn("claim cannot proceed", stderr.getvalue())

    def test_project_preflight_requires_ready_state_and_target_option(self):
        item = {
            "status": {"name": "Ready"},
            "project": {
                "title": "widgets Board",
                "repositories": {"nodes": [{"nameWithOwner": "octocat/widgets"}]},
                "field": {
                    "id": "STATUS_FIELD",
                    "options": [{"id": "IN_PROGRESS", "name": "In Progress"}],
                },
            },
        }
        with patch.object(claim_issue, "get_repo_slug", return_value="octocat/widgets"), \
             patch.object(claim_issue, "query_issue_project_items", return_value=[item]):
            self.assertTrue(self.required_board_preflight(7, "In Progress"))
            item["status"]["name"] = "Backlog"
            with patch("sys.stderr", io.StringIO()):
                self.assertFalse(self.required_board_preflight(7, "In Progress"))

    @patch.object(claim_issue, "_required_board_preflight", return_value=False)
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_failed_project_preflight_writes_no_claim_state(
        self, get_issue, ensure_label, run_cmd, update_status, _preflight
    ):
        get_issue.return_value = issue_with_labels("status:ready")
        result = claim_issue.claim_issue(7, "agent-a")
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue, "_required_board_preflight", return_value=False)
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_interrupted_claim_preflight_reports_retained_state(
        self, get_issue, ensure_label, run_cmd, update_status, _preflight
    ):
        get_issue.return_value = issue_with_labels(
            "status:ready", "agent:agent-a")
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            result = claim_issue.claim_issue(7, "agent-a")
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()
        self.assertIn("Incomplete claim state retained", stderr.getvalue())
        self.assertIn("agent:agent-a", stderr.getvalue())
        self.assertIn("existing assignee", stderr.getvalue())

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=False)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_failed_board_mutation_rolls_back_and_returns_error(
        self, get_issue, _ensure, run_cmd, _update, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
        ]
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            result = claim_issue.claim_issue(7, "agent-a")
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        commands = [call.args[0] for call in run_cmd.call_args_list]
        self.assertIn(
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-a"],
            commands,
        )
        self.assertIn(
            ["gh", "issue", "edit", "7", "--remove-assignee", "@me"],
            commands,
        )
        self.assertIn("Required Project Board/status mutation failed", stderr.getvalue())
        self.assertIn("read:project", stderr.getvalue())

    def test_cli_propagates_claim_error_exit_code(self):
        with patch.object(claim_issue, "claim_issue", return_value=claim_issue.EXIT_ERROR), \
             patch("sys.argv", ["claim_issue.py", "--issue", "7", "--agent", "agent-a"]):
            with self.assertRaisesRegex(SystemExit, "1"):
                claim_issue.main()
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_direct_claim_refuses_needs_human_issue(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels("status:ready", "needs-human")

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_needs_human_label_race_during_settle_releases_claim(
        self, get_issue, _ensure, run_cmd, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a", "needs-human"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_called_once_with(7, "Backlog", require_board=True)
        self.assertEqual(
            run_cmd.call_args_list[-2].args[0],
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-a"],
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_needs_human_label_race_before_finalize_returns_to_backlog(
        self, get_issue, _ensure, _run, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a", "needs-human"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_called_once_with(7, "Backlog", require_board=True)

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_needs_human_label_race_after_status_returns_to_backlog(
        self, get_issue, _ensure, _run, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:in-progress", "agent:agent-a", "needs-human"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        self.assertEqual(
            update_status.call_args_list,
            [
                call(7, "In Progress", require_board=True),
                call(7, "Backlog", require_board=True),
            ],
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_loser_removes_only_its_agent_label(
        self, get_issue, _ensure_label, run_cmd, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a", "agent:agent-b"),
        ]
        run_cmd.return_value = (0, "", "")

        result = claim_issue.claim_issue(7, "agent-b")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_not_called()
        cleanup = run_cmd.call_args_list[-1].args[0]
        self.assertEqual(
            cleanup,
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-b"],
        )
        self.assertNotIn("status:in-progress", cleanup)
        self.assertNotIn("status:ready", cleanup)

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_successful_claim_requires_board_update(
        self, get_issue, _ensure_label, _run_cmd, update_status, _sleep
    ):
        # pre-check + settle reads + pre-finalize revalidation + post-status verify
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("agent:agent-a", "status:in-progress"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_OK)
        self.assertEqual(
            update_status.call_args,
            call(7, "In Progress", require_board=True),
        )
        self.assertEqual(_sleep.call_count, claim_issue.SETTLE_ROUNDS + 1)

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_late_smaller_contender_detected_on_second_settle(
        self, get_issue, _ensure_label, run_cmd, update_status, _sleep
    ):
        """Larger agent sees only itself first; smaller label arrives before confirm."""
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-b"),
            issue_with_labels(
                "status:ready", "agent:agent-a", "agent:agent-b"
            ),
        ]

        result = claim_issue.claim_issue(7, "agent-b")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_not_called()
        cleanup = run_cmd.call_args_list[-1].args[0]
        self.assertEqual(
            cleanup,
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-b"],
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_late_contender_after_status_update_rolls_back(
        self, get_issue, _ensure_label, run_cmd, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-b"),
            issue_with_labels("status:ready", "agent:agent-b"),
            issue_with_labels("status:ready", "agent:agent-b"),
            # post-status: smaller agent landed
            issue_with_labels("agent:agent-a", "agent:agent-b", "status:in-progress"),
        ]

        result = claim_issue.claim_issue(7, "agent-b")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        self.assertEqual(
            update_status.call_args_list[0],
            call(7, "In Progress", require_board=True),
        )
        self.assertEqual(
            update_status.call_args_list[1],
            call(7, "Ready", require_board=True),
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_interrupted_claim_is_completed_on_retry(
        self, get_issue, _ensure_label, run_cmd, update_status, _sleep
    ):
        # Label present but status still Ready (crash between label and status).
        get_issue.side_effect = [
            issue_with_labels("agent:agent-a", "status:ready"),
            issue_with_labels("agent:agent-a", "status:ready"),
            issue_with_labels("agent:agent-a", "status:in-progress"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_OK)
        update_status.assert_called_once_with(7, "In Progress", require_board=True)
        # Must not re-add the claim label on the completion path.
        add_label_calls = [
            c for c in run_cmd.call_args_list
            if c.args and "--add-label" in c.args[0]
        ]
        self.assertEqual(add_label_calls, [])

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_completed_same_agent_claim_returns_without_rewriting(
        self, get_issue, _ensure_label, run_cmd, update_status, _sleep
    ):
        get_issue.return_value = issue_with_labels(
            "agent:agent-a", "status:in-progress"
        )

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_OK)
        update_status.assert_not_called()
        run_cmd.assert_not_called()

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_parked_in_review_issue_cannot_be_reclaimed(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels(
            "status:in-review", "agent:agent-1"
        )

        result = claim_issue.claim_issue(39, "agent-0")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_ready_issue_parked_during_settle_cannot_be_reopened(
        self, get_issue, _ensure, run_cmd, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels(
                "status:in-review", "agent:agent-0", "agent:agent-1"
            ),
        ]

        result = claim_issue.claim_issue(39, "agent-0")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_not_called()
        self.assertEqual(
            run_cmd.call_args_list[-1].args[0],
            ["gh", "issue", "edit", "39", "--remove-label", "agent:agent-0"],
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_ready_issue_parked_before_finalize_cannot_be_reopened(
        self, get_issue, _ensure, run_cmd, update_status, _sleep
    ):
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-0"),
            issue_with_labels("status:ready", "agent:agent-0"),
            issue_with_labels(
                "status:in-review", "agent:agent-0", "agent:agent-1"
            ),
        ]

        result = claim_issue.claim_issue(39, "agent-0")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_not_called()
        self.assertEqual(
            run_cmd.call_args_list[-1].args[0],
            ["gh", "issue", "edit", "39", "--remove-label", "agent:agent-0"],
        )

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_stale_same_agent_label_on_backlog_cannot_resume(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels(
            "status:backlog", "agent:agent-0"
        )

        result = claim_issue.claim_issue(39, "agent-0")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "get_issue")
    def test_release_returns_needs_human_issue_to_backlog(
        self, get_issue, _run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels(
            "status:in-progress", "agent:agent-a", "needs-human"
        )

        result = claim_issue.release_issue(7, "agent-a")

        self.assertIsNone(result)
        update_status.assert_called_once_with(7, "Backlog", require_board=True)

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_ambiguous_ready_and_in_review_status_fails_closed(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels(
            "status:ready", "status:in-review", "agent:agent-1"
        )

        result = claim_issue.claim_issue(39, "agent-0")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()
        self.assertEqual(
            claim_issue._status_name(get_issue.return_value),
            "ambiguous(in-review,ready)",
        )

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_direct_claim_refuses_untrusted_author(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels("status:ready", author="attacker")

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_direct_claim_refuses_authorless_issue(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        get_issue.return_value = issue_with_labels("status:ready", author=None)

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_trusted_rewrite_label_allows_outsider_claim(
        self, get_issue, _ensure, _run, update_status, _sleep
    ):
        rewritten = issue_with_labels(
            "status:ready", "trusted-rewrite", author="attacker")
        claimed = issue_with_labels(
            "status:ready", "trusted-rewrite", "agent:agent-a", author="attacker")
        in_progress = issue_with_labels(
            "status:in-progress", "trusted-rewrite", "agent:agent-a",
            author="attacker")
        get_issue.side_effect = [
            rewritten, claimed, claimed, claimed, in_progress,
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_OK)
        update_status.assert_called_once_with(7, "In Progress", require_board=True)

    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd")
    @patch.object(claim_issue, "ensure_label")
    @patch.object(claim_issue, "get_issue")
    def test_direct_claim_refuses_unresolved_trust_identity(
        self, get_issue, ensure_label, run_cmd, update_status
    ):
        record = issue_with_labels(
            "status:ready", "trusted-rewrite", author="attacker")
        record["trustIdentityResolved"] = False
        get_issue.return_value = record

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_finalize_revalidates_and_rolls_back_untrusted_rewrite(
        self, get_issue, _ensure, run_cmd, update_status, _sleep
    ):
        ready = issue_with_labels("status:ready")
        labeled = issue_with_labels("status:ready", "agent:agent-a")
        untrusted = issue_with_labels(
            "status:ready", "agent:agent-a", author="attacker")
        get_issue.side_effect = [
            ready, labeled, labeled, untrusted,
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        update_status.assert_called_once_with(7, "Ready", require_board=True)
        self.assertEqual(
            run_cmd.call_args_list[-2].args[0],
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-a"],
        )

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "update_status", return_value=True)
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_post_status_read_revalidates_metadata_trust(
        self, get_issue, _ensure, run_cmd, update_status, _sleep
    ):
        ready = issue_with_labels("status:ready")
        labeled = issue_with_labels("status:ready", "agent:agent-a")
        untrusted = issue_with_labels(
            "status:in-progress", "agent:agent-a", author="attacker")
        get_issue.side_effect = [
            ready, labeled, labeled, labeled, untrusted,
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        self.assertEqual(
            update_status.call_args_list,
            [
                call(7, "In Progress", require_board=True),
                call(7, "Ready", require_board=True),
            ],
        )


class InReviewHandoffStatusTests(unittest.TestCase):
    @patch("update_issue_status.run_cmd", return_value=(0, "", ""))
    @patch("update_issue_status.set_board_status", return_value=True)
    @patch("update_issue_status.get_issue")
    def test_update_status_retains_authorship_backstop_when_moving_to_in_review(
        self, mock_get_issue, _mock_set_board, mock_run_cmd
    ):
        import update_issue_status

        mock_get_issue.return_value = issue_with_labels(
            "status:in-progress", "agent:agent-1"
        )

        res = update_issue_status.update_status(7, "In Review")

        self.assertTrue(res)
        cmd = mock_run_cmd.call_args[0][0]
        self.assertIn("--add-label", cmd)
        self.assertIn("status:in-review", cmd)
        self.assertIn("--remove-label", cmd)
        self.assertIn("status:in-progress", cmd)
        self.assertNotIn("agent:agent-1", cmd)


class ClaimAgeReaperTests(unittest.TestCase):
    OLD = "2020-01-01T00:00:00Z"

    @staticmethod
    def _pr(number, label, *, reviews=None, updated_at=None):
        pr = {
            "number": number,
            "labels": [{"name": label}],
            "reviews": reviews or [],
        }
        if updated_at is not None:
            pr["updatedAt"] = updated_at
        return pr

    @staticmethod
    def _timeline(label, created_at):
        return [{
            "event": "labeled",
            "label": {"name": label},
            "created_at": created_at,
        }]

    @staticmethod
    def _list_result(prs):
        return 0, json.dumps(prs), ""

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_pr_activity_does_not_protect_old_review_claim(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            self._list_result([
                self._pr(247, "reviewer:dead", updated_at=recent),
            ]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline("reviewer:dead", self.OLD)

        self.assertEqual(claim_issue.reap_stale_reviews(4), [247])
        self.assertNotIn("updatedAt", run_cmd.call_args_list[0].args[0][-1])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_review_claim_survives_idle_pr(self, run_cmd, fetch_timeline):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.return_value = self._list_result([
            self._pr(8, "reviewer:busy", updated_at=self.OLD),
        ])
        fetch_timeline.return_value = self._timeline("reviewer:busy", recent)

        self.assertEqual(claim_issue.reap_stale_reviews(4), [])
        self.assertEqual(run_cmd.call_count, 1)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_review_after_old_claim_exempts(
        self, run_cmd, fetch_timeline
    ):
        claim_time = "2020-01-02T00:00:00Z"
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.return_value = self._list_result([
            self._pr(
                9,
                "reviewer:done",
                reviews=[{"submittedAt": recent}],
            ),
        ])
        fetch_timeline.return_value = self._timeline("reviewer:done", claim_time)

        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_advisory_review_does_not_exempt_old_claim(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            self._list_result([
                self._pr(
                    17,
                    "reviewer:abandoned",
                    reviews=[{
                        "author": {"login": "coderabbitai[bot]"},
                        "submittedAt": recent,
                    }],
                ),
            ]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline(
            "reviewer:abandoned", self.OLD
        )

        self.assertEqual(claim_issue.reap_stale_reviews(4), [17])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_non_advisory_review_still_exempts_old_claim(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.return_value = self._list_result([
            self._pr(
                20,
                "reviewer:done",
                reviews=[{
                    "author": {"login": "peer-reviewer"},
                    "submittedAt": recent,
                }],
            ),
        ])
        fetch_timeline.return_value = self._timeline("reviewer:done", self.OLD)

        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_completed_legacy_review_claim_is_cleared_immediately(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            self._list_result([{
                "number": 21,
                "labels": [
                    {"name": "reviewer:done"},
                    {"name": "reviewed-by:done"},
                ],
                "reviews": [{"submittedAt": recent}],
            }]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline("reviewer:done", self.OLD)

        self.assertEqual(claim_issue.reap_stale_reviews(4), [21])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_old_post_claim_review_does_not_preserve_claim_forever(
        self, run_cmd, fetch_timeline
    ):
        run_cmd.side_effect = [
            self._list_result([
                self._pr(
                    18,
                    "reviewer:crashed-after-review",
                    reviews=[{"submittedAt": "2020-01-03T00:00:00Z"}],
                ),
            ]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline(
            "reviewer:crashed-after-review", "2020-01-02T00:00:00Z"
        )

        self.assertEqual(claim_issue.reap_stale_reviews(4), [18])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_review_before_claim_does_not_exempt(self, run_cmd, fetch_timeline):
        run_cmd.side_effect = [
            self._list_result([
                self._pr(
                    10,
                    "reviewer:abandoned",
                    reviews=[{"submittedAt": "2020-01-01T00:00:00Z"}],
                ),
            ]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline(
            "reviewer:abandoned", "2020-01-02T00:00:00Z"
        )

        self.assertEqual(claim_issue.reap_stale_reviews(4), [10])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_timeline_failure_aborts_all_review_reaping_before_mutation(
        self, run_cmd, fetch_timeline
    ):
        run_cmd.return_value = self._list_result([
            self._pr(11, "reviewer:first"),
            self._pr(12, "reviewer:unknown"),
        ])
        fetch_timeline.side_effect = [
            self._timeline("reviewer:first", self.OLD),
            None,
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_reviews(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 1)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_missing_exact_label_event_aborts_review_reaping(
        self, run_cmd, fetch_timeline
    ):
        run_cmd.return_value = self._list_result([
            self._pr(13, "reviewer:exact"),
        ])
        fetch_timeline.return_value = self._timeline(
            "reviewer:someone-else", self.OLD
        )

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_reviews(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 1)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_latest_matching_label_event_wins(self, run_cmd, fetch_timeline):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.return_value = self._list_result([
            self._pr(14, "reviewer:repeat"),
        ])
        fetch_timeline.return_value = [
            *self._timeline("reviewer:repeat", self.OLD),
            *self._timeline("reviewer:other", self.OLD),
            *self._timeline("reviewer:repeat", recent),
        ]

        self.assertEqual(claim_issue.reap_stale_reviews(4), [])
        self.assertEqual(run_cmd.call_count, 1)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_reacquired_claim_is_not_removed_from_old_snapshot(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.return_value = self._list_result([
            self._pr(19, "reviewer:renewed"),
        ])
        fetch_timeline.side_effect = [
            self._timeline("reviewer:renewed", self.OLD),
            [
                *self._timeline("reviewer:renewed", self.OLD),
                *self._timeline("reviewer:renewed", recent),
            ],
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_reviews(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 1)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_merge_reaper_uses_each_claim_age(self, run_cmd, fetch_timeline):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            self._list_result([
                self._pr(15, "merger:stale", updated_at=recent),
                self._pr(16, "merger:busy", updated_at=self.OLD),
            ]),
            self._list_result([]),
            (0, "", ""),
        ]

        def timeline(endpoint):
            if endpoint.endswith("/15/timeline"):
                return self._timeline("merger:stale", self.OLD)
            return self._timeline("merger:busy", recent)

        fetch_timeline.side_effect = timeline

        self.assertEqual(claim_issue.reap_stale_merges(4), [15])
        self.assertNotIn("updatedAt", run_cmd.call_args_list[0].args[0][-1])

    @patch.object(claim_issue, "fetch_paginated_gh_api", return_value=None)
    @patch.object(claim_issue, "run_cmd")
    def test_merge_timeline_failure_reaps_nothing(self, run_cmd, _fetch_timeline):
        run_cmd.side_effect = [
            self._list_result([self._pr(17, "merger:unknown")]),
            self._list_result([]),
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_merges(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 2)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_zero_threshold_makes_no_api_calls(self, run_cmd, fetch_timeline):
        self.assertEqual(claim_issue.reap_stale_reviews(0), [])
        self.assertEqual(claim_issue.reap_stale_merges(0), [])
        run_cmd.assert_not_called()
        fetch_timeline.assert_not_called()


class DummyPresenceStore:
    def __init__(self, records=None, error=None, ttl_seconds=300):
        self.records = records or {}
        self.error = error
        self.heartbeat_ttl_seconds = ttl_seconds

    def get(self, agent_id):
        if self.error:
            raise self.error
        return self.records.get(agent_id)


class DummyRecord:
    def __init__(self, agent_id, availability="available", last_heartbeat=""):
        self.agent_id = agent_id
        self.availability = availability
        self.last_heartbeat = last_heartbeat


class ClaimIssueTests(unittest.TestCase):
    def test_review_claim_conflict_message_references_review_pool(self):
        with patch("sys.stderr") as stderr:
            code = claim_issue.claim_review(17, "codex-review-pool")
        self.assertEqual(code, claim_issue.EXIT_CONFLICT)
        self.assertIn("review-pool", "".join(call.args[0] for call in stderr.write.call_args_list).lower())

    def test_absent_agent_reduced_reap_threshold(self):
        store = DummyPresenceStore(records={})
        hours, reason = claim_issue._effective_reap_threshold("absent-agent", 4, store=store)
        self.assertEqual(hours, 2.0)
        self.assertIn("absent from presence registry", reason)

    def test_live_agent_full_reap_threshold(self):
        now = datetime.now(timezone.utc)
        fresh_hb = now.isoformat().replace("+00:00", "Z")
        store = DummyPresenceStore(records={
            "live-agent": DummyRecord("live-agent", availability="available", last_heartbeat=fresh_hb)
        })
        hours, reason = claim_issue._effective_reap_threshold("live-agent", 4, store=store, now=now)
        self.assertEqual(hours, 4.0)
        self.assertEqual(reason, "live agent")

    def test_recent_claim_never_reaped(self):
        store = DummyPresenceStore(records={})
        hours, _ = claim_issue._effective_reap_threshold("absent-agent", 4, store=store)
        # 0.5h claim is younger than the 2.0h reduced threshold
        self.assertLess(0.5, hours)

    def test_missing_presence_fallback(self):
        store = DummyPresenceStore(error=RuntimeError("disk unreadable"))
        import io
        fake_stderr = io.StringIO()
        with patch("sys.stderr", fake_stderr):
            hours, reason = claim_issue._effective_reap_threshold("any-agent", 4, store=store)
        self.assertEqual(hours, 4.0)
        self.assertIn("fallback", reason)
        self.assertIn("[WARN]", fake_stderr.getvalue())

    def test_reduced_threshold_floor_1h(self):
        store = DummyPresenceStore(records={})
        # Base 1.5h -> half is 0.75h -> floored at 1.0h
        hours, _ = claim_issue._effective_reap_threshold("absent-agent", 1.5, store=store)
        self.assertEqual(hours, 1.0)
        # Base 1.0h -> half is 0.5h -> floored at 1.0h
        hours_one, _ = claim_issue._effective_reap_threshold("absent-agent", 1.0, store=store)
        self.assertEqual(hours_one, 1.0)

    def test_reap_output_explains_threshold(self):
        import io
        fake_stderr = io.StringIO()
        store = DummyPresenceStore(records={})
        now = datetime.now(timezone.utc)
        claimed_at = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")

        with patch("sys.stderr", fake_stderr), \
             patch.object(claim_issue, "run_cmd") as mock_cmd, \
             patch.object(claim_issue, "fetch_paginated_gh_api") as mock_timeline:
            mock_cmd.side_effect = [
                (0, json.dumps([{"number": 42, "labels": [{"name": "reviewer:absent-agent"}], "reviews": []}]), ""),
                (0, "", ""),
            ]
            mock_timeline.return_value = [{"event": "labeled", "label": {"name": "reviewer:absent-agent"}, "created_at": claimed_at}]
            released = claim_issue.reap_stale_reviews(4, presence_store=store, now=now)
            self.assertEqual(released, [42])
            output = fake_stderr.getvalue()
            self.assertIn("Released stale review claim on PR #42", output)
            self.assertIn("absent from presence registry", output)
            self.assertIn("claim age > 2h", output)


if __name__ == "__main__":
    unittest.main()
