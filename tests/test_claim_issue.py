# +59 for the #344 terminal merge lease tests.
# +7 for the #410 fixed-quiet-threshold recovery tests.
# line-ceiling: 1203
import io
import json
import sys
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402


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
        owner = patch.object(claim_issue, "repository_owner_login", return_value="owner")
        trusted = patch.object(claim_issue, "repository_trusted_logins", return_value={"owner"})
        board = patch.object(claim_issue, "_required_board_preflight", return_value=True)
        self.enterContext(patch.object(claim_issue.merge_pr, "repository_merge_lock",
            return_value=nullcontext((True, "locked"))))
        self.addCleanup(owner.stop)
        self.addCleanup(trusted.stop)
        self.addCleanup(board.stop)
        owner.start()
        trusted.start()
        board.start()

    @staticmethod
    def _board_item():
        return {
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

    def _assert_preflight_blocks_claim(self, items):
        stderr = io.StringIO()
        with patch.object(
            claim_issue,
            "_required_board_preflight",
            side_effect=self.required_board_preflight,
        ), patch.object(
            claim_issue, "get_repo_slug", return_value="octocat/widgets"
        ), patch.object(
            claim_issue, "query_issue_project_items", return_value=items
        ), patch.object(
            claim_issue, "get_issue", return_value=issue_with_labels("status:ready")
        ), patch.object(
            claim_issue, "ensure_label"
        ) as ensure_label, patch.object(
            claim_issue, "run_cmd"
        ) as run_cmd, patch.object(
            claim_issue, "update_status"
        ) as update_status, patch(
            "sys.stderr", stderr
        ):
            result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_ERROR)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()
        update_status.assert_not_called()
        return stderr.getvalue()

    def _claim_with_failed_board_update(self, label_cleanup, assignee_cleanup):
        snapshots = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
            issue_with_labels("status:ready", "agent:agent-a"),
        ]
        stderr = io.StringIO()
        with patch.object(claim_issue.time, "sleep"), patch.object(
            claim_issue, "update_status", return_value=False
        ), patch.object(
            claim_issue, "run_cmd"
        ) as run_cmd, patch.object(
            claim_issue, "ensure_label", return_value=True
        ), patch.object(
            claim_issue, "get_issue", side_effect=snapshots
        ), patch(
            "sys.stderr", stderr
        ):
            def command_result(command, **_kwargs):
                if "--remove-label" in command:
                    return label_cleanup
                if "--remove-assignee" in command:
                    return assignee_cleanup
                return 0, "", ""

            run_cmd.side_effect = command_result
            result = claim_issue.claim_issue(7, "agent-a")

        commands = [record.args[0] for record in run_cmd.call_args_list]
        return result, commands, stderr.getvalue()

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
        item = self._board_item()
        with patch.object(claim_issue, "get_repo_slug", return_value="octocat/widgets"), \
             patch.object(claim_issue, "query_issue_project_items", return_value=[item]):
            self.assertTrue(self.required_board_preflight(7, "In Progress"))
            item["status"]["name"] = "Backlog"
            with patch("sys.stderr", io.StringIO()):
                self.assertFalse(self.required_board_preflight(7, "In Progress"))

    def test_project_preflight_rejects_zero_or_multiple_governed_items(self):
        item = self._board_item()
        for items in ([], [item, self._board_item()]):
            with self.subTest(project_item_count=len(items)):
                error = self._assert_preflight_blocks_claim(items)
                self.assertIn("exactly one governed Project Board item", error)

    def test_project_preflight_rejects_missing_status_contract(self):
        no_field = self._board_item()
        no_field["project"]["field"] = None
        no_target = self._board_item()
        no_target["project"]["field"]["options"] = [{"id": "READY", "name": "Ready"}]
        for case, item in (("missing field", no_field), ("missing target", no_target)):
            with self.subTest(case=case):
                error = self._assert_preflight_blocks_claim([item])
                self.assertIn("no readable Status option 'In Progress'", error)

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

    def test_failed_board_mutation_rolls_back_and_returns_error(self):
        result, commands, stderr = self._claim_with_failed_board_update(
            (0, "", ""), (0, "", "")
        )
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        self.assertIn(
            ["gh", "issue", "edit", "7", "--remove-label", "agent:agent-a"],
            commands,
        )
        self.assertIn(
            ["gh", "issue", "edit", "7", "--remove-assignee", "@me"],
            commands,
        )
        self.assertIn("Required Project Board/status mutation failed", stderr)
        self.assertIn("read:project", stderr)

    def test_failed_board_mutation_reports_failed_label_cleanup(self):
        result, commands, stderr = self._claim_with_failed_board_update(
            (1, "", "label cleanup denied"), (0, "", "")
        )
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        self.assertTrue(any("--remove-label" in command for command in commands))
        self.assertTrue(any("--remove-assignee" in command for command in commands))
        self.assertIn("Claim rollback incomplete", stderr)
        self.assertIn("manual reconciliation required", stderr)
        self.assertIn("claim label cleanup failed", stderr)

    def test_failed_board_mutation_reports_failed_assignee_cleanup(self):
        result, commands, stderr = self._claim_with_failed_board_update(
            (0, "", ""), (1, "", "assignee cleanup denied")
        )
        self.assertEqual(result, claim_issue.EXIT_ERROR)
        self.assertTrue(any("--remove-label" in command for command in commands))
        self.assertTrue(any("--remove-assignee" in command for command in commands))
        self.assertIn("Claim rollback incomplete", stderr)
        self.assertIn("manual reconciliation required", stderr)
        self.assertIn("assignee cleanup denied", stderr)

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
    @patch.object(claim_issue, "update_status")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "get_issue")
    def test_settle_rollback_unassigns_the_caller_supplied_assignee(
        self, get_issue, _ensure, run_cmd, _update_status, _sleep
    ):
        """A custom --assignee must be undone by the same name it was made with.

        Rolling back with "@me" leaves the operator-only issue assigned to
        whoever the claim named, so board ownership and GitHub assignment
        disagree on an issue no agent may hold.
        """
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("status:ready", "agent:agent-a", "needs-human"),
        ]

        result = claim_issue.claim_issue(7, "agent-a", assignee="octocat")

        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        self.assertEqual(
            run_cmd.call_args_list[-1].args[0],
            ["gh", "issue", "edit", "7", "--remove-assignee", "octocat"],
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

    @staticmethod
    def _merged_list_pair(prs):
        """`reap_stale_merges` scans open PRs and then merged ones."""
        return [(0, json.dumps(prs), ""), (0, "[]", "")]

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_pr_activity_does_not_protect_old_merge_claim(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            *self._merged_list_pair([
                self._pr(247, "merger:dead", updated_at=recent),
            ]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline("merger:dead", self.OLD)

        self.assertEqual(claim_issue.reap_stale_merges(4), [247])
        self.assertNotIn("updatedAt", run_cmd.call_args_list[0].args[0][-1])

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_recent_merge_claim_survives_idle_pr(self, run_cmd, fetch_timeline):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(8, "merger:busy", updated_at=self.OLD),
        ])
        fetch_timeline.return_value = self._timeline("merger:busy", recent)

        self.assertEqual(claim_issue.reap_stale_merges(4), [])
        self.assertEqual(run_cmd.call_count, 2)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_timeline_failure_aborts_all_reaping_before_mutation(
        self, run_cmd, fetch_timeline
    ):
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(11, "merger:first"),
            self._pr(12, "merger:unknown"),
        ])
        fetch_timeline.side_effect = [
            self._timeline("merger:first", self.OLD),
            None,
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_merges(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 2)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_missing_exact_label_event_aborts_reaping(
        self, run_cmd, fetch_timeline
    ):
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(13, "merger:exact"),
        ])
        fetch_timeline.return_value = self._timeline(
            "merger:someone-else", self.OLD
        )

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_merges(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 2)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_latest_matching_label_event_wins(self, run_cmd, fetch_timeline):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(14, "merger:repeat"),
        ])
        fetch_timeline.return_value = [
            *self._timeline("merger:repeat", self.OLD),
            *self._timeline("merger:other", self.OLD),
            *self._timeline("merger:repeat", recent),
        ]

        self.assertEqual(claim_issue.reap_stale_merges(4), [])
        self.assertEqual(run_cmd.call_count, 2)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_reacquired_claim_is_not_removed_from_old_snapshot(
        self, run_cmd, fetch_timeline
    ):
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(19, "merger:renewed"),
        ])
        fetch_timeline.side_effect = [
            self._timeline("merger:renewed", self.OLD),
            [
                *self._timeline("merger:renewed", self.OLD),
                *self._timeline("merger:renewed", recent),
            ],
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_merges(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 2)
        self.assertTrue(stderr.write.called)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_claim_reacquired_after_revalidation_is_not_removed(
        self, run_cmd, fetch_timeline
    ):
        """The sweep re-proves each claim in the instant before removing it.

        Revalidation covers the whole set at preflight time; the removal
        happens later, so a claim released and re-acquired in between would
        otherwise be reaped on the strength of the older observation.
        """
        recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = self._merged_list_pair([
            self._pr(21, "merger:renewed"),
        ])
        fetch_timeline.side_effect = [
            self._timeline("merger:renewed", self.OLD),
            self._timeline("merger:renewed", self.OLD),
            [
                *self._timeline("merger:renewed", self.OLD),
                *self._timeline("merger:renewed", recent),
            ],
        ]

        with patch("sys.stderr") as stderr:
            result = claim_issue.reap_stale_merges(4)

        self.assertEqual(result, [])
        self.assertEqual(run_cmd.call_count, 2)
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
        self.assertEqual(claim_issue.reap_stale_merges(0), [])
        run_cmd.assert_not_called()
        fetch_timeline.assert_not_called()


class RetiredReviewClaimSurfaceTests(unittest.TestCase):
    """#414: this helper claims issues and merges; review is not its business."""

    def test_no_review_claim_completion_or_reaping_api_remains(self):
        for name in ("claim_review", "release_review", "complete_review",
                     "reap_stale_reviews", "review_claimant", "reviewed_by",
                     "reviewer_labels", "_remove_reviewer_label",
                     "REVIEWER_LABEL_PREFIX", "REVIEWED_BY_LABEL_PREFIX",
                     "REVIEWER_FAMILY_LABEL_PREFIX",
                     "AGENT_REVIEW_ATTESTATION_VERSION"):
            self.assertFalse(hasattr(claim_issue, name),
                             f"claim_issue still exposes {name}")

    def test_cli_refuses_a_bare_pr_instead_of_silently_claiming_review(self):
        argv = ["claim_issue.py", "--pr", "17", "--agent", "codex-1"]
        with patch.object(sys, "argv", argv), \
                patch("sys.stderr") as stderr, \
                self.assertRaises(SystemExit) as exit_info:
            claim_issue.main()
        self.assertEqual(exit_info.exception.code, claim_issue.EXIT_ERROR)
        message = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("--adopt", message)
        self.assertIn("--merge", message)

    def test_cli_rejects_the_retired_review_completion_flags(self):
        for flag in ("--complete-review", "--review-disposition"):
            argv = ["claim_issue.py", "--pr", "17", "--agent", "codex-1", flag]
            with patch.object(sys, "argv", argv), \
                    patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as exit_info:
                claim_issue.main()
            self.assertEqual(exit_info.exception.code, 2)


class QuietThresholdReapTests(unittest.TestCase):
    """#410: recovery reads GitHub timestamps against one fixed threshold.

    The reaper used to shorten its own window for an agent the local presence
    registry could not vouch for, which made the release deadline depend on
    unauthoritative local state that no longer exists.
    """

    OLD = "2020-01-01T00:00:00Z"

    @staticmethod
    def _pr(number, label):
        return {"number": number, "labels": [{"name": label}], "reviews": []}

    @classmethod
    def _merged_list_pair(cls, prs):
        return [(0, json.dumps(prs), ""), (0, "[]", "")]

    @staticmethod
    def _timeline(label, created_at):
        return [{
            "event": "labeled",
            "label": {"name": label},
            "created_at": created_at,
        }]

    def test_threshold_label_renders_whole_and_fractional_hours(self):
        self.assertEqual(claim_issue._quiet_threshold_label(4), "4h")
        self.assertEqual(claim_issue._quiet_threshold_label(1.5), "1.5h")

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_claim_younger_than_the_full_threshold_survives(
        self, run_cmd, fetch_timeline
    ):
        now = datetime.now(timezone.utc)
        claimed_at = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = self._merged_list_pair(
            [self._pr(42, "merger:unknown-agent")]
        )
        fetch_timeline.return_value = self._timeline(
            "merger:unknown-agent", claimed_at
        )

        # A 3h-old claim is stale only under the halved presence threshold.
        self.assertEqual(claim_issue.reap_stale_merges(4, now=now), [])
        self.assertEqual(run_cmd.call_count, 2)

    @patch.object(claim_issue, "fetch_paginated_gh_api")
    @patch.object(claim_issue, "run_cmd")
    def test_merge_reaper_names_the_configured_threshold(
        self, run_cmd, fetch_timeline
    ):
        now = datetime.now(timezone.utc)
        claimed_at = (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        run_cmd.side_effect = [
            *self._merged_list_pair([self._pr(43, "merger:gone")]),
            (0, "", ""),
        ]
        fetch_timeline.return_value = self._timeline("merger:gone", claimed_at)

        fake_stderr = io.StringIO()
        with patch("sys.stderr", fake_stderr):
            released = claim_issue.reap_stale_merges(4, now=now)

        self.assertEqual(released, [43])
        output = fake_stderr.getvalue()
        self.assertIn("Released stale merge claim on PR #43", output)
        self.assertIn("claim age > 4h quiet threshold", output)

    @patch.object(claim_issue, "fetch_paginated_gh_api", return_value=None)
    @patch.object(claim_issue, "run_cmd")
    def test_unreadable_claim_history_reaps_nothing(self, run_cmd, _timeline):
        run_cmd.side_effect = self._merged_list_pair(
            [self._pr(44, "merger:gone")]
        )
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(claim_issue.reap_stale_merges(4), [])
        self.assertEqual(run_cmd.call_count, 2)

    def test_claim_recovery_imports_no_presence_or_metrics_runtime(self):
        source = (
            Path(claim_issue.__file__).read_text(encoding="utf-8").lower()
        )
        for banned in ("agent_presence", "factory_metrics", "heartbeat", "presence"):
            self.assertNotIn(banned, source)

    def test_generic_github_helpers_come_from_retained_common_code(self):
        import common

        self.assertIs(claim_issue.fetch_paginated_gh_api,
                      common.fetch_paginated_gh_api)
        self.assertIs(claim_issue.parse_iso, common.parse_iso)


if __name__ == "__main__":
    unittest.main()


class TerminalLeaseClaimTests(unittest.TestCase):
    """#344: an issue closed by a governed merge cannot be re-claimed."""

    @staticmethod
    def _gh(issue_state, pr_state):
        def fake(cmd):
            if "issue" in cmd:
                return {"state": issue_state,
                        "closedByPullRequestsReferences": [{"number": 89}]}
            return {"state": pr_state, "headRefName": "fix/issue-87-x"}
        return fake

    def test_issue_closed_by_a_merged_pr_is_refused(self):
        with patch.object(claim_issue, "run_gh_json", side_effect=self._gh("CLOSED", "MERGED")), \
             patch.object(claim_issue, "terminal_merge_lease", return_value=None):
            self.assertIsNotNone(claim_issue._terminally_merged(87))

    def test_open_issue_is_not_terminally_merged(self):
        with patch.object(claim_issue, "run_gh_json", side_effect=self._gh("OPEN", "MERGED")):
            self.assertIsNone(claim_issue._terminally_merged(87))

    def test_closed_without_a_merged_pr_is_not_terminal(self):
        with patch.object(claim_issue, "run_gh_json", side_effect=self._gh("CLOSED", "CLOSED")):
            self.assertIsNone(claim_issue._terminally_merged(87))

    LEASED = ["author:agent-a", "terminal-lease:abcdef123456"]

    def test_merge_claim_is_refused_when_the_pr_really_is_merged(self):
        """Criterion 4: the lease blocks continuation of a merged claim."""
        with patch.object(claim_issue, "run_gh_json", return_value={"state": "MERGED"}):
            self.assertEqual(
                claim_issue._terminal_lease_conflict(89, self.LEASED),
                claim_issue.EXIT_CONFLICT,
            )

    def test_a_forged_lease_label_cannot_freeze_an_unmerged_pr(self):
        """CWE-345: the label is a cache; the merged-PR record is the authority."""
        with patch.object(claim_issue, "run_gh_json", return_value={"state": "OPEN"}):
            self.assertIsNone(claim_issue._terminal_lease_conflict(89, self.LEASED))

    def test_unreadable_merge_state_refuses_rather_than_guessing(self):
        with patch.object(claim_issue, "run_gh_json", return_value=None):
            self.assertEqual(
                claim_issue._terminal_lease_conflict(89, self.LEASED),
                claim_issue.EXIT_ERROR,
            )

    def test_no_lease_label_costs_no_lookup(self):
        with patch.object(claim_issue, "run_gh_json") as gh:
            self.assertIsNone(claim_issue._terminal_lease_conflict(89, ["author:agent-a"]))
        gh.assert_not_called()

    def test_unreadable_issue_lookup_blocks_the_claim(self):
        """A failed governance lookup must surface, not read as 'not merged'."""
        with patch.object(claim_issue, "run_gh_json", return_value=None):
            self.assertIsNotNone(claim_issue._terminally_merged(87))

    def test_malformed_closing_pr_reference_list_blocks_the_claim(self):
        """A non-list references payload is unknown, not "nothing merged"."""
        for payload in ({"nope": 1}, "refs", 7):
            with self.subTest(payload=payload):
                with patch.object(claim_issue, "run_gh_json", return_value={
                    "state": "CLOSED", "closedByPullRequestsReferences": payload,
                }):
                    self.assertIsNotNone(claim_issue._terminally_merged(87))

    def test_unidentifiable_closing_pr_reference_blocks_the_claim(self):
        """A reference with no resolvable number cannot clear the merge check."""
        for ref in ({"number": None}, {}, "89", None):
            with self.subTest(ref=ref):
                with patch.object(claim_issue, "run_gh_json", return_value={
                    "state": "CLOSED", "closedByPullRequestsReferences": [ref],
                }) as gh:
                    self.assertIsNotNone(claim_issue._terminally_merged(87))
                self.assertEqual(gh.call_count, 1, "must not look past a bad reference")

    def test_unreadable_closing_pr_blocks_the_claim(self):
        def gh(cmd):
            return ({"state": "CLOSED", "closedByPullRequestsReferences": [{"number": 89}]}
                    if "issue" in cmd else None)
        with patch.object(claim_issue, "run_gh_json", side_effect=gh):
            self.assertIsNotNone(claim_issue._terminally_merged(87))
