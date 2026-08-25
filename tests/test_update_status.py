import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import update_issue_status  # noqa: E402


class StatusSynchronizationTests(unittest.TestCase):
    @patch.object(update_issue_status, "run_cmd")
    @patch.object(update_issue_status, "set_board_status", return_value=False)
    @patch.object(update_issue_status, "get_issue")
    def test_required_board_failure_does_not_change_labels(
        self, get_issue, _set_board_status, run_cmd
    ):
        get_issue.return_value = {"labels": [{"name": "status:ready"}]}

        result = update_issue_status.update_status(9, "In Progress", require_board=True)

        self.assertFalse(result)
        run_cmd.assert_not_called()

    @patch.object(update_issue_status, "run_cmd", return_value=(0, "", ""))
    @patch.object(update_issue_status, "set_board_status", return_value=False)
    @patch.object(update_issue_status, "get_issue")
    def test_optional_board_allows_label_only_update(
        self, get_issue, _set_board_status, run_cmd
    ):
        get_issue.return_value = {"labels": [{"name": "status:backlog"}]}

        result = update_issue_status.update_status(9, "Ready")

        self.assertTrue(result)
        command = run_cmd.call_args.args[0]
        self.assertIn("status:ready", command)
        self.assertIn("status:backlog", command)

    @patch.object(update_issue_status, "run_cmd")
    @patch.object(update_issue_status, "set_board_status")
    @patch.object(update_issue_status, "get_issue")
    def test_conditional_transition_refuses_changed_or_claimed_issue(
        self, get_issue, set_board_status, run_cmd,
    ):
        cases = [
            {"state": "OPEN", "labels": [{"name": "status:in-progress"}]},
            {"state": "OPEN", "labels": [
                {"name": "status:backlog"}, {"name": "agent:other"},
            ]},
            {"state": "CLOSED", "labels": [{"name": "status:backlog"}]},
            {"state": "OPEN", "updatedAt": "new", "labels": [
                {"name": "status:backlog"},
            ]},
        ]
        for issue in cases:
            with self.subTest(issue=issue):
                get_issue.return_value = issue
                self.assertFalse(update_issue_status.update_status(
                    9, "Ready", require_board=True,
                    expected_status="Backlog", require_unclaimed=True,
                    expected_updated_at=("old" if issue.get("updatedAt") else None),
                ))
        set_board_status.assert_not_called()
        run_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
