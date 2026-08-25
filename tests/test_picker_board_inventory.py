import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_work as fnw  # noqa: E402


class GovernedBoardInventoryTests(unittest.TestCase):
    def setUp(self):
        self.project = {
            "number": 7,
            "title": "repo Board",
            "owner": {"login": "owner"},
            "repositories": {"nodes": [{"nameWithOwner": "owner/repo"}]},
        }

    @patch.object(fnw, "run_cmd")
    @patch.object(fnw, "get_repo_projects")
    def test_returns_exact_status_map_for_complete_inventory(self, projects, run_cmd):
        projects.return_value = [self.project]
        run_cmd.return_value = (0, json.dumps({
            "totalCount": 3,
            "items": [
                {"status": "Backlog", "content": {
                    "number": 1, "repository": "owner/repo",
                }},
                {"status": "Ready", "content": {
                    "number": 2, "repository": "owner/repo",
                }},
                {"status": "Backlog", "content": {
                    "number": 8, "repository": "other/repo",
                }},
            ],
        }), "")

        result = fnw._governed_open_issue_statuses("owner/repo", {1, 2})

        self.assertEqual(result, {1: "Backlog", 2: "Ready"})

    @patch.object(fnw, "run_cmd")
    @patch.object(fnw, "get_repo_projects")
    def test_fails_closed_for_missing_duplicate_or_truncated_items(
        self, projects, run_cmd,
    ):
        projects.return_value = [self.project]
        bad_payloads = [
            {"totalCount": 1, "items": []},
            {"totalCount": 2, "items": [
                {"status": "Backlog", "content": {
                    "number": 1, "repository": "owner/repo",
                }},
                {"status": "Backlog", "content": {
                    "number": 1, "repository": "owner/repo",
                }},
            ]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                run_cmd.return_value = (0, json.dumps(payload), "")
                self.assertIsNone(
                    fnw._governed_open_issue_statuses("owner/repo", {1})
                )


if __name__ == "__main__":
    unittest.main()
