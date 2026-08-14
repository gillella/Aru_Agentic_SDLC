import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fleet_status import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_COMPLETE,
    EXIT_ERROR,
    EXIT_WAITING,
    LINE_CEILING,
    collect_codebase_health,
    evaluate_fleet_status,
)


def mock_issue(num, *labels, body="touches: src/a.py\n", title="Test Issue"):
    return {
        "number": num,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


def mock_pr(num, *labels, decision="", merge_state="CLEAN"):
    return {
        "number": num,
        "title": f"PR {num}",
        "labels": [{"name": label} for label in labels],
        "reviews": [],
        "reviewDecision": decision,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [],
    }


def mock_project(title="widgets Board", repo_slug="octocat/widgets"):
    return {
        "id": "PROJ_1",
        "number": 1,
        "title": title,
        "repositories": {"nodes": [{"nameWithOwner": repo_slug}]},
    }


def mock_project_item(status="Ready", repo_slug="octocat/widgets"):
    return {
        "id": "ITEM_1",
        "status": {"optionId": f"STATUS_{status.upper().replace(' ', '_')}", "name": status},
        "project": {
            **mock_project(repo_slug=repo_slug),
            "field": {
                "id": "FIELD_STATUS",
                "options": [
                    {"id": f"STATUS_{status.upper().replace(' ', '_')}", "name": status}
                ],
            },
        },
    }


class FleetStatusTests(unittest.TestCase):
    def evaluate_fixture(self, issues=None, prs=None, items=None):
        issues = [] if issues is None else issues
        prs = [] if prs is None else prs
        item_map = {} if items is None else items
        with (
            patch("fleet_status.get_repo_slug", return_value="octocat/widgets"),
            patch("fleet_status.get_repo_projects", return_value=[mock_project()]),
            patch("fleet_status.query_open_issues", return_value=issues),
            patch("fleet_status.list_open_prs_details", return_value=prs),
            patch("fleet_status.list_worktree_branches", return_value=[]),
            patch(
                "fleet_status.query_issue_project_items",
                side_effect=lambda number: item_map.get(number, [mock_project_item()]),
            ),
        ):
            return evaluate_fleet_status(".")

    def test_complete_state_when_board_and_repo_are_empty(self):
        status = self.evaluate_fixture()

        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["exit_code"], EXIT_COMPLETE)
        self.assertEqual(status["open_issues_count"], 0)
        self.assertEqual(status["open_prs_count"], 0)

    def test_waiting_state_when_ready_issue_exists(self):
        status = self.evaluate_fixture([mock_issue(10, "status:ready")])

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["exit_code"], EXIT_WAITING)
        self.assertIn("Issue #10 is Ready for implementation.", status["reasons"])

    def test_waiting_state_when_issue_is_in_progress(self):
        status = self.evaluate_fixture(
            [mock_issue(11, "status:in-progress", "agent:codex-1")],
            items={11: [mock_project_item("In Progress")]},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertIn("Issue #11 is In Progress (codex-1).", status["reasons"])
        self.assertEqual(
            status["active_claims"],
            [{"type": "issue", "number": 11, "agent": "codex-1"}],
        )

    def test_waiting_state_when_issue_is_in_review(self):
        status = self.evaluate_fixture(
            [mock_issue(12, "status:in-review", "agent:codex-1")],
            items={12: [mock_project_item("In Review")]},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertIn("Issue #12 is In Review.", status["reasons"])
        self.assertEqual(status["active_claims"], [])

    def test_waiting_state_when_issue_is_in_backlog(self):
        status = self.evaluate_fixture(
            [mock_issue(13, "status:backlog")],
            items={13: [mock_project_item("Backlog")]},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertIn("Issue #13 is in Backlog.", status["reasons"])

    def test_dependency_blocked_backlog_remains_waiting(self):
        status = self.evaluate_fixture(
            [
                mock_issue(
                    14,
                    "status:backlog",
                    body="depends-on: #999\ntouches: src/a.py\n",
                )
            ],
            items={14: [mock_project_item("Backlog")]},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["exit_code"], EXIT_WAITING)
        self.assertNotEqual(status["state"], "complete")

    def test_human_intervention_is_blocked_only_for_escalated_merge_conflict(self):
        status = self.evaluate_fixture(
            prs=[mock_pr(15, "needs-human-review", merge_state="DIRTY")]
        )

        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["exit_code"], EXIT_BLOCKED)
        self.assertIn("severe merge conflict", status["reasons"][0])

    def test_human_review_label_alone_is_not_a_blanket_gate(self):
        status = self.evaluate_fixture(
            [mock_issue(16, "status:ready", "needs-human-review")]
        )

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["exit_code"], EXIT_WAITING)

    def test_open_issue_missing_from_governed_board_is_orphan_waiting(self):
        status = self.evaluate_fixture(
            [mock_issue(17, "status:ready")],
            items={17: []},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["orphans"], [17])
        self.assertIn("not on board", " ".join(status["reasons"]))

    def test_board_and_label_status_mismatch_is_drifted_waiting(self):
        status = self.evaluate_fixture(
            [mock_issue(18, "status:in-progress")],
            items={18: [mock_project_item("Ready")]},
        )

        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["drifted"], [18])
        self.assertIn("drifts from board status", " ".join(status["reasons"]))

    @patch("common.run_gh_json", return_value=None)
    @patch("fleet_status.get_repo_projects", return_value=[mock_project()])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_error_state_on_real_issue_query_failure(self, _slug, _projects, _run_gh):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)
        self.assertIn("Could not query open issues.", status["summary"])

    @patch("common.run_gh_json", return_value=None)
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_error_state_on_real_board_query_failure(self, _slug, _run_gh):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)
        self.assertIn("Could not query project boards.", status["summary"])

    @patch("common.run_gh_json", return_value=None)
    @patch("fleet_status.list_worktree_branches", return_value=[])
    @patch("fleet_status.list_open_prs_details", return_value=[])
    @patch(
        "fleet_status.query_open_issues",
        return_value=[mock_issue(19, "status:in-progress")],
    )
    @patch("fleet_status.get_repo_projects", return_value=[mock_project()])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_error_state_on_real_project_item_query_failure(
        self, _slug, _projects, _issues, _prs, _worktrees, _run_gh
    ):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)
        self.assertIn("Could not query issue project-board state.", status["summary"])

    @patch(
        "common.run_gh_json",
        return_value={
            "errors": [{"message": "partial result"}],
            "data": {
                "repository": {
                    "issue": {"projectItems": {"nodes": []}},
                }
            },
        },
    )
    @patch("fleet_status.list_worktree_branches", return_value=[])
    @patch("fleet_status.list_open_prs_details", return_value=[])
    @patch(
        "fleet_status.query_open_issues",
        return_value=[mock_issue(20, "status:ready")],
    )
    @patch("fleet_status.get_repo_projects", return_value=[mock_project()])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_partial_project_item_graphql_errors_fail_closed(
        self, _slug, _projects, _issues, _prs, _worktrees, _run_gh
    ):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)

    @patch("fleet_status.get_repo_projects", return_value=[])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_blocked_state_when_governed_board_is_genuinely_missing(
        self, _slug, _projects
    ):
        status = evaluate_fleet_status(".")

        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["exit_code"], EXIT_BLOCKED)
        self.assertIn("Missing governed project board.", status["summary"])

    def test_repo_dir_selects_target_and_restores_caller_directory(self):
        original = os.getcwd()
        observed = []
        with tempfile.TemporaryDirectory() as target:
            with (
                patch(
                    "fleet_status.get_repo_slug",
                    side_effect=lambda: observed.append(os.getcwd()) or "octocat/widgets",
                ),
                patch("fleet_status.get_repo_projects", return_value=[mock_project()]),
                patch("fleet_status.query_open_issues", return_value=[]),
                patch("fleet_status.list_open_prs_details", return_value=[]),
                patch("fleet_status.list_worktree_branches", return_value=[]),
            ):
                status = evaluate_fleet_status(target)

        self.assertEqual(status["state"], "complete")
        self.assertEqual(observed, [os.path.realpath(target)])
        self.assertEqual(os.getcwd(), original)

    def test_missing_repo_dir_is_error(self):
        status = evaluate_fleet_status("/definitely/not/a/repository")

        self.assertEqual(status["state"], "error")
        self.assertEqual(status["exit_code"], EXIT_ERROR)

    def test_codebase_health_counts_loc_and_ceiling_warnings(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "small.py").write_text("a\nb\n", encoding="utf-8")
            (root / ".venv").mkdir()
            (root / ".venv" / "ignored.py").write_text("x\n" * 500, encoding="utf-8")
            (root / "notes.md").write_text("m\n" * 500, encoding="utf-8")
            (root / "big.py").write_text("l\n" * LINE_CEILING, encoding="utf-8")
            health = collect_codebase_health(str(root))
        self.assertEqual(health["loc"], 2 + LINE_CEILING)
        self.assertEqual(health["file_count"], 2)
        self.assertEqual(health["line_ceiling"], 400)
        self.assertEqual(
            health["files_at_or_over_ceiling"],
            [{"path": "big.py", "lines": LINE_CEILING}],
        )
        self.assertGreater(health["average_file_bytes"], 0)

    def test_codebase_health_counts_mixed_language_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "app.js").write_text("a\nb\n", encoding="utf-8")
            (root / "run.sh").write_text("echo\n", encoding="utf-8")
            (root / "View.swift").write_text("s\n" * LINE_CEILING, encoding="utf-8")
            (root / "index.html").write_text("<p></p>\n", encoding="utf-8")
            (root / "app.css").write_text("body{}\n", encoding="utf-8")
            (root / "notes.md").write_text("m\n" * 500, encoding="utf-8")
            (root / "bundle.min.js").write_text("x\n" * 500, encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("d\n" * 500, encoding="utf-8")
            (root / "dist").mkdir()
            (root / "dist" / "out.js").write_text("o\n" * 500, encoding="utf-8")
            health = collect_codebase_health(str(root))
        self.assertEqual(health["loc"], 2 + 1 + LINE_CEILING + 1 + 1)
        self.assertEqual(health["file_count"], 5)
        self.assertEqual(
            health["files_at_or_over_ceiling"],
            [{"path": "View.swift", "lines": LINE_CEILING}],
        )

    def test_codebase_health_skips_symlinks_and_fifos(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real.py"
            real.write_text("print(1)\n", encoding="utf-8")
            (root / "link.py").symlink_to(real)
            os.mkfifo(root / "pipe.py")
            health = collect_codebase_health(str(root))
        self.assertEqual(health["file_count"], 1)
        self.assertEqual(health["loc"], 1)

    def test_complete_status_includes_codebase_health(self):
        with tempfile.TemporaryDirectory() as target:
            Path(target, "app.py").write_text("print(1)\n", encoding="utf-8")
            with (
                patch("fleet_status.get_repo_slug", return_value="octocat/widgets"),
                patch("fleet_status.get_repo_projects", return_value=[mock_project()]),
                patch("fleet_status.query_open_issues", return_value=[]),
                patch("fleet_status.list_open_prs_details", return_value=[]),
                patch("fleet_status.list_worktree_branches", return_value=[]),
            ):
                status = evaluate_fleet_status(target)
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["codebase_health"]["loc"], 1)
        self.assertEqual(status["codebase_health"]["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
