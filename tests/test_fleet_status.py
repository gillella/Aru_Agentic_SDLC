import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_status  # noqa: E402
from fleet_status import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_COMPLETE,
    EXIT_ERROR,
    EXIT_WAITING,
    evaluate_fleet_status,
)


def mock_issue(num, *labels, body="touches: src/a.py\n", title="Test Issue"):
    return {
        "number": num,
        "title": title,
        "body": body,
        "labels": [{"name": l} for l in labels],
    }


def mock_pr(num, *labels, decision=""):
    return {
        "number": num,
        "title": f"PR {num}",
        "labels": [{"name": l} for l in labels],
        "reviews": [],
        "reviewDecision": decision,
        "statusCheckRollup": [],
    }


def mock_project(title="widgets Board", repo_slug="octocat/widgets"):
    return {
        "id": "PROJ_1",
        "number": 1,
        "title": title,
        "repositories": {
            "nodes": [{"nameWithOwner": repo_slug}]
        },
    }


class FleetStatusTests(unittest.TestCase):

    @patch("fleet_status.list_worktree_branches", return_value=[])
    @patch("fleet_status.list_open_prs_details", return_value=[])
    @patch("fleet_status.list_open_issues", return_value=[])
    @patch("fleet_status.get_repo_projects")
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_complete_state_when_board_and_repo_are_empty(
        self, _slug, projects, _issues, _prs, _worktrees
    ):
        projects.return_value = [mock_project()]
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["exit_code"], EXIT_COMPLETE)
        self.assertEqual(status["open_issues_count"], 0)
        self.assertEqual(status["open_prs_count"], 0)

    @patch("fleet_status.get_issue_project_items", return_value=[])
    @patch("fleet_status.list_worktree_branches", return_value=[])
    @patch("fleet_status.list_open_prs_details", return_value=[])
    @patch("fleet_status.list_open_issues")
    @patch("fleet_status.get_repo_projects")
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_waiting_state_when_ready_issues_exist(
        self, _slug, projects, issues, _prs, _worktrees, _items
    ):
        projects.return_value = [mock_project()]
        issues.return_value = [mock_issue(10, "status:ready")]

        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["exit_code"], EXIT_WAITING)
        self.assertIn("Issue #10 is Ready for implementation.", status["reasons"])

    @patch("fleet_status.get_issue_project_items", return_value=[])
    @patch("fleet_status.list_worktree_branches", return_value=[])
    @patch("fleet_status.list_open_prs_details", return_value=[])
    @patch("fleet_status.list_open_issues")
    @patch("fleet_status.get_repo_projects")
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_blocked_state_when_human_review_required(
        self, _slug, projects, issues, _prs, _worktrees, _items
    ):
        projects.return_value = [mock_project()]
        issues.return_value = [mock_issue(15, "status:ready", "needs-human-review")]

        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["exit_code"], EXIT_BLOCKED)
        self.assertIn("Issue #15 carries 'needs-human-review'.", status["reasons"])

    @patch("fleet_status.list_open_issues", return_value=None)
    @patch("fleet_status.get_repo_projects")
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_error_state_on_api_failure(self, _slug, projects, _issues):
        projects.return_value = [mock_project()]
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)
        self.assertIn("Could not query open issues.", status["summary"])

    @patch("fleet_status.get_repo_projects", return_value=[])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_blocked_state_when_governed_board_missing(self, _slug, _projects):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["exit_code"], EXIT_BLOCKED)
        self.assertIn("Missing governed project board.", status["summary"])


if __name__ == "__main__":
    unittest.main()
