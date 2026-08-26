import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_ci  # noqa: E402


class CheckCiRestTests(unittest.TestCase):
    @patch.object(check_ci, "get_repo_slug", return_value="owner/repo")
    @patch.object(check_ci, "run_gh_json")
    def test_resolves_pr_and_checks_entirely_through_rest(self, api, _slug):
        api.side_effect = [
            {"head": {"sha": "abc"}},
            {"total_count": 1, "check_runs": [
                {"name": "CI", "status": "completed", "conclusion": "success"},
            ]},
            {"statuses": []},
        ]

        self.assertTrue(check_ci.check_ci_status(17))

        self.assertTrue(all(call.args[0][1] == "api" for call in api.call_args_list))
        self.assertTrue(all("graphql" not in call.args[0] for call in api.call_args_list))

    @patch.object(check_ci, "_pull_head", return_value="abc")
    @patch.object(check_ci, "_ci_contexts", return_value=None)
    def test_api_uncertainty_fails_closed(self, _contexts, _head):
        self.assertFalse(check_ci.check_ci_status(17))

    @patch.object(check_ci, "time")
    @patch.object(check_ci, "_pull_head", return_value="abc")
    @patch.object(check_ci, "_ci_contexts", return_value=[])
    def test_wait_timeout_fails_closed_and_backs_off(self, _contexts, _head, clock):
        clock.time.side_effect = [0, 0, 15, 45, 75]

        self.assertFalse(
            check_ci.check_ci_status(17, wait=True, poll_interval=15, timeout=45)
        )

        self.assertEqual([call.args[0] for call in clock.sleep.call_args_list], [15, 30])

    @patch.object(check_ci, "_pull_head", return_value="abc")
    @patch.object(check_ci, "_ci_contexts", return_value=[
        {"name": "CI", "state": "FAILURE"},
    ])
    def test_failure_is_reported(self, _contexts, _head):
        self.assertFalse(check_ci.check_ci_status(17))

    @patch.object(check_ci, "_pull_head", return_value="abc")
    @patch.object(check_ci, "_ci_contexts", return_value=[
        {"name": "CI", "state": "COMPLETED"},
    ])
    def test_unknown_or_incomplete_terminal_state_fails_closed(self, _contexts, _head):
        self.assertFalse(check_ci.check_ci_status(17))


if __name__ == "__main__":
    unittest.main()
