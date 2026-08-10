import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402


def issue_with_labels(*names):
    return {"number": 7, "labels": [{"name": name} for name in names]}


class ClaimProtocolTests(unittest.TestCase):
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
            issue_with_labels("agent:agent-a", "agent:agent-b"),
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
        # pre-check + SETTLE_ROUNDS settle reads + post-status verify
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("agent:agent-a"),
            issue_with_labels("agent:agent-a"),
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
            issue_with_labels("agent:agent-b"),  # first settle: sole holder
            issue_with_labels("agent:agent-a", "agent:agent-b"),  # second: loses
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
            issue_with_labels("agent:agent-b"),
            issue_with_labels("agent:agent-b"),
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


class InReviewHandoffStatusTests(unittest.TestCase):
    @patch("update_issue_status.run_cmd", return_value=(0, "", ""))
    @patch("update_issue_status.set_board_status", return_value=True)
    @patch("update_issue_status.get_issue")
    def test_update_status_removes_agent_claim_label_when_moving_to_in_review(
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
        self.assertIn("agent:agent-1", cmd)


if __name__ == "__main__":
    unittest.main()
