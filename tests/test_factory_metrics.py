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
            {"status": "Backlog", "timestamp": "2026-08-13T10:00:00Z"},
            {"status": "Ready", "timestamp": "2026-08-13T12:00:00Z"},
            {"status": "In Progress", "timestamp": "2026-08-13T15:00:00Z"},
            {"status": "Done", "timestamp": "2026-08-13T18:00:00Z"},
        ]
        dwell = fm.calculate_dwell_times(events)
        medians = dwell["median_hours_per_status"]
        self.assertEqual(medians["Backlog"], 2.0)
        self.assertEqual(medians["Ready"], 3.0)
        self.assertEqual(medians["In Progress"], 3.0)

    def test_calculate_rework_rounds_and_first_pass_yield(self):
        prs = [
            {"pr_number": 1, "merged": True, "rework_rounds": 0},
            {"pr_number": 2, "merged": True, "rework_rounds": 0},
            {"pr_number": 3, "merged": True, "rework_rounds": 1},
            {"pr_number": 4, "merged": False, "rework_rounds": 2},
        ]
        rework = fm.calculate_rework_rounds(prs)
        self.assertEqual(rework["total_prs"], 4)
        self.assertEqual(rework["first_pass_count"], 2)
        self.assertEqual(rework["first_pass_yield_percent"], 50.0)
        self.assertEqual(rework["median_rework_rounds"], 0.5)
        self.assertEqual(rework["max_rework_rounds"], 2)
        self.assertEqual(rework["rework_distribution"], {0: 2, 1: 1, 2: 1})

    def test_format_text_report(self):
        dwell = {"median_hours_per_status": {"Backlog": 2.5, "Ready": 1.0}}
        rework = {
            "total_prs": 2,
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
            [{"status": "Ready", "timestamp": "2026-08-13T10:00:00Z"}, {"status": "In Progress", "timestamp": "2026-08-13T12:00:00Z"}],
            [{"pr_number": 10, "merged": True, "rework_rounds": 0}],
        )

        with patch("sys.argv", ["factory_metrics.py", "--json"]), patch("sys.stdout.write") as mock_stdout:
            fm.main()
            written = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(written)
            self.assertIn("dwell_time", data)
            self.assertIn("rework_yield", data)
            self.assertEqual(data["rework_yield"]["first_pass_yield_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
