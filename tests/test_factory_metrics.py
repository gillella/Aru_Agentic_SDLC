import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import factory_metrics as fm
import fleet_status as fs


class FactoryMetricsUnitTests(unittest.TestCase):
    def test_parse_iso_valid_and_invalid(self):
        dt = fm.parse_iso("2026-08-13T10:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 10)
        self.assertIsNone(fm.parse_iso("invalid-timestamp"))

    def test_calculate_dwell_times(self):
        events = [
            {"issue_id": 1, "status": "Backlog", "timestamp": "2026-08-13T10:00:00Z"},
            {"issue_id": 1, "status": "Ready", "timestamp": "2026-08-13T12:00:00Z"},
            {"issue_id": 1, "status": "In Progress", "timestamp": "2026-08-13T15:00:00Z"},
            {"issue_id": 1, "status": "Done", "timestamp": "2026-08-13T18:00:00Z"},
            {"issue_id": 2, "status": "Backlog", "timestamp": "2026-08-13T10:00:00Z"},
            {"issue_id": 2, "status": "Ready", "timestamp": "2026-08-13T14:00:00Z"},
        ]
        dwell = fm.calculate_dwell_times(events)
        medians = dwell["median_hours_per_status"]
        self.assertEqual(medians["Backlog"], 3.0)  # (2.0 + 4.0) / 2 = 3.0
        self.assertEqual(medians["Ready"], 3.0)
        self.assertEqual(medians["In Progress"], 3.0)

    def test_calculate_rework_rounds_and_first_pass_yield_over_merged(self):
        prs = [
            {"pr_number": 1, "merged": True, "rework_rounds": 0},
            {"pr_number": 2, "merged": True, "rework_rounds": 0},
            {"pr_number": 3, "merged": True, "rework_rounds": 1},
            {"pr_number": 4, "merged": False, "rework_rounds": 2},  # Unmerged/open PR does not dilute completed yield
        ]
        rework = fm.calculate_rework_rounds(prs)
        self.assertEqual(rework["total_prs_evaluated"], 4)
        self.assertEqual(rework["total_merged_prs"], 3)
        self.assertEqual(rework["first_pass_count"], 2)
        self.assertEqual(rework["first_pass_yield_percent"], 66.67)  # 2 / 3 = 66.67%
        self.assertEqual(rework["median_rework_rounds"], 0.0)
        self.assertEqual(rework["max_rework_rounds"], 1)
        self.assertEqual(rework["rework_distribution"], {0: 2, 1: 1})

    def test_fetch_paginated_gh_api_stream_decoding(self):
        with patch("factory_metrics.run_cmd", return_value=(0, '[{"id": 1}][{"id": 2}]', "")):
            items = fm.fetch_paginated_gh_api("test/endpoint")
            self.assertIsNotNone(items)
            self.assertEqual(len(items), 2)

    def test_fetch_paginated_gh_api_error_returns_none(self):
        with patch("factory_metrics.run_cmd", return_value=(1, "", "API rate limit exceeded")):
            with patch("sys.stderr.write"):
                items = fm.fetch_paginated_gh_api("test/endpoint")
                self.assertIsNone(items)

    @patch("factory_metrics.fetch_paginated_gh_api")
    def test_fetch_github_telemetry_filters_window_and_detects_review_cycles(self, mock_api):
        def side_effect(endpoint):
            if "pulls?state=all" in endpoint:
                return [
                    # PR within window with multiple root comment threads from 1 review session
                    {"number": 1, "created_at": "2026-08-14T00:00:00Z", "merged_at": "2026-08-14T02:00:00Z"},
                    # PR outside window
                    {"number": 2, "created_at": "2020-01-01T00:00:00Z", "merged_at": "2020-01-01T02:00:00Z"},
                ]
            elif "pulls/1/reviews" in endpoint:
                return [{"id": 501, "state": "COMMENTED"}]
            elif "pulls/1/comments" in endpoint:
                # 3 root comments, all submitted under pull_request_review_id 501
                return [
                    {"id": 101, "pull_request_review_id": 501, "in_reply_to_id": None},
                    {"id": 102, "pull_request_review_id": 501, "in_reply_to_id": 101},
                    {"id": 103, "pull_request_review_id": 501, "in_reply_to_id": None},
                    {"id": 104, "pull_request_review_id": 501, "in_reply_to_id": None},
                ]
            elif "issues?state=all" in endpoint:
                return [
                    {"number": 5, "pull_request": None, "created_at": "2026-08-14T00:00:00Z"}
                ]
            elif "issues/5/timeline" in endpoint:
                return [
                    {"event": "labeled", "label": {"name": "status:ready"}, "created_at": "2026-08-14T00:00:00Z"},
                    {"event": "labeled", "label": {"name": "status:in-progress"}, "created_at": "2026-08-14T01:00:00Z"},
                ]
            return []

        mock_api.side_effect = side_effect
        issue_events, pr_list = fm.fetch_github_telemetry(window_days=7)
        self.assertEqual(len(pr_list), 1)
        self.assertEqual(pr_list[0]["pr_number"], 1)
        # N root threads under 1 review session must equal 1 rework round (not N)
        self.assertEqual(pr_list[0]["rework_rounds"], 1)
        self.assertEqual(len(issue_events), 2)
        self.assertEqual(issue_events[0]["status"], "ready")

    @patch("factory_metrics.fetch_paginated_gh_api")
    def test_fetch_github_telemetry_builds_closed_issue_lifecycle_details(self, mock_api):
        def side_effect(endpoint):
            if "pulls?state=all" in endpoint:
                return []
            if "issues?state=all" in endpoint:
                return [{
                    "number": 7, "title": "feat: measured", "pull_request": None,
                    "created_at": "2026-08-14T00:00:00Z", "updated_at": "2026-08-14T03:00:00Z",
                    "closed_at": "2026-08-14T03:00:00Z", "labels": [{"name": "type:feat"}],
                }]
            if "issues/7/timeline" in endpoint:
                return [
                    {"event": "labeled", "label": {"name": "agent:codex-1"}, "created_at": "2026-08-14T01:00:00Z"},
                    {"event": "labeled", "label": {"name": "status:in-progress"}, "created_at": "2026-08-14T01:05:00Z"},
                    {"event": "labeled", "label": {"name": "status:done"}, "created_at": "2026-08-14T03:00:00Z"},
                ]
            return []

        mock_api.side_effect = side_effect
        events, _ = fm.fetch_github_telemetry(window_days=7, include_closed_details=True)
        detail = next(event for event in events if event.get("event_type") == "closed_issue")
        self.assertEqual(detail["agents"], ["codex-1"])
        self.assertEqual(detail["claim_started_at"], "2026-08-14T01:05:00Z")
        self.assertEqual(detail["done_at"], "2026-08-14T03:00:00Z")
        self.assertEqual(detail["issue_type"], "feat")

    def test_format_text_report(self):
        dwell = {"median_hours_per_status": {"Backlog": 2.5, "Ready": 1.0}}
        rework = {
            "total_prs_evaluated": 3,
            "total_merged_prs": 2,
            "first_pass_count": 1,
            "first_pass_yield_percent": 50.0,
            "median_rework_rounds": 0.5,
            "max_rework_rounds": 1,
            "rework_distribution": {0: 1, 1: 1},
        }
        report = fm.format_text_report(dwell, rework)
        self.assertIn("Factory Telemetry", report)
        self.assertIn("Backlog", report)
        self.assertIn("50.00%", report)

    @patch("factory_metrics.collect_factory_metrics")
    def test_main_json_output(self, mock_collect):
        mock_collect.return_value = {
            "dwell_time": {"median_hours_per_status": {}},
            "rework_yield": {"first_pass_yield_percent": 100.0},
            "closed_issues": {"closed_issue_count": 1},
        }

        with patch("sys.argv", ["factory_metrics.py", "--json"]), patch("sys.stdout.write") as mock_stdout:
            exit_code = fm.main()
            self.assertEqual(exit_code, 0)
            written = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(written)
            self.assertIn("dwell_time", data)
            self.assertIn("rework_yield", data)
            self.assertEqual(data["rework_yield"]["first_pass_yield_percent"], 100.0)

    @patch("factory_metrics.collect_factory_metrics", side_effect=RuntimeError("API error"))
    def test_main_handles_api_failure(self, mock_collect):
        with patch("sys.argv", ["factory_metrics.py"]), patch("sys.stderr.write"):
            exit_code = fm.main()
            self.assertEqual(exit_code, 1)

    def test_load_local_usage_jsonl_and_reject_invalid_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            path.write_text('{"issue_number": 7, "total_tokens": 10}\n{"issue_number": 8, "cost_usd": 1.25}\n')
            self.assertEqual([row["issue_number"] for row in fm.load_local_usage(str(path))], [7, 8])
            path.write_text('[1, 2, 3]')
            with self.assertRaises(RuntimeError):
                fm.load_local_usage(str(path))
            path.write_text('{"issue_number": "7", "total_tokens": 10}')
            with self.assertRaises(RuntimeError):
                fm.load_local_usage(str(path))

    @patch("factory_metrics.fetch_paginated_gh_api")
    def test_fetch_ci_runs_ignores_invalid_and_out_of_window_timestamps(self, mock_api):
        mock_api.return_value = [
            {"id": 1, "created_at": "2026-08-14T00:00:00Z"},
            {"id": 2, "created_at": "invalid"},
            {"id": 3, "created_at": "2020-01-01T00:00:00Z"},
        ]
        fixed_now = fm.parse_iso("2026-08-14T12:00:00Z")
        with patch("factory_metrics.datetime", wraps=fm.datetime) as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            runs = fm.fetch_ci_runs(1)
        self.assertEqual([run["id"] for run in runs], [1])

    def test_closed_issue_metrics_attribute_measurement_unavailable_and_outliers(self):
        closed = [
            {
                "event_type": "closed_issue", "issue_id": number, "title": f"Issue {number}",
                "closed_at": "2026-08-14T02:00:00Z", "claim_started_at": "2026-08-14T01:00:00Z",
                "done_at": "2026-08-14T02:00:00Z", "agents": [], "issue_type": "feat",
            }
            for number in range(1, 5)
        ]
        prs = [
            {
                "pr_number": number, "merged": True, "issue_numbers": [number],
                "agent": "codex-1", "family": "openai", "rework_rounds": 0,
            }
            for number in range(1, 5)
        ]
        ci_runs = [{"pull_requests": [{"number": 1}]}, {"pull_requests": [{"number": 1}]}]
        usage = [
            {"issue_number": 1, "agent": "codex-1", "family": "openai", "total_tokens": 100, "cost_usd": 1.0},
            {"issue_number": 2, "agent": "codex-1", "family": "openai", "input_tokens": 60, "output_tokens": 40, "cost_usd": 1.0},
            {"issue_number": 3, "agent": "codex-1", "family": "openai", "total_tokens": 5000, "cost_usd": 20.0},
        ]
        result = fm.build_closed_issue_metrics(closed, prs, ci_runs, usage, 30)
        first, _, third, fourth = result["issues"]
        self.assertEqual(first["agent"], "codex-1")
        self.assertEqual(first["family"], "openai")
        self.assertEqual(first["cycle_time_hours"], 1.0)
        self.assertEqual(first["ci_runs"], 2)
        self.assertEqual(first["tokens"]["value"], 100)
        self.assertEqual(first["human_oversight_minutes"]["availability"], "unavailable")
        self.assertTrue(third["outlier"])
        self.assertIn("cost_usd", third["outlier_reasons"])
        self.assertEqual(fourth["tokens"]["availability"], "unavailable")
        self.assertEqual(fourth["cost_usd"]["availability"], "unavailable")
        self.assertEqual(result["cost_per_closed_issue"]["average_usd_measured"], 7.333333)
        self.assertEqual(result["cost_per_closed_issue"]["unavailable_issue_count"], 1)
        self.assertEqual(result["by_issue_type"]["feat"]["closed_issues"], 4)

    @patch("factory_metrics.collect_factory_metrics")
    @patch("fleet_status.evaluate_fleet_status")
    def test_fleet_status_metrics_are_opt_in(self, mock_status, mock_collect):
        mock_status.return_value = {"state": "waiting", "exit_code": 2, "summary": "waiting"}
        mock_collect.return_value = {"closed_issues": {"closed_issue_count": 3, "window_days": 7}}
        with patch("sys.argv", ["fleet_status.py", "--json", "--metrics", "--metrics-window-days", "7"]), \
             patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as raised:
            fs.main()
        self.assertEqual(raised.exception.code, 2)
        payload = json.loads(mock_print.call_args.args[0])
        self.assertEqual(payload["factory_metrics"]["closed_issue_count"], 3)
        mock_collect.assert_called_once_with(7, None)


if __name__ == "__main__":
    unittest.main()
