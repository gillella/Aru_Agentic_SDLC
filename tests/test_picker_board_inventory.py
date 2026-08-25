import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import picker_board_inventory as inventory  # noqa: E402


class GovernedBoardInventoryTests(unittest.TestCase):
    def setUp(self):
        self.project = {
            "number": 7,
            "title": "repo Board",
            "owner": {"login": "owner"},
            "repositories": {"nodes": [{"nameWithOwner": "owner/repo"}]},
        }

    @patch.object(inventory, "run_cmd")
    @patch.object(inventory, "get_repo_projects")
    def test_returns_exact_status_map_for_complete_inventory(self, projects, run_cmd):
        projects.return_value = [self.project]
        run_cmd.return_value = (0, json.dumps({
            "totalCount": 4,
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
                {"status": "Ready", "content": {"type": "DraftIssue"}},
            ],
        }), "")

        result = inventory.governed_board_inventory("owner/repo", {1, 2})

        self.assertEqual(result, ({1: "Backlog", 2: "Ready"}, 2))

    @patch.object(inventory, "run_cmd")
    @patch.object(inventory, "get_repo_projects")
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
                    inventory.governed_board_inventory("owner/repo", {1})
                )


if __name__ == "__main__":
    unittest.main()
