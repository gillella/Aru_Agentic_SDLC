import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue


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
        get_issue.side_effect = [
            issue_with_labels("status:ready"),
            issue_with_labels("agent:agent-a"),
            # Third read: the confirmation pass that catches a contender whose
            # label write landed after the first read-back.
            issue_with_labels("agent:agent-a"),
        ]

        result = claim_issue.claim_issue(7, "agent-a")

        self.assertEqual(result, claim_issue.EXIT_OK)
        self.assertEqual(
            update_status.call_args,
            call(7, "In Progress", require_board=True),
        )


if __name__ == "__main__":
    unittest.main()
