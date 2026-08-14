import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import factory_metrics as fm


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
    def test_fetch_github_telemetry_filters_window_and_detects_comment_threads(self, mock_api):
        def side_effect(endpoint):
            if "pulls?state=all" in endpoint:
                return [
                    # PR within window with comment threads
                    {"number": 1, "created_at": "2026-08-14T00:00:00Z", "merged_at": "2026-08-14T02:00:00Z"},
                    # PR outside window
                    {"number": 2, "created_at": "2020-01-01T00:00:00Z", "merged_at": "2020-01-01T02:00:00Z"},
                ]
            elif "pulls/1/reviews" in endpoint:
                return [{"state": "COMMENTED"}]
            elif "pulls/1/comments" in endpoint:
                return [{"id": 101, "in_reply_to_id": None}, {"id": 102, "in_reply_to_id": 101}]
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
        self.assertEqual(pr_list[0]["rework_rounds"], 1)  # 1 distinct thread
        self.assertEqual(len(issue_events), 2)
        self.assertEqual(issue_events[0]["status"], "ready")

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

    @patch("factory_metrics.fetch_github_telemetry")
    def test_main_json_output(self, mock_fetch):
        mock_fetch.return_value = (
            [{"issue_id": 1, "status": "Ready", "timestamp": "2026-08-13T10:00:00Z"}, {"issue_id": 1, "status": "In Progress", "timestamp": "2026-08-13T12:00:00Z"}],
            [{"pr_number": 10, "merged": True, "rework_rounds": 0}],
        )

        with patch("sys.argv", ["factory_metrics.py", "--json"]), patch("sys.stdout.write") as mock_stdout:
            exit_code = fm.main()
            self.assertEqual(exit_code, 0)
            written = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(written)
            self.assertIn("dwell_time", data)
            self.assertIn("rework_yield", data)
            self.assertEqual(data["rework_yield"]["first_pass_yield_percent"], 100.0)

    @patch("factory_metrics.fetch_github_telemetry", side_effect=RuntimeError("API error"))
    def test_main_handles_api_failure(self, mock_fetch):
        with patch("sys.argv", ["factory_metrics.py"]), patch("sys.stderr.write"):
            exit_code = fm.main()
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
