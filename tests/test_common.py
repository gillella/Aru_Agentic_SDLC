import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import (
    parse_touches,
    paths_overlap,
    select_governed_project_items,
)


class TouchMetadataTests(unittest.TestCase):
    def test_parser_ignores_prose_and_reads_metadata_line(self):
        body = """The new `touches:` declaration prevents collisions.

## Dependencies
touches: scripts/*, tests/test_common.py
"""

        self.assertEqual(
            parse_touches(body),
            ["scripts/*", "tests/test_common.py"],
        )

    def test_path_overlap_is_conservative_without_prefix_confusion(self):
        self.assertTrue(paths_overlap("src/*", "src/app/main.py"))
        self.assertFalse(paths_overlap("src/*", "src2/main.py"))
        self.assertFalse(paths_overlap("README.md", "README.md.bak"))


class GovernedProjectSelectionTests(unittest.TestCase):
    def project_item(self, title, *repositories):
        return {
            "id": title,
            "project": {
                "title": title,
                "repositories": {
                    "nodes": [{"nameWithOwner": repo} for repo in repositories]
                },
            },
        }

    def test_exact_governed_board_wins_over_other_linked_projects(self):
        items = [
            self.project_item("Team Roadmap", "octocat/widgets"),
            self.project_item("widgets Board", "octocat/widgets"),
        ]

        selected = select_governed_project_items(items, "octocat/widgets")

        self.assertEqual([item["id"] for item in selected], ["widgets Board"])

    def test_multiple_linked_projects_without_governed_name_fail_closed(self):
        items = [
            self.project_item("Team Roadmap", "octocat/widgets"),
            self.project_item("Release Tracker", "octocat/widgets"),
        ]

        self.assertEqual(select_governed_project_items(items, "octocat/widgets"), [])


if __name__ == "__main__":
    unittest.main()
