import json
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
        self.assertNotIn("deletion", types)
        self.assertNotIn("non_fast_forward", types)
        pr = next(r for r in payload["rules"] if r["type"] == "pull_request")
        self.assertEqual(pr["parameters"]["required_approving_review_count"], 0)
        checks = next(
            r for r in payload["rules"] if r["type"] == "required_status_checks"
        )
        contexts = [c["context"] for c in checks["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, [ruleset.ci_context_from_workflow()])

    def test_ci_context_matches_the_workflow_job_name(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn(
            f"name: {ruleset.ci_context_from_workflow()}",
            workflow,
        )

    def test_default_enforcement_is_active(self):
        self.assertEqual(ruleset.ruleset_payload()["enforcement"], "active")

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


class FindExistingTests(unittest.TestCase):
    def test_plan_403_is_blocked_not_missing(self):
        err = "Upgrade to GitHub Pro or make this repository public"
        with patch.object(ruleset, "run_cmd", return_value=(1, "", err)):
            status, existing, _ = ruleset.find_existing_id("owner/repo")
        self.assertEqual(status, "blocked")
        self.assertIsNone(existing)

    def test_other_list_failure_is_an_error(self):
        with patch.object(
            ruleset, "run_cmd", return_value=(1, "", "Bad credentials")
        ):
            status, existing, detail = ruleset.find_existing_id("owner/repo")
        self.assertEqual(status, "error")
        self.assertIsNone(existing)
        self.assertIn("Bad credentials", detail)

    def test_named_ruleset_is_found(self):
        body = json.dumps([{"name": "other", "id": 1},
                           {"name": ruleset.RULESET_NAME, "id": 9}])
        with patch.object(ruleset, "run_cmd", return_value=(0, body, "")):
            status, existing, _ = ruleset.find_existing_id("owner/repo")
        self.assertEqual((status, existing), ("ok", 9))

    def test_empty_list_is_missing_not_error(self):
        with patch.object(ruleset, "run_cmd", return_value=(0, "[]", "")):
            status, existing, _ = ruleset.find_existing_id("owner/repo")
        self.assertEqual((status, existing), ("ok", None))


class ApplyTests(unittest.TestCase):
    def test_plan_403_on_list_does_not_post(self):
        with patch.object(
            ruleset, "find_existing_id",
            return_value=("blocked", None, "Upgrade to GitHub Pro"),
        ), patch.object(ruleset, "_gh_api") as api:
            code = ruleset.apply_ruleset("gillella/Aru_Agentic_SDLC")
        self.assertEqual(code, ruleset.EXIT_BLOCKED)
        api.assert_not_called()

    def test_list_error_does_not_post_a_duplicate(self):
        with patch.object(
            ruleset, "find_existing_id",
            return_value=("error", None, "Bad credentials"),
        ), patch.object(ruleset, "_gh_api") as api:
            code = ruleset.apply_ruleset("owner/repo")
        self.assertEqual(code, 1)
        api.assert_not_called()

    def test_create_success(self):
        with patch.object(
            ruleset, "find_existing_id", return_value=("ok", None, "")
        ), patch.object(ruleset, "_gh_api", return_value=(0, "{}", "")):
            code = ruleset.apply_ruleset("gillella/Aru_Agentic_SDLC")
        self.assertEqual(code, 0)

    def test_update_uses_put(self):
        calls = []

        def fake_api(method, path, body=None):
            calls.append((method, path))
            return 0, "{}", ""

        with patch.object(
            ruleset, "find_existing_id", return_value=("ok", 42, "")
        ), patch.object(ruleset, "_gh_api", side_effect=fake_api):
            code = ruleset.apply_ruleset("owner/repo")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("PUT", "repos/owner/repo/rulesets/42")])

    def test_delete_uses_delete(self):
        calls = []

        def fake_api(method, path, body=None):
            calls.append((method, path))
            return 0, "", ""

        with patch.object(
            ruleset, "find_existing_id", return_value=("ok", 7, "")
        ), patch.object(ruleset, "_gh_api", side_effect=fake_api):
            code = ruleset.delete_ruleset("owner/repo")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("DELETE", "repos/owner/repo/rulesets/7")])

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
