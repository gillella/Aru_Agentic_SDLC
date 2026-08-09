import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue
from fetch_next_issue import build_candidates, parse_dependencies


def issue(number, body="", labels=()):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


class IssueSelectionTests(unittest.TestCase):
    def test_dependency_parser_ignores_prose(self):
        body = """A `depends-on:` field is required.

depends-on: #2, #4
"""
        self.assertEqual(parse_dependencies(body), [2, 4])

    def test_claimed_and_overlapping_work_are_not_candidates(self):
        issues = [
            issue(
                1,
                "touches: src/**\n",
                labels=("agent:agent-a", "status:in-progress"),
            ),
            issue(2, "touches: src/models.py\n", labels=("status:ready",)),
            issue(3, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-b")

        self.assertEqual([item["number"] for item in result["candidates"]], [3])
        self.assertEqual(result["conflicted"][0]["number"], 2)

    def test_backlog_and_missing_touches_are_not_claimable(self):
        issues = [
            issue(4, "touches: docs/**\n", labels=("status:backlog",)),
            issue(5, "touches:\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["not_ready"], [4])
        self.assertEqual(result["missing_touches"], [5])

    @patch.object(fetch_next_issue, "update_status", return_value=True)
    @patch.object(fetch_next_issue, "run_cmd")
    def test_reaper_synchronizes_board_before_removing_claim(self, run_cmd, update_status):
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        stale = issue(8, "touches: src/**\n", labels=("agent:agent-a", "status:in-progress"))
        stale["updatedAt"] = old

        def command_result(command, check=False):
            if command[:3] == ["gh", "pr", "list"]:
                return 0, "[]", ""
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return 0, "", ""
            return 0, "", ""

        run_cmd.side_effect = command_result

        released = fetch_next_issue.reap_stale_claims([stale], 4)

        self.assertEqual(released, [8])
        update_status.assert_called_once_with(8, "Ready", require_board=True)
        release_command = run_cmd.call_args_list[-1].args[0]
        self.assertIn("--remove-assignee", release_command)
        self.assertIn("agent:agent-a", release_command)


if __name__ == "__main__":
    unittest.main()
