import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue  # noqa: E402
from fetch_next_issue import build_candidates, parse_dependencies  # noqa: E402


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


class ClaimWalkTests(unittest.TestCase):
    @patch("claim_issue.claim_issue")
    @patch.object(fetch_next_issue, "get_current_branch", return_value="main")
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_claim_rebuilds_candidates_after_conflict(
        self, list_open_issues, _branch, claim_issue_fn
    ):
        """After losing #10, rebuild so #11 overlapping touches is deferred."""
        initial = [
            issue(10, "touches: src/a.py\n", labels=("status:ready",)),
            issue(11, "touches: src/a.py\n", labels=("status:ready",)),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]
        after_conflict = [
            issue(
                10,
                "touches: src/a.py\n",
                labels=("agent:agent-a", "status:in-progress"),
            ),
            issue(11, "touches: src/a.py\n", labels=("status:ready",)),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]
        list_open_issues.side_effect = [initial, after_conflict]
        claim_issue_fn.side_effect = [
            fetch_next_issue_claim_conflict(),
            fetch_next_issue_claim_ok(),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--claim", "--json"]):
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"claimed_now": 12', output)
        self.assertEqual(
            [call.args[0] for call in claim_issue_fn.call_args_list],
            [10, 12],
        )
        self.assertEqual(list_open_issues.call_count, 2)

    @patch.object(fetch_next_issue, "get_current_branch", return_value="feat/issue-99-stale")
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_stale_branch_resume_is_ignored(self, list_open_issues, _branch):
        list_open_issues.return_value = [
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--json"]):
            buf = io.StringIO()
            err = io.StringIO()
            with patch.object(sys, "stdout", buf), patch.object(sys, "stderr", err):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"resumable_in_flight_issue": null', output)
        self.assertIn("ignoring stale resume", err.getvalue())

    @patch.object(fetch_next_issue, "get_current_branch", return_value="feat/issue-9-wip")
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_active_branch_resume_is_honored(self, list_open_issues, _branch):
        list_open_issues.return_value = [
            issue(
                9,
                "touches: src/**\n",
                labels=("agent:agent-b", "status:in-progress"),
            ),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--json"]):
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"resumable_in_flight_issue": 9', output)


def fetch_next_issue_claim_conflict():
    import claim_issue
    return claim_issue.EXIT_CONFLICT


def fetch_next_issue_claim_ok():
    import claim_issue
    return claim_issue.EXIT_OK


if __name__ == "__main__":
    unittest.main()
