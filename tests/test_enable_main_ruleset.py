import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import enable_main_ruleset as ruleset  # noqa: E402


class PayloadContractTests(unittest.TestCase):
    def test_payload_requires_pr_and_ci_without_approvals(self):
        payload = ruleset.ruleset_payload()
        types = [rule["type"] for rule in payload["rules"]]
        self.assertIn("pull_request", types)
        self.assertIn("required_status_checks", types)
        self.assertNotIn("required_linear_history", types)
        pr = next(r for r in payload["rules"] if r["type"] == "pull_request")
        self.assertEqual(pr["parameters"]["required_approving_review_count"], 0)
        checks = next(
            r for r in payload["rules"] if r["type"] == "required_status_checks"
        )
        contexts = [c["context"] for c in checks["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, [ruleset.CI_CONTEXT])

    def test_assert_safe_payload_rejects_linear_history(self):
        payload = ruleset.ruleset_payload()
        payload["rules"].append({"type": "required_linear_history"})
        with self.assertRaises(ValueError):
            ruleset.assert_safe_payload(payload)

    def test_assert_safe_payload_rejects_required_approvals(self):
        payload = ruleset.ruleset_payload()
        for rule in payload["rules"]:
            if rule["type"] == "pull_request":
                rule["parameters"]["required_approving_review_count"] = 1
        with self.assertRaises(ValueError):
            ruleset.assert_safe_payload(payload)


class ApplyTests(unittest.TestCase):
    def test_plan_403_is_blocked_not_success(self):
        err = (
            '{"message":"Upgrade to GitHub Pro or make this repository '
            'public to enable this feature.","status":"403"}'
        )
        with patch.object(ruleset, "find_existing_id", return_value=None), \
                patch.object(ruleset, "_gh_api", return_value=(1, "", err)):
            code = ruleset.apply_ruleset("gillella/Aru_Agentic_SDLC")
        self.assertEqual(code, ruleset.EXIT_BLOCKED)

    def test_create_success(self):
        with patch.object(ruleset, "find_existing_id", return_value=None), \
                patch.object(ruleset, "_gh_api", return_value=(0, "{}", "")):
            code = ruleset.apply_ruleset("gillella/Aru_Agentic_SDLC")
        self.assertEqual(code, 0)

    def test_update_uses_put(self):
        calls = []

        def fake_api(method, path, body=None):
            calls.append((method, path))
            return 0, "{}", ""

        with patch.object(ruleset, "find_existing_id", return_value=42), \
                patch.object(ruleset, "_gh_api", side_effect=fake_api):
            code = ruleset.apply_ruleset("owner/repo")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("PUT", "repos/owner/repo/rulesets/42")])

    def test_dry_run_prints_json_and_does_not_apply(self):
        with patch.object(ruleset, "apply_ruleset") as applied:
            code = ruleset.main(["--dry-run"])
        self.assertEqual(code, 0)
        applied.assert_not_called()

    def test_apply_without_slug_is_an_error(self):
        with patch.object(ruleset, "get_repo_slug", return_value=None), \
                patch.object(ruleset, "apply_ruleset") as applied:
            code = ruleset.main(["--apply"])
        self.assertEqual(code, 1)
        applied.assert_not_called()


class PlanBlockedDetectionTests(unittest.TestCase):
    def test_upgrade_message(self):
        self.assertTrue(
            ruleset.is_plan_blocked(
                "Upgrade to GitHub Pro or make this repository public",
                "",
            )
        )

    def test_unrelated_error_is_not_plan_blocked(self):
        self.assertFalse(ruleset.is_plan_blocked("Not Found", ""))


if __name__ == "__main__":
    unittest.main()
