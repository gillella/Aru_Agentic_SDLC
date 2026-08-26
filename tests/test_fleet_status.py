# line-ceiling: 1580
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_status  # noqa: E402
import agent_presence as ap  # noqa: E402
import slack_projects as sp  # noqa: E402
from fleet_status import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_COMPLETE,
    EXIT_ERROR,
    EXIT_STALLED,
    EXIT_WAITING,
    LINE_CEILING,
    apply_ci_failure_rate,
    apply_closed_issue_cost,
    build_merge_queue,
    build_operator_screen,
    collect_codebase_health,
    detect_stall,
    most_recent_merge_history,
    most_recent_merge_time,
    notify_stall,
    stall_alert_text,
    discover_fleet_size,
    evaluate_fleet_status,
    evaluate_queue_row,
    format_merge_queue,
    format_operator_screen,
    next_queue_action,
    resolve_fleet_size,
    resolve_ready_target,
)


_STATE_TEMPORARY = None
_PRESENCE_PATCHER = None
_REGISTRY_DEFAULTS = None


def setUpModule():
    global _STATE_TEMPORARY, _PRESENCE_PATCHER, _REGISTRY_DEFAULTS
    _STATE_TEMPORARY = tempfile.TemporaryDirectory()
    root = Path(_STATE_TEMPORARY.name)
    _PRESENCE_PATCHER = patch.object(
        ap, "DEFAULT_PRESENCE_PATH", root / "agent-presence.json"
    )
    _PRESENCE_PATCHER.start()
    _REGISTRY_DEFAULTS = sp.ProjectRegistry.__init__.__defaults__
    sp.ProjectRegistry.__init__.__defaults__ = (
        root / "projects.json",
        root / "slack-audit.json",
        _REGISTRY_DEFAULTS[2],
    )


def tearDownModule():
    sp.ProjectRegistry.__init__.__defaults__ = _REGISTRY_DEFAULTS
    _PRESENCE_PATCHER.stop()
    _STATE_TEMPORARY.cleanup()


def mock_issue(num, *labels, body="touches: src/a.py\n", title="Test Issue", **extra):
    issue = {
        "number": num,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in labels],
    }
    issue.update(extra)
    return issue


def mock_pr(num, *labels, decision="", merge_state="CLEAN", **extra):
    pr = {
        "number": num,
        "title": f"PR {num}",
        "labels": [{"name": label} for label in labels],
        "reviews": [],
        "reviewDecision": decision,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [],
    }
    pr.update(extra)
    return pr


def coderabbit_evidence(head="c"):
    oid = head * 40
    return {"head_oid": oid, "unresolved": 0, "unfixed": 0, "outdated_unfixed": 0, "reviewed_head": True, "reviews": [{"id": "coderabbit-review", "state": "COMMENTED", "submittedAt": "2026-08-22T00:00:00Z", "body": "Review complete.", "commit": {"oid": oid}, "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}], "coderabbit_status": [{"__typename": "CheckRun", "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS", "checkSuite": {"app": {"slug": "coderabbitai"}}}]}


def hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def questions(status):
    return {item["key"]: item for item in status["operator_screen"]["questions"]}


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
    def evaluate_fixture(
        self,
        issues=None,
        prs=None,
        items=None,
        fleet_size=None,
        ready_target=None,
        merge_history=None,
        agent_count=0,
    ):
        issues = [] if issues is None else issues
        prs = [] if prs is None else prs
        item_map = {} if items is None else items
        if merge_history is None:
            merge_history = (datetime.now(timezone.utc), True)
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
            patch("fleet_status.most_recent_merge_history", return_value=merge_history),
            patch("fleet_status.registered_agent_count", return_value=agent_count),
        ):
            return evaluate_fleet_status(".", fleet_size=fleet_size, ready_target=ready_target)

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

    @patch("common.run_cmd", return_value=(1, "", "failed"))
    @patch("common.get_repo_slug", return_value="octocat/widgets")
    @patch("fleet_status.get_repo_projects", return_value=[mock_project()])
    @patch("fleet_status.get_repo_slug", return_value="octocat/widgets")
    def test_error_state_on_real_issue_query_failure(
        self, _slug, _projects, _common_slug, _run
    ):
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

    def test_codebase_health_counts_non_utf8_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "latin1.py").write_bytes(
                b"# -*- coding: latin-1 -*-\nx = '\xe9'\n"
            )
            (root / "app.js").write_text("a\nb\n", encoding="utf-8")
            health = collect_codebase_health(str(root))
        self.assertEqual(health["file_count"], 2)
        self.assertEqual(health["loc"], 4)

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

    def test_complete_board_includes_six_operator_questions(self):
        status = self.evaluate_fixture()
        keys = [item["key"] for item in status["operator_screen"]["questions"]]
        self.assertEqual(
            keys,
            [
                "ready_depth",
                "holders",
                "review_age",
                "review_rounds",
                "ci_failure_rate",
                "cost",
            ],
        )
        self.assertEqual(questions(status)["ready_depth"]["severity"], "warn")
        self.assertIn("fleet size unavailable", questions(status)["ready_depth"]["summary"])
        self.assertIn("[WARN]", format_operator_screen(status["operator_screen"]))

    def test_ready_depth_marks_starved_fleet(self):
        status = self.evaluate_fixture(
            [
                mock_issue(
                    21, "status:in-progress", "agent:codex-1",
                    updatedAt=hours_ago(1),
                )
            ],
            items={21: [mock_project_item("In Progress")]},
            fleet_size=1,
        )
        ready = questions(status)["ready_depth"]
        self.assertEqual(ready["severity"], "attn")
        self.assertEqual(ready["ready_depth"], 0)
        self.assertEqual(ready["fleet_size"], 1)
        self.assertEqual(ready["in_flight"], 1)
        self.assertIn("[ATTN]", format_operator_screen(status["operator_screen"]))

    def test_negative_fleet_size_is_unavailable_not_healthy(self):
        status = self.evaluate_fixture(fleet_size=-1)
        ready = questions(status)["ready_depth"]
        self.assertIsNone(ready["fleet_size"])
        self.assertEqual(ready["severity"], "warn")
        self.assertIn("unavailable", ready["summary"])

    def test_main_fetches_ci_history_from_repo_dir(self):
        from fleet_status import main

        original = os.getcwd()
        observed = []
        status = {
            "state": "waiting",
            "exit_code": EXIT_WAITING,
            "summary": "waiting",
            "operator_screen": {"questions": [], "severity": "ok"},
        }
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "widgets"
            repo.mkdir()

            def fake_fetch(_window):
                observed.append(os.getcwd())
                return []

            with (
                patch("fleet_status.evaluate_fleet_status", return_value=status),
                patch("factory_metrics.fetch_ci_runs", side_effect=fake_fetch),
                patch("sys.argv", ["fleet_status.py", "--json", "--repo-dir", str(repo)]),
                patch("builtins.print"),
                self.assertRaises(SystemExit) as raised,
            ):
                main()
        self.assertEqual(raised.exception.code, EXIT_WAITING)
        self.assertEqual(observed, [str(repo.resolve())])
        self.assertEqual(os.getcwd(), original)

    def test_holders_mark_stale_claims(self):
        status = self.evaluate_fixture(
            [
                mock_issue(
                    22, "status:in-progress", "agent:codex-1",
                    updatedAt=hours_ago(6),
                )
            ],
            items={22: [mock_project_item("In Progress")]},
        )
        holders = questions(status)["holders"]
        self.assertEqual(holders["severity"], "attn")
        self.assertIn("idle", holders["summary"])
        self.assertEqual(holders["holders"][0]["age_availability"], "measured")

    def test_review_age_and_rounds_and_ci_rate(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    30,
                    createdAt=hours_ago(10),
                    comments=[{"body": f"review-queued-at: {hours_ago(10)}"}],
                    reviews=[{"state": "CHANGES_REQUESTED"}, {"state": "CHANGES_REQUESTED"}],
                    _active_review_feedback=[],
                )
            ]
        )
        self.assertEqual(status["state"], "waiting")
        asked = questions(status)
        self.assertEqual(asked["review_age"]["severity"], "attn")
        self.assertGreaterEqual(asked["review_age"]["oldest_age_hours"], 8)
        self.assertEqual(asked["review_rounds"]["severity"], "warn")
        self.assertEqual(asked["review_rounds"]["max_review_rounds"], 2)
        self.assertEqual(asked["ci_failure_rate"]["availability"], "unavailable")

    def test_old_merge_with_open_prs_and_agents_evaluates_stalled(self):
        old = datetime.now(timezone.utc) - timedelta(hours=9)
        status = self.evaluate_fixture(
            prs=[mock_pr(30)],
            merge_history=(old, True),
            agent_count=2,
        )
        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["exit_code"], EXIT_STALLED)
        self.assertEqual(status["open_pr_numbers"], [30])
        self.assertTrue(status["stall"]["stalled"])

    def test_merge_lookup_failure_does_not_evaluate_stalled(self):
        status = self.evaluate_fixture(
            prs=[mock_pr(30)],
            merge_history=(None, False),
            agent_count=2,
        )
        self.assertEqual(status["state"], "waiting")
        self.assertFalse(status["stall"]["stalled"])

    def test_review_age_ignores_drafts_and_completed_reviews(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    32, isDraft=True,
                    comments=[{"body": f"review-queued-at: {hours_ago(10)}"}],
                ),
                mock_pr(
                    33, decision="COMMENTED",
                    comments=[{"body": f"review-queued-at: {hours_ago(10)}"}],
                    statusCheckRollup=[{
                        "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
                    }],
                    _active_review_feedback=[],
                    unresolvedReviewThreadsCount=0,
                    reviewThreads={"nodes": []},
                    _review_evidence=coderabbit_evidence(),
                ),
            ]
        )
        asked = questions(status)["review_age"]
        self.assertEqual(asked["pending"], [])
        self.assertEqual(asked["severity"], "ok")

    def test_review_age_uses_queued_at_not_created_at(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    34,
                    createdAt=hours_ago(48),
                    comments=[{"body": f"review-queued-at: {hours_ago(1)}"}],
                    _active_review_feedback=[],
                )
            ]
        )
        asked = questions(status)["review_age"]
        self.assertEqual(asked["pending"][0]["age_availability"], "measured")
        self.assertLess(asked["oldest_age_hours"], 2)
        self.assertEqual(asked["severity"], "ok")

    def test_review_age_without_queue_stamp_is_unavailable(self):
        status = self.evaluate_fixture(
            prs=[mock_pr(35, createdAt=hours_ago(48), _active_review_feedback=[])]
        )
        asked = questions(status)["review_age"]
        self.assertEqual(asked["pending"][0]["age_availability"], "unavailable")
        self.assertIsNone(asked["oldest_age_hours"])
        self.assertEqual(asked["severity"], "warn")

    def test_review_rounds_count_same_account_commented_reviews(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    31,
                    reviews=[
                        {"state": "COMMENTED"},
                        {"state": "COMMENTED"},
                        {"state": "COMMENTED"},
                        {"state": "APPROVED"},
                    ],
                )
            ]
        )
        asked = questions(status)["review_rounds"]
        self.assertEqual(asked["max_review_rounds"], 0)
        self.assertEqual(asked["severity"], "ok")

    def test_review_rounds_count_blocking_commented_reviews_only(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    36,
                    reviews=[
                        {
                            "state": "COMMENTED",
                            "body": "Verdict: **CHANGES REQUESTED**. Blocking finding below.",
                        },
                        {
                            "state": "COMMENTED",
                            "body": "Verdict: no blocking findings. LGTM.",
                        },
                    ],
                )
            ]
        )
        asked = questions(status)["review_rounds"]
        self.assertEqual(asked["max_review_rounds"], 1)
        self.assertEqual(asked["severity"], "ok")

    def test_review_rounds_prefer_changes_requested_over_clean_phrasing(self):
        status = self.evaluate_fixture(
            prs=[
                mock_pr(
                    37,
                    reviews=[
                        {
                            "state": "COMMENTED",
                            "body": (
                                "No blocking compatibility issues; however "
                                "verdict: CHANGES REQUESTED for correctness"
                            ),
                        },
                        {
                            "state": "COMMENTED",
                            "body": "No findings from lint. Blocking finding: runtime failure.",
                        },
                    ],
                )
            ]
        )
        asked = questions(status)["review_rounds"]
        self.assertEqual(asked["max_review_rounds"], 2)

    def test_default_evaluation_discovers_launch_fleet_clones(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repo = home / "widgets"
            repo.mkdir()
            for name in ("agent-1", "agent-2", "agent-3", "agent-4"):
                (home / ".aru-fleet" / "widgets" / name / ".git").mkdir(parents=True)
            with (
                patch("fleet_status.Path.home", return_value=home),
                patch("fleet_status.get_repo_slug", return_value="octocat/widgets"),
                patch("fleet_status.get_repo_projects", return_value=[mock_project()]),
                patch("fleet_status.query_open_issues", return_value=[]),
                patch("fleet_status.list_open_prs_details", return_value=[]),
                patch("fleet_status.list_worktree_branches", return_value=[]),
            ):
                status = evaluate_fleet_status(str(repo))
        ready = questions(status)["ready_depth"]
        self.assertEqual(ready["fleet_size"], 4)
        self.assertEqual(ready["in_flight"], 0)
        self.assertEqual(ready["severity"], "ok")
        self.assertEqual(ready["signal"], "complete")

    def test_discover_fleet_size_reads_run_fleet_state(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "widgets"
            repo.mkdir()
            state = Path(raw) / "state" / "aru-factory"
            digest = __import__("hashlib").sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
            folder = state / digest
            folder.mkdir(parents=True)
            (folder / "cursor-1.json").write_text("{}", encoding="utf-8")
            (folder / "codex-1.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(raw) / "state")}, clear=False):
                self.assertEqual(discover_fleet_size(str(repo)), 2)

    def test_ci_failure_rate_counts_failed_then_green_reruns(self):
        screen = build_operator_screen([], [])
        apply_ci_failure_rate(
            screen,
            [
                {"status": "completed", "conclusion": "failure", "pull_requests": [{"number": 1}]},
                {"status": "completed", "conclusion": "success", "pull_requests": [{"number": 1}]},
            ],
            None,
        )
        ci = {item["key"]: item for item in screen["questions"]}["ci_failure_rate"]
        self.assertEqual(ci["failed"], 1)
        self.assertEqual(ci["completed"], 2)
        self.assertEqual(ci["failure_rate"], 0.5)
        self.assertEqual(ci["severity"], "attn")
        self.assertEqual(ci["availability"], "measured")

    def test_resolve_fleet_size_reads_env_and_rejects_invalid(self):
        with patch.dict(os.environ, {"ARU_FLEET_SIZE": "4"}, clear=False):
            self.assertEqual(resolve_fleet_size(None), 4)
        with patch.dict(os.environ, {"ARU_FLEET_SIZE": "nope"}, clear=False):
            self.assertIsNone(resolve_fleet_size(None))
        self.assertEqual(resolve_fleet_size(3), 3)
        self.assertIsNone(resolve_fleet_size(-1))

    def test_default_main_skips_closed_issue_collection(self):
        status = {
            "state": "waiting",
            "exit_code": EXIT_WAITING,
            "summary": "waiting",
            "operator_screen": {"questions": [], "severity": "ok"},
        }
        with (
            patch("fleet_status.evaluate_fleet_status", return_value=status),
            patch("fleet_status.fetch_ci_history", return_value=[]),
            patch("factory_metrics.collect_factory_metrics") as collect,
            patch("sys.argv", ["fleet_status.py", "--json"]),
            patch("builtins.print"),
            self.assertRaises(SystemExit) as raised,
        ):
            from fleet_status import main
            main()
        self.assertEqual(raised.exception.code, EXIT_WAITING)
        collect.assert_not_called()

    def test_cost_question_uses_measured_closed_issue_metrics(self):
        screen = build_operator_screen([], [])
        apply_closed_issue_cost(
            screen,
            {
                "closed_issue_count": 2,
                "cost_per_closed_issue": {
                    "availability": "measured",
                    "average_usd_measured": 1.5,
                },
                "outlier_issue_numbers": [9],
                "issues": [
                    {"cycle_time_hours": 2.0},
                    {"cycle_time_hours": 4.0},
                ],
            },
            None,
        )
        cost = {item["key"]: item for item in screen["questions"]}["cost"]
        self.assertEqual(cost["severity"], "attn")
        self.assertEqual(cost["average_cycle_hours"], 3.0)
        self.assertIn("outlier", cost["summary"])

    def test_resolve_ready_target_prefers_explicit_and_env_and_fleet_size(self):
        self.assertEqual(resolve_ready_target(fleet_size=3, configured=5), 5)
        with patch.dict(os.environ, {"ARU_READY_TARGET": "4"}, clear=False):
            self.assertEqual(resolve_ready_target(fleet_size=3, configured=None), 4)
            self.assertEqual(resolve_ready_target(fleet_size=3, configured=6), 6)
        with patch.dict(os.environ, {"ARU_READY_TARGET": "invalid"}, clear=False):
            self.assertEqual(resolve_ready_target(fleet_size=3, configured=None), 3)
        self.assertEqual(resolve_ready_target(fleet_size=3, configured=None), 3)
        self.assertIsNone(resolve_ready_target(fleet_size=None, configured=None))

    def test_ready_depth_starved_state_distinct_from_complete(self):
        # Factory with in-flight work and 0 Ready issues with fleet_size=2 is starved
        status = self.evaluate_fixture(
            issues=[
                mock_issue(50, "status:in-progress", "agent:codex-1"),
            ],
            items={50: [mock_project_item("In Progress")]},
            fleet_size=2,
        )
        self.assertEqual(status["state"], "waiting")
        ready_q = questions(status)["ready_depth"]
        self.assertEqual(ready_q["severity"], "attn")
        self.assertEqual(ready_q["signal"], "starved")
        self.assertEqual(ready_q["ready_depth"], 0)
        self.assertEqual(ready_q["ready_target"], 2)
        self.assertIn("starved", ready_q["summary"])

    def test_ready_depth_healthy_state_when_target_met(self):
        status = self.evaluate_fixture(
            issues=[
                mock_issue(51, "status:ready", body="touches: src/a.py\n"),
                mock_issue(52, "status:ready", body="touches: src/b.py\n"),
            ],
            fleet_size=2,
        )
        self.assertEqual(status["state"], "waiting")
        ready_q = questions(status)["ready_depth"]
        self.assertEqual(ready_q["severity"], "ok")
        self.assertEqual(ready_q["signal"], "healthy")
        self.assertEqual(ready_q["ready_depth"], 2)
        self.assertEqual(ready_q["ready_target"], 2)
        self.assertEqual(ready_q["claimable"], 2)

    def test_review_queue_depth_reported_and_marks_flooded_queue(self):
        prs = [mock_pr(i, _active_review_feedback=[]) for i in range(101, 107)]  # 6 open PRs awaiting review
        status = self.evaluate_fixture(
            issues=[
                mock_issue(51, "status:ready", body="touches: src/a.py\n"),
                mock_issue(52, "status:ready", body="touches: src/b.py\n"),
            ],
            prs=prs,
            fleet_size=2,
        )
        ready_q = questions(status)["ready_depth"]
        self.assertEqual(ready_q["review_queue_depth"], 6)
        self.assertEqual(ready_q["severity"], "attn")
        self.assertEqual(ready_q["signal"], "flooded")
        self.assertIn("flooded", ready_q["summary"])

    def test_ready_depth_complete_state_distinct_from_starved(self):
        # When fleet_size is 0, empty board is complete and healthy
        status_empty = self.evaluate_fixture(fleet_size=0)
        self.assertEqual(status_empty["state"], "complete")
        ready_q = questions(status_empty)["ready_depth"]
        self.assertEqual(ready_q["severity"], "ok")
        self.assertEqual(ready_q["signal"], "complete")
        self.assertEqual(ready_q["ready_depth"], 0)

        # An authoritatively complete board with fleet_size=2 remains complete and non-alarming
        status_active = self.evaluate_fixture(fleet_size=2)
        self.assertEqual(status_active["state"], "complete")
        ready_active_q = questions(status_active)["ready_depth"]
        self.assertEqual(ready_active_q["severity"], "ok")
        self.assertEqual(ready_active_q["signal"], "complete")
        self.assertNotIn("starved", ready_active_q["summary"])

    def test_evaluate_fixture_with_configurable_ready_target(self):
        status = self.evaluate_fixture(
            issues=[
                mock_issue(51, "status:ready", body="touches: src/a.py\n"),
            ],
            fleet_size=1,
            ready_target=4,
        )
        ready_q = questions(status)["ready_depth"]
        self.assertEqual(ready_q["ready_depth"], 1)
        self.assertEqual(ready_q["ready_target"], 4)
        self.assertEqual(ready_q["signal"], "starved")
        self.assertEqual(ready_q["severity"], "attn")

    def test_triage_backlog_print_capacity_with_ready_target(self):
        import io
        from triage_backlog import print_capacity
        out = io.StringIO()
        with patch("sys.stdout", out):
            print_capacity({"ready_total": 1, "concurrent": [10], "deferred": []}, [], ready_target=3)
        printed = out.getvalue()
        self.assertIn("Ready target:            3", printed)

    def test_pending_review_eligibility_matches_canonical_rules(self):
        from fleet_status import _pending_review
        # Resolved threads on changes-requested PR: eligible for re-review
        pr_rework_resolved = {
            "number": 10, "isDraft": False, "reviewDecision": "CHANGES_REQUESTED",
            "unresolvedReviewThreadsCount": 0, "labels": [],
        }
        self.assertTrue(_pending_review(pr_rework_resolved))

        # Unresolved threads on commented review: waiting on author, not review queue
        pr_unresolved_feedback = {
            "number": 11, "isDraft": False, "reviewDecision": "COMMENTED",
            "unresolvedReviewThreadsCount": 2, "labels": [],
        }
        self.assertFalse(_pending_review(pr_unresolved_feedback))

        # Helper fallback when thread counts are missing from gh pr list payload
        pr_missing_field = {
            "number": 15, "isDraft": False, "reviewDecision": "COMMENTED", "labels": [],
        }
        with patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=[{"id": "t1"}]):
            self.assertFalse(_pending_review(pr_missing_field))

        with patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=[]):
            pr_clean_field = {
                "number": 16, "isDraft": False, "reviewDecision": "COMMENTED", "labels": [],
            }
            self.assertTrue(_pending_review(pr_clean_field))

        pr_coderabbit_reviewed = {
            "number": 17,
            "isDraft": False,
            "reviewDecision": "COMMENTED",
            "labels": [],
            "statusCheckRollup": [{
                "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
            }],
        }
        pr_coderabbit_stale = {
            "number": 17,
            "isDraft": False,
            "reviewDecision": "COMMENTED",
            "labels": [],
            "statusCheckRollup": [{
                "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
            }],
        }
        pr_coderabbit_reviewed["_active_review_feedback"] = []
        pr_coderabbit_reviewed["_review_evidence"] = coderabbit_evidence("a")
        with patch(
            "merge_pr._with_coderabbit_status",
            side_effect=AssertionError("seeded evidence should stay hermetic"),
        ):
            self.assertFalse(_pending_review(pr_coderabbit_reviewed))

        stale = coderabbit_evidence("a")
        stale["reviewed_head"] = False
        stale["reviews"][0]["commit"] = {"oid": "old" * 10}
        pr_coderabbit_stale["_active_review_feedback"] = []
        pr_coderabbit_stale["_review_evidence"] = stale
        with patch(
            "merge_pr._with_coderabbit_status",
            side_effect=AssertionError("seeded evidence should stay hermetic"),
        ):
            self.assertTrue(_pending_review(pr_coderabbit_stale))

        # Generic approval is not authoritative CodeRabbit review evidence.
        pr_approved = {
            "number": 12, "isDraft": False, "reviewDecision": "APPROVED",
            "unresolvedReviewThreadsCount": 0, "labels": [],
            "_review_evidence": None,
        }
        self.assertTrue(_pending_review(pr_approved))

        # Legacy reviewed-by labels do not satisfy the CodeRabbit-only contract.
        pr_reviewed = {
            "number": 13, "isDraft": False, "reviewDecision": None,
            "unresolvedReviewThreadsCount": 0,
            "labels": [{"name": "author:claude-1"}, {"name": "reviewed-by:codex-1"}],
            "_active_review_feedback": [], "_review_evidence": None,
        }
        self.assertTrue(_pending_review(pr_reviewed))

        # Draft PR: not in review queue
        pr_draft = {"number": 14, "isDraft": True, "labels": []}
        self.assertFalse(_pending_review(pr_draft))

    def test_invalid_authority_skips_review_evidence_lookup(self):
        for labels in ([], [{"name": "review:unknown"}], [
            {"name": "review:coderabbit"}, {"name": "review:sourcery"},
        ]):
            pr = {"number": 21, "isDraft": False, "reviewDecision": "COMMENTED",
                  "labels": labels, "_active_review_feedback": []}
            with self.subTest(labels=labels), patch(
                "merge_pr.review_evidence",
                side_effect=AssertionError("review evidence should stay unloaded"),
            ):
                self.assertTrue(fleet_status._pending_review(pr))

    def test_assigned_service_evidence_controls_compact_review_state(self):
        for service, authoritative in (
            ("sourcery", True), ("codeant", True), ("agent", True), ("sourcery", False),
        ):
            reviewed = mock_pr(18, f"review:{service}", decision="COMMENTED")
            evidence = {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0}
            with self.subTest(service=service, authoritative=authoritative), \
                 patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=[]), \
                 patch("merge_pr.review_evidence", return_value=evidence), \
                 patch("merge_pr.with_service_evidence", return_value=evidence) as enrich, \
                 patch("merge_pr.has_authoritative_assigned_review", return_value=authoritative):
                status = self.evaluate_fixture(prs=[reviewed])
            reason = "PR #18 is reviewed and waiting for merge." if authoritative else "PR #18 is open and pending review."
            self.assertIn(reason, status["reasons"])
            enrich.assert_called_once_with(reviewed, 18, evidence)

    def test_assigned_service_threads_isolate_feedback_state(self):
        evidence = {
            "unresolved": 1,
            "unfixed": 0,
            "outdated_unfixed": 0,
            "service_threads": {
                "sourcery": {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
                "codeant": {"unresolved": 1, "unfixed": 0, "outdated_unfixed": 0},
            },
        }
        sourcery_pr = mock_pr(18, "review:sourcery", decision="COMMENTED")
        with patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=[{"id": 1}]), \
             patch("merge_pr.review_evidence", return_value=evidence), \
             patch("merge_pr.with_service_evidence", return_value=evidence), \
             patch("merge_pr.has_authoritative_assigned_review", return_value=True):
            status = self.evaluate_fixture(prs=[sourcery_pr])
        self.assertIn("PR #18 is reviewed and waiting for merge.", status["reasons"])

        codeant_pr = mock_pr(18, "review:codeant", decision="COMMENTED")
        with patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=[]), \
             patch("merge_pr.review_evidence", return_value=evidence), \
             patch("merge_pr.with_service_evidence", return_value=evidence), \
             patch("merge_pr.has_authoritative_assigned_review", return_value=True):
            status = self.evaluate_fixture(prs=[codeant_pr])
        self.assertIn("PR #18 has active review feedback.", status["reasons"])

    def test_unknown_feedback_result_keeps_pr_in_feedback_state(self):
        pr = mock_pr(19, decision="COMMENTED", statusCheckRollup=[{
            "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
        }])
        with patch("fetch_pr_feedback.fetch_active_review_feedback", return_value=None), \
             patch("merge_pr.review_evidence", side_effect=AssertionError("feedback state should short-circuit")):
            status = self.evaluate_fixture(prs=[pr])
        self.assertIn("PR #19 has active review feedback.", status["reasons"])

    def test_feedback_lookup_exception_keeps_pr_in_feedback_state(self):
        pr = mock_pr(20, decision="COMMENTED", statusCheckRollup=[{
            "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
        }])
        with patch("fetch_pr_feedback.fetch_active_review_feedback", side_effect=RuntimeError("boom")), \
             patch("merge_pr.review_evidence", side_effect=AssertionError("feedback state should short-circuit")):
            status = self.evaluate_fixture(prs=[pr])
        self.assertIn("PR #20 has active review feedback.", status["reasons"])

    def test_api_failure_still_never_reports_complete(self):
        status = evaluate_fleet_status("/definitely/not/a/repository")
        self.assertEqual(status["state"], "error")
        self.assertNotEqual(status["state"], "complete")
        self.assertNotIn("operator_screen", status)


class MergeQueueViewTests(unittest.TestCase):
    """§5.5 merge-queue view wrapping dry-run DoD verdicts."""

    def _full_pr(self, number, *labels, title=None, body="Closes #1"):
        return mock_pr(
            number,
            *labels,
            title=title or f"PR {number}",
            body=body,
            headRefOid="abc123",
        )

    def test_next_queue_action_mapping(self):
        self.assertEqual(next_queue_action(True, None, 0), "merge")
        self.assertEqual(next_queue_action(False, "review", 0), "review")
        self.assertEqual(next_queue_action(False, "ci", 2), "feedback")
        self.assertEqual(next_queue_action(False, "ci", 0), "wait")
        self.assertEqual(next_queue_action(False, "rebased", 0), "wait")

    def test_queue_reports_mergeable_pr(self):
        pr = self._full_pr(10, "author:agent-a", "reviewed-by:agent-b")
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", True, "ok"),
            ("ci", True, "green"),
            ("review", True, "reviewed"),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
            ("accept #1", True, "done"),
        ]

        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: {"unresolved": 0, "unfixed": 0, "withdrawn": 0},
            evaluate_dod_fn=lambda *_a, **_k: (True, gates),
        )
        self.assertTrue(row["ok"])
        self.assertIsNone(row["first_blocking"])
        self.assertEqual(row["next_action"], "merge")
        self.assertEqual(row["verdict"], "pass")
        self.assertEqual(row["author"], "agent-a")
        self.assertEqual(row["reviewer"], "agent-b")

    def test_queue_reports_missing_review_as_first_blocking_gate(self):
        pr = self._full_pr(11, "author:agent-a")
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", True, "ok"),
            ("ci", True, "green"),
            ("review", False, "no independent reviewed-by:<id>"),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
        ]
        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: {"unresolved": 0, "unfixed": 0, "withdrawn": 0},
            evaluate_dod_fn=lambda *_a, **_k: (False, gates),
        )
        self.assertFalse(row["ok"])
        self.assertEqual(row["first_blocking"], "review")
        self.assertEqual(row["next_action"], "review")
        self.assertIn("review:", row["verdict"])

    def test_queue_routes_unresolved_threads_to_feedback(self):
        pr = self._full_pr(12, "author:agent-a", "reviewed-by:agent-b")
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", True, "ok"),
            ("ci", True, "green"),
            ("review", False, "2 unresolved review thread(s)."),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
        ]
        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: {"unresolved": 2, "unfixed": 0, "withdrawn": 0},
            evaluate_dod_fn=lambda *_a, **_k: (False, gates),
        )
        self.assertEqual(row["first_blocking"], "review")
        self.assertEqual(row["next_action"], "feedback")
        self.assertEqual(row["unresolved_threads"], 2)

    def test_queue_reports_red_ci_as_first_blocking_gate(self):
        pr = self._full_pr(13, "author:agent-a", "reviewed-by:agent-b")
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", True, "ok"),
            ("ci", False, "CI is red"),
            ("review", True, "reviewed"),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
        ]
        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: {"unresolved": 0, "unfixed": 0, "withdrawn": 0},
            evaluate_dod_fn=lambda *_a, **_k: (False, gates),
        )
        self.assertEqual(row["first_blocking"], "ci")
        self.assertEqual(row["next_action"], "wait")
        self.assertIn("ci:", row["verdict"])
        self.assertEqual(row["ci"], "red")
        self.assertEqual(row["threads"], "clean")

    def test_queue_table_shows_ci_and_threads_when_earlier_gate_fails(self):
        """CI and thread state stay visible even when verification fails first."""
        pr = self._full_pr(14, "author:agent-a", "reviewed-by:agent-b")
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", False, "missing verified: trailer"),
            ("ci", True, "CI green (4 checks)."),
            ("review", False, "2 unresolved review thread(s)."),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
        ]
        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: {"unresolved": 2, "unfixed": 0, "withdrawn": 0},
            evaluate_dod_fn=lambda *_a, **_k: (False, gates),
        )
        self.assertEqual(row["first_blocking"], "verification")
        self.assertEqual(row["ci"], "green")
        self.assertEqual(row["threads"], "2 open")
        self.assertNotIn("ci:", row["verdict"])
        text = format_merge_queue(
            {"queue": [row], "open_prs_count": 1, "mergeable_count": 0}
        )
        self.assertIn("CI", text.splitlines()[3])
        self.assertIn("Threads", text.splitlines()[3])
        self.assertRegex(text, r"#14\s+agent-a\s+agent-b\s+green\s+2 open")

    def test_build_merge_queue_never_invokes_merge_side_effects(self):
        prs = [self._full_pr(20, "author:a"), self._full_pr(21, "author:b")]
        calls = {"n": 0}

        def fake_row(pr):
            calls["n"] += 1
            return {
                "pr": pr["number"],
                "title": pr["title"],
                "author": "a",
                "reviewer": None,
                "ok": False,
                "first_blocking": "review",
                "verdict": "review: missing",
                "next_action": "review",
                "gates": [],
                "unresolved_threads": 0,
                "ci": "none",
                "threads": "clean",
            }

        with patch("merge_pr.execute_merge") as execute_merge, \
             patch("merge_pr.main") as merge_main:
            payload = build_merge_queue(prs, evaluate_row_fn=fake_row)
            text = format_merge_queue(payload)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(payload["open_prs_count"], 2)
        self.assertEqual(payload["mergeable_count"], 0)
        self.assertIn("Merge queue", text)
        self.assertIn("#20", text)
        self.assertIn("none", text)
        execute_merge.assert_not_called()
        merge_main.assert_not_called()

    def test_unavailable_review_evidence_fails_closed_without_keyerror(self):
        pr = self._full_pr(30, "author:agent-a")
        row = evaluate_queue_row(
            pr,
            fetch_pr_fn=lambda _n: pr,
            linked_issues_fn=lambda _body: [1],
            issue_body_fn=lambda _n: "- [x] done",
            review_evidence_fn=lambda _n: None,
            evaluate_dod_fn=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("evaluate_dod must not run when evidence is unavailable")
            ),
        )
        self.assertFalse(row["ok"])
        self.assertEqual(row["first_blocking"], "review")
        self.assertEqual(row["next_action"], "review")
        self.assertEqual(row["threads"], "unknown")
        self.assertIn("evidence unavailable", row["verdict"])
        # CI still surfaces from the fetched PR even when evidence is missing.
        self.assertIn(row["ci"], {"none", "red", "pending", "green", "—"})

    def test_queue_row_wraps_github_review_evidence_with_authoritative_status(self):
        pr = self._full_pr(31, "author:agent-a")
        pr["reviewDecision"] = "COMMENTED"
        pr["statusCheckRollup"] = [{
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }]
        raw = {
            "head_oid": "abc123",
            "github_review_evidence": True,
            "reviews": [],
            "unresolved": 0,
            "unfixed": 0,
            "withdrawn": 0,
        }
        with patch("merge_pr.fetch_pr", return_value=pr), \
             patch("merge_pr.linked_issues", return_value=[1]), \
             patch("merge_pr.review_evidence", return_value=raw), \
             patch(
                 "merge_pr._with_coderabbit_status",
                 return_value=None,
             ) as refresh, \
             patch(
                 "merge_pr.evaluate_dod",
                 side_effect=AssertionError("DoD should not run without authoritative status"),
             ):
            row = evaluate_queue_row(pr)

        refresh.assert_called_once_with(31, raw)
        self.assertFalse(row["ok"])
        self.assertEqual(row["first_blocking"], "review")
        self.assertEqual(row["next_action"], "review")
        self.assertIn("evidence unavailable", row["verdict"])

    def test_open_pr_list_failure_does_not_look_empty(self):
        payload = build_merge_queue(list_prs_fn=lambda: None)
        self.assertIsNone(payload["queue"])
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()


class DetectStallTests(unittest.TestCase):
    """All three conditions are required; any one missing means no stall."""

    def test_old_merge_with_open_prs_and_agents_is_a_stall(self):
        self.assertTrue(detect_stall(7.0, open_pr_count=5, agent_count=3, stall_hours=4.0))

    def test_recent_merge_is_not_a_stall(self):
        self.assertFalse(detect_stall(0.5, open_pr_count=5, agent_count=3, stall_hours=4.0))

    def test_no_open_prs_is_never_a_stall(self):
        # A quiet board with nothing open is idle, not stalled, however old the
        # last merge is.
        self.assertFalse(detect_stall(99.0, open_pr_count=0, agent_count=3, stall_hours=4.0))

    def test_no_registered_agents_is_never_a_stall(self):
        # Nobody is running; zero throughput is expected, not a fault.
        self.assertFalse(detect_stall(99.0, open_pr_count=5, agent_count=0, stall_hours=4.0))

    def test_failed_merge_lookup_is_not_a_stall(self):
        self.assertFalse(
            detect_stall(
                None, open_pr_count=2, agent_count=1, stall_hours=4.0,
                merge_lookup_ok=False,
            )
        )

    def test_no_merge_at_all_with_work_and_agents_is_a_stall(self):
        self.assertTrue(detect_stall(None, open_pr_count=2, agent_count=1, stall_hours=4.0))

    def test_no_merge_at_all_without_agents_is_not_a_stall(self):
        self.assertFalse(detect_stall(None, open_pr_count=2, agent_count=0, stall_hours=4.0))

    def test_zero_stall_hours_disables_detection(self):
        self.assertFalse(detect_stall(99.0, open_pr_count=5, agent_count=3, stall_hours=0))

    def test_threshold_is_inclusive_at_the_boundary(self):
        self.assertTrue(detect_stall(4.0, open_pr_count=1, agent_count=1, stall_hours=4.0))
        self.assertFalse(detect_stall(3.99, open_pr_count=1, agent_count=1, stall_hours=4.0))


class StallExitCodeTests(unittest.TestCase):
    def test_stalled_exit_code_is_distinct_from_every_other_state(self):
        codes = {EXIT_COMPLETE, EXIT_ERROR, EXIT_WAITING, EXIT_BLOCKED}
        self.assertNotIn(EXIT_STALLED, codes)
        self.assertEqual(EXIT_STALLED, 4)


class StallQuestionTests(unittest.TestCase):
    def _screen(self, **kwargs):
        return fleet_status._stall_question(**kwargs)

    def test_json_payload_carries_hours_and_verdict(self):
        stall = self._screen(
            hours_since_last_merge=7.5, open_pr_count=5,
            agent_count=3, stall_hours=4.0,
        )
        self.assertTrue(stall["stalled"])
        self.assertEqual(stall["hours_since_last_merge"], 7.5)
        self.assertEqual(stall["open_pr_count"], 5)
        self.assertEqual(stall["registered_agents"], 3)
        self.assertEqual(stall["severity"], "attn")

    def test_healthy_board_reports_ok(self):
        stall = self._screen(
            hours_since_last_merge=0.2, open_pr_count=2,
            agent_count=2, stall_hours=4.0,
        )
        self.assertFalse(stall["stalled"])
        self.assertEqual(stall["severity"], "ok")

    def test_failed_merge_lookup_is_ok_not_stalled(self):
        stall = self._screen(
            hours_since_last_merge=None, open_pr_count=5,
            agent_count=3, stall_hours=4.0, merge_lookup_ok=False,
        )
        self.assertFalse(stall["stalled"])
        self.assertEqual(stall["severity"], "ok")
        self.assertIn("unavailable", stall["summary"])

    def test_stall_is_not_a_seventh_operator_question(self):
        # The operator screen is the documented six-question contract.
        screen = build_operator_screen([], [])
        self.assertNotIn("stall", [q["key"] for q in screen["questions"]])
        self.assertEqual(len(screen["questions"]), 6)


class MostRecentMergeTests(unittest.TestCase):
    def test_returns_newest_merged_at_not_first_row(self):
        # gh returns newest-created first, which is not newest-merged.
        rows = [
            {"mergedAt": "2026-08-17T01:00:00Z"},
            {"mergedAt": "2026-08-17T09:00:00Z"},
            {"mergedAt": "2026-08-17T03:00:00Z"},
        ]
        with patch.object(fleet_status, "run_gh_json", return_value=rows):
            newest = most_recent_merge_time()
        self.assertEqual(newest, datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc))

    def test_lookup_uses_merge_date_search_not_creation_order(self):
        captured = []

        def fake_gh(argv, **kwargs):
            captured.append(argv)
            return []

        with patch.object(fleet_status, "run_gh_json", side_effect=fake_gh):
            most_recent_merge_history()
        self.assertEqual(len(captured), 1)
        argv = captured[0]
        self.assertIn("--search", argv)
        search = argv[argv.index("--search") + 1]
        self.assertIn("is:merged", search)
        self.assertIn("merged:>=", search)
        self.assertNotIn("--state", argv)

    def test_lookup_failure_returns_unavailable(self):
        with patch.object(fleet_status, "run_gh_json", return_value=None):
            newest, ok = most_recent_merge_history()
        self.assertIsNone(newest)
        self.assertFalse(ok)
        with patch.object(fleet_status, "run_gh_json", return_value=None):
            self.assertIsNone(most_recent_merge_time())

    def test_empty_successful_history_is_available(self):
        with patch.object(fleet_status, "run_gh_json", return_value=[]):
            newest, ok = most_recent_merge_history()
        self.assertIsNone(newest)
        self.assertTrue(ok)

    def test_malformed_rows_are_skipped(self):
        rows = ["nonsense", {"mergedAt": None}, {"mergedAt": "2026-08-17T05:00:00Z"}]
        with patch.object(fleet_status, "run_gh_json", return_value=rows):
            newest = most_recent_merge_time()
        self.assertEqual(newest, datetime(2026, 8, 17, 5, 0, tzinfo=timezone.utc))


class StallAlertTests(unittest.TestCase):
    def _question(self):
        return fleet_status._stall_question(9.0, 5, 3, 4.0)

    def test_alert_body_carries_no_secrets_or_logs(self):
        text = stall_alert_text(self._question(), ["PR #1 unmet: review"])
        self.assertIn("stopped merging", text)
        self.assertIn("PR #1 unmet: review", text)
        self.assertIn("No merge within 4h", text)
        self.assertNotIn("9.0h", text)
        self.assertNotIn(self._question()["summary"], text)
        for forbidden in ("diff --git", "Traceback", "ghp_", "gho_", "token"):
            self.assertNotIn(forbidden, text)

    def test_slack_invoked_once_with_hitl_and_a_file(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fleet_status, "get_repo_slug", return_value="o/r"), \
             patch.dict(os.environ, {"ARU_PROJECT_ID": "proj_1"}), \
             patch.object(fleet_status, "run_cmd", side_effect=fake_run):
            ok = notify_stall(self._question(), ["PR #1 unmet: review"],
                              tmp, "claude-1", "anthropic", pr=12)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("--event", calls[0])
        self.assertEqual(calls[0][calls[0].index("--event") + 1], "hitl")
        self.assertIn("--decision-file", calls[0])
        self.assertIn("--pr", calls[0])
        self.assertEqual(calls[0][calls[0].index("--pr") + 1], "12")

    def test_alert_file_is_0600_and_removed_afterwards(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            path = cmd[cmd.index("--decision-file") + 1]
            seen["path"] = path
            seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
            return 0, "", ""

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fleet_status, "get_repo_slug", return_value="o/r"), \
             patch.dict(os.environ, {"ARU_PROJECT_ID": "proj_1"}), \
             patch.object(fleet_status, "run_cmd", side_effect=fake_run):
            notify_stall(self._question(), [], tmp, "claude-1", "anthropic", pr=12)
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(os.path.exists(seen["path"]))

    def test_slack_failure_is_reported_without_raising(self):
        # Slack downtime must never halt the GitHub loop.
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fleet_status, "get_repo_slug", return_value="o/r"), \
             patch.dict(os.environ, {"ARU_PROJECT_ID": "proj_1"}), \
             patch.object(fleet_status, "run_cmd", return_value=(1, "", "boom")):
            self.assertFalse(
                notify_stall(self._question(), [], tmp, "a", "anthropic", pr=12)
            )

    def test_missing_pr_skips_notification(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fleet_status, "get_repo_slug", return_value="o/r"), \
             patch.dict(os.environ, {"ARU_PROJECT_ID": "proj_1"}), \
             patch.object(fleet_status, "run_cmd") as run:
            self.assertFalse(notify_stall(self._question(), [], tmp, "a", "anthropic"))
        run.assert_not_called()

    def test_missing_project_id_skips_notification(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fleet_status, "get_repo_slug", return_value="o/r"), \
             patch.dict(os.environ, {"ARU_PROJECT_ID": ""}), \
             patch.object(fleet_status, "run_cmd") as run:
            self.assertFalse(
                notify_stall(self._question(), [], tmp, "a", "anthropic", pr=12)
            )
        run.assert_not_called()

    def test_main_reads_top_level_stall_and_filters_pr_reasons(self):
        question = fleet_status._stall_question(9.0, 5, 3, 4.0)
        status = {
            "state": "stalled",
            "exit_code": EXIT_STALLED,
            "summary": f"STALLED: {question['summary']}",
            "reasons": [
                question["summary"],
                "PR #12 is open and pending review.",
                "Issue #9 is Ready for implementation.",
            ],
            "stall": question,
            "operator_screen": {"questions": []},
            "open_pr_numbers": [12],
        }
        captured = {}

        def fake_notify(q, blocked, repo_dir, agent, family, pr=None):
            captured["question"] = q
            captured["blocked"] = blocked
            captured["pr"] = pr
            return True

        import io
        with patch.object(fleet_status, "evaluate_fleet_status", return_value=status), \
             patch.object(fleet_status, "notify_stall", side_effect=fake_notify), \
             patch.object(fleet_status, "fetch_ci_history", return_value=[]), \
             patch.object(sys, "argv", ["fleet_status.py", "--json"]), \
             patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                fleet_status.main()
        self.assertEqual(raised.exception.code, EXIT_STALLED)
        self.assertIs(captured["question"], question)
        self.assertEqual(captured["blocked"], ["PR #12 is open and pending review."])
        self.assertEqual(captured["pr"], 12)


class RegisteredAgentCountTests(unittest.TestCase):
    def test_unreadable_presence_registry_reports_zero(self):
        # Absence of evidence must not manufacture a stall.
        with patch.object(fleet_status, "registered_agent_count", wraps=fleet_status.registered_agent_count):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(fleet_status.registered_agent_count(tmp), 0)
