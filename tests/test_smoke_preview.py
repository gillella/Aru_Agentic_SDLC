#!/usr/bin/env python3
"""Hermetic unit tests for scripts/smoke_preview.py."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smoke_preview as sp


class TestSmokeUrlValidation(unittest.TestCase):
    def test_validate_preview_url_valid(self):
        self.assertTrue(sp.validate_preview_url("https://octocat.github.io/my-app/"))
        self.assertTrue(sp.validate_preview_url("http://localhost:8080/index.html"))
        self.assertTrue(sp.validate_preview_url("https://preview.example.com/test"))

    def test_validate_preview_url_invalid(self):
        self.assertFalse(sp.validate_preview_url(""))
        self.assertFalse(sp.validate_preview_url(None))
        self.assertFalse(sp.validate_preview_url("not a url"))
        self.assertFalse(sp.validate_preview_url("ftp://example.com/file"))
        self.assertFalse(sp.validate_preview_url("javascript:alert(1)"))
        self.assertFalse(sp.validate_preview_url("https://example.com/path with spaces"))
        self.assertFalse(sp.validate_preview_url("https://example.com/<script>"))


class TestSmokeBooleanHelpers(unittest.TestCase):
    def test_is_truthy(self):
        self.assertTrue(sp.is_truthy("true"))
        self.assertTrue(sp.is_truthy("True"))
        self.assertTrue(sp.is_truthy("TRUE"))
        self.assertTrue(sp.is_truthy("1"))
        self.assertTrue(sp.is_truthy("yes"))
        self.assertTrue(sp.is_truthy("y"))
        self.assertFalse(sp.is_truthy("false"))
        self.assertFalse(sp.is_truthy("0"))
        self.assertFalse(sp.is_truthy(None))
        self.assertFalse(sp.is_truthy(""))

    def test_is_falsy(self):
        self.assertTrue(sp.is_falsy("false"))
        self.assertTrue(sp.is_falsy("False"))
        self.assertTrue(sp.is_falsy("FALSE"))
        self.assertTrue(sp.is_falsy("0"))
        self.assertTrue(sp.is_falsy("no"))
        self.assertTrue(sp.is_falsy("n"))
        self.assertFalse(sp.is_falsy("true"))
        self.assertFalse(sp.is_falsy("1"))
        self.assertFalse(sp.is_falsy(None))
        self.assertFalse(sp.is_falsy(""))


class TestSmokeScenarioEvaluation(unittest.TestCase):
    def test_evaluate_html_scenarios_valid_page(self):
        html = "<!DOCTYPE html><html><head><title>Aru Visualizer</title></head><body><h1>Welcome</h1></body></html>"
        results = sp.evaluate_html_scenarios(html)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].passed)
        self.assertEqual(results[0].name, "HTML Document Structure")
        self.assertTrue(results[1].passed)
        self.assertEqual(results[1].name, "Page Title Declaration")
        self.assertIn("Aru Visualizer", results[1].details)

    def test_evaluate_html_scenarios_missing_structure_and_title(self):
        html = "Just plain text without tags"
        results = sp.evaluate_html_scenarios(html)
        self.assertFalse(results[0].passed)
        self.assertFalse(results[1].passed)

    def test_evaluate_html_scenarios_custom_rules(self):
        html = "<!DOCTYPE html><html><head><title>Test App</title></head><body><div id='app'>Live</div></body></html>"
        scenarios = [
            {"name": "Check App Container", "contains": "id=['\"]app['\"]"},
            {"name": "Check No Error Boundary", "not_contains": "Unhandled Runtime Error"},
            {"name": "Min Content Length", "min_length": 30},
        ]
        results = sp.evaluate_html_scenarios(html, scenarios=scenarios)
        self.assertEqual(len(results), 5)
        for res in results:
            self.assertTrue(res.passed, f"Scenario '{res.name}' should pass")

    def test_evaluate_html_scenarios_custom_rule_failure(self):
        html = "<!DOCTYPE html><html><head><title>Test App</title></head><body>Unhandled Runtime Error occurred</body></html>"
        scenarios = [
            {"name": "Check No Error Boundary", "not_contains": "Unhandled Runtime Error"},
            {"name": "Expected Missing Element", "contains": "id='footer'"},
            {"name": "Min Content Length", "min_length": 500},
        ]
        results = sp.evaluate_html_scenarios(html, scenarios=scenarios)
        # Builtins pass
        self.assertTrue(results[0].passed)
        self.assertTrue(results[1].passed)
        # Custom rules fail
        self.assertFalse(results[2].passed)
        self.assertFalse(results[3].passed)
        self.assertFalse(results[4].passed)


class TestSmokePreviewFetch(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_fetch_preview_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.read.return_value = b"<!DOCTYPE html><html><head><title>OK</title></head><body>Hello</body></html>"
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        status, headers, body, err = sp.fetch_preview_with_retry("https://example.github.io/app/", max_retries=1)
        self.assertEqual(status, 200)
        self.assertIn("Content-Type", headers)
        self.assertIn("<title>OK</title>", body)
        self.assertEqual(err, "")

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_fetch_preview_http_404_retries_and_returns_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://example.github.io/app/",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b""),
        )

        status, headers, body, err = sp.fetch_preview_with_retry("https://example.github.io/app/", max_retries=2)
        self.assertEqual(status, 404)
        self.assertIn("HTTP 404 Not Found", err)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_fetch_preview_url_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = urllib.error.URLError(reason="Connection refused")

        status, headers, body, err = sp.fetch_preview_with_retry("https://example.github.io/app/", max_retries=1)
        self.assertEqual(status, 0)
        self.assertIn("Connection refused", err)


class TestRunSmokeCheck(unittest.TestCase):
    def test_library_skip_visibly_with_is_library(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as summary_file:
            summary_path = summary_file.name

        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary_path}):
                outcome = sp.run_smoke_check(is_library="true", commit_sha="abc1234")
            self.assertTrue(outcome.success)
            self.assertTrue(outcome.skipped)
            self.assertEqual(outcome.status_code, 0)
            self.assertIn("library", outcome.message.lower())

            with open(summary_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Smoke & E2E Preview Stage: Visibly Skipped", content)
            self.assertIn("abc1234", content)
        finally:
            if os.path.exists(summary_path):
                os.unlink(summary_path)

    def test_library_skip_visibly_with_has_preview_false(self):
        outcome = sp.run_smoke_check(has_preview="false")
        self.assertTrue(outcome.success)
        self.assertTrue(outcome.skipped)

    def test_missing_or_invalid_url_for_runnable_product_fails(self):
        outcome = sp.run_smoke_check(url="", has_preview="true", is_library="false")
        self.assertFalse(outcome.success)
        self.assertFalse(outcome.skipped)
        self.assertIn("Invalid or missing preview URL", outcome.message)

    @patch("smoke_preview.fetch_preview_with_retry")
    def test_runnable_preview_success(self, mock_fetch):
        mock_fetch.return_value = (
            200,
            {"content-type": "text/html"},
            "<!DOCTYPE html><html><head><title>App</title></head><body><h1>Content</h1></body></html>",
            "",
        )
        outcome = sp.run_smoke_check(
            url="https://example.github.io/app/",
            has_preview="true",
            is_library="false",
            commit_sha="fedcba9876543210",
        )
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.skipped)
        self.assertEqual(outcome.status_code, 200)
        self.assertIn("All smoke and E2E scenarios passed", outcome.message)

    @patch("smoke_preview.fetch_preview_with_retry")
    def test_runnable_preview_http_failure_blocks_promotion(self, mock_fetch):
        mock_fetch.return_value = (500, {}, "", "HTTP 500 Internal Server Error")
        outcome = sp.run_smoke_check(
            url="https://example.github.io/app/",
            has_preview="true",
            is_library="false",
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.status_code, 500)

    @patch("smoke_preview.fetch_preview_with_retry")
    def test_runnable_preview_scenario_failure_blocks_promotion(self, mock_fetch):
        mock_fetch.return_value = (
            200,
            {"content-type": "text/html"},
            "plain text body without html tags",
            "",
        )
        outcome = sp.run_smoke_check(
            url="https://example.github.io/app/",
            has_preview="true",
            is_library="false",
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.status_code, 200)
        self.assertIn("scenarios failed", outcome.message.lower())


class TestSmokeCli(unittest.TestCase):
    def test_main_library_skip_returns_0(self):
        with patch.object(sys, "argv", ["smoke_preview.py", "--is-library", "true"]):
            exit_code = sp.main()
            self.assertEqual(exit_code, 0)

    @patch("smoke_preview.run_smoke_check")
    def test_main_failure_returns_1(self, mock_check):
        mock_check.return_value = sp.SmokeOutcome(
            success=False,
            skipped=False,
            status_code=500,
            url="https://example.com",
            message="Failed",
            scenario_results=[],
        )
        with patch.object(sys, "argv", ["smoke_preview.py", "--url", "https://example.com"]):
            exit_code = sp.main()
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
