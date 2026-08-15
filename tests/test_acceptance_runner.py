import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import acceptance_runner  # noqa: E402
import merge_pr  # noqa: E402


class AlwaysPass(unittest.TestCase):
    """Live target for an allowlisted `python3 -m unittest` verify command."""

    def test_ok(self):
        self.assertTrue(True)


def _issue(criteria: str) -> str:
    return (
        "## Summary\nDo a thing.\n\n"
        "## Acceptance Criteria\n"
        f"{criteria}\n\n"
        "## Verification\n`python3 -m unittest`\n"
    )


class ParseTests(unittest.TestCase):
    def test_backticked_command_is_extracted(self):
        body = _issue("- [ ] Rejects a redirect (verify: `python3 -m unittest tests.test_foo`)")
        parsed = acceptance_runner.parse_criteria(body)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(
            parsed[0].argv, ["python3", "-m", "unittest", "tests.test_foo"]
        )
        self.assertFalse(parsed[0].ticked)

    def test_indented_command_is_extracted(self):
        body = _issue(
            "- [ ] Rejects a redirect whose target starts with a variable\n"
            "      verify: python3 -m unittest tests.test_enforce_touches.RedirectTests"
        )
        parsed = acceptance_runner.parse_criteria(body)
        self.assertEqual(
            parsed[0].argv,
            ["python3", "-m", "unittest", "tests.test_enforce_touches.RedirectTests"],
        )

    def test_prose_verify_without_backticks_is_absent_command(self):
        body = _issue(
            "- [x] Findings artifact attached (verify: artifact comment or path present)"
        )
        parsed = acceptance_runner.parse_criteria(body)
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0].command)
        self.assertIsNone(parsed[0].argv)
        self.assertTrue(parsed[0].ticked)

    def test_no_acceptance_section_is_empty(self):
        self.assertEqual(acceptance_runner.parse_criteria("no headings here"), [])


class ValidateTests(unittest.TestCase):
    def test_shell_metacharacters_are_rejected(self):
        for command in (
            "python3 -m unittest tests.x && id",
            "python3 -m unittest tests.x; curl evil.test",
            "python3 -m unittest tests.x | tee /tmp/out",
            "python3 -c 'import os; os.system(\"id\")'",
            "python3 -m unittest tests.x $(id)",
        ):
            with self.subTest(command=command):
                with self.assertRaises(acceptance_runner.CommandRejected):
                    acceptance_runner.validate_command(command)

    def test_non_allowlisted_runner_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("bash -lc id")

    def test_python_dash_c_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 -c print(1)")

    def test_python_other_module_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 -m http.server")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 tests/../scripts/merge_pr.py")

    def test_absolute_runner_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("/usr/bin/python3 -m unittest tests.x")


class RunTests(unittest.TestCase):
    def test_malicious_command_never_executes(self):
        body = _issue("- [ ] Exploit (verify: `python3 -c print(1)`)")
        with patch.object(acceptance_runner, "run_cmd") as run:
            result = acceptance_runner.run_issue(body, cwd=".")
        run.assert_not_called()
        self.assertEqual(result["errors"][0][0], "rejected")
        self.assertEqual(result["records"], [])

    def test_pass_records_evidence_without_requiring_a_tick(self):
        body = _issue("- [ ] Unticked but verified (verify: `python3 -m unittest tests.ok`)")

        def fake_run(argv, check=False, cwd=None, evidence=None):
            if evidence is not None:
                evidence.append({
                    "command": argv,
                    "duration_seconds": 0.01,
                    "exit_code": 0,
                    "status": "passed",
                })
            return 0, "", ""

        result = acceptance_runner.run_issue(body, cwd=".", run_cmd_fn=fake_run)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["records"][0]["exit_code"], 0)
        parsed = acceptance_runner.parse_criteria(body)
        pending = [item for item in parsed if item.argv is None and not item.ticked]
        self.assertFalse(pending)
        self.assertIn("Unticked", parsed[0].text)

    def test_fail_is_reported(self):
        body = _issue("- [x] Must pass (verify: `python3 -m unittest tests.missing`)")

        def fake_run(argv, check=False, cwd=None, evidence=None):
            if evidence is not None:
                evidence.append({
                    "command": argv,
                    "duration_seconds": 0.01,
                    "exit_code": 1,
                    "status": "failed",
                })
            return 1, "", "boom"

        result = acceptance_runner.run_issue(body, cwd=".", run_cmd_fn=fake_run)
        self.assertEqual(result["errors"][0][0], "failed")

    def test_absent_command_keeps_checkbox_behaviour(self):
        ticked = _issue("- [x] Docs updated")
        unticked = _issue("- [ ] Docs updated")
        ok, _, _ = acceptance_runner.evaluate_issue(ticked, cwd=".")
        self.assertTrue(ok)
        ok, message, _ = acceptance_runner.evaluate_issue(unticked, cwd=".")
        self.assertFalse(ok)
        self.assertIn("unticked", message)

    def test_merge_into_evidence_appends_and_rolls_status(self):
        evidence = {
            "schema": "aru.verification.v1",
            "status": "passed",
            "head_sha": "abc",
            "commands": [{
                "command": ["ruff", "check", "."],
                "duration_seconds": 1,
                "exit_code": 0,
                "status": "passed",
            }],
        }
        failed = [{
            "command": ["python3", "-m", "unittest", "tests.x"],
            "duration_seconds": 0.2,
            "exit_code": 1,
            "status": "failed",
        }]
        merged = acceptance_runner.merge_into_evidence(evidence, failed)
        self.assertEqual(len(merged["commands"]), 2)
        self.assertEqual(merged["status"], "failed")
        self.assertEqual(merged["schema"], "aru.verification.v1")


class MergeGateTests(unittest.TestCase):
    def test_verify_pass_does_not_require_tick(self):
        body = _issue("- [ ] Runner works (verify: `python3 -m unittest tests.ok`)")

        def fake_run(argv, check=False, cwd=None, evidence=None):
            if evidence is not None:
                evidence.append({
                    "command": argv,
                    "duration_seconds": 0.01,
                    "exit_code": 0,
                    "status": "passed",
                })
            return 0, "", ""

        with patch.object(acceptance_runner, "run_cmd", side_effect=fake_run):
            ok, message = merge_pr.check_acceptance(96, body, cwd=".")
        self.assertTrue(ok)
        self.assertIn("verify:", message)

    def test_verify_fail_refuses_merge(self):
        body = _issue("- [x] Runner works (verify: `python3 -m unittest tests.ok`)")

        def fake_run(argv, check=False, cwd=None, evidence=None):
            if evidence is not None:
                evidence.append({
                    "command": argv,
                    "duration_seconds": 0.01,
                    "exit_code": 1,
                    "status": "failed",
                })
            return 1, "", "nope"

        with patch.object(acceptance_runner, "run_cmd", side_effect=fake_run):
            ok, message = merge_pr.check_acceptance(96, body, cwd=".")
        self.assertFalse(ok)
        self.assertIn("failed", message)

    def test_malicious_command_refuses_merge_without_running(self):
        body = _issue("- [x] Exploit (verify: `rm -rf /`)")
        with patch.object(acceptance_runner, "run_cmd") as run:
            ok, message = merge_pr.check_acceptance(96, body, cwd=".")
        run.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("illegal", message)

    def test_absent_command_unticked_still_blocks(self):
        body = (
            "## Acceptance Criteria\n\n- [x] first done\n- [ ] second not done\n\n"
            "## Verification\n\nx\n"
        )
        ok, message = merge_pr.check_acceptance(7, body, cwd=".")
        self.assertFalse(ok)
        self.assertIn("unticked", message)

    def test_live_allowlisted_unittest_passes(self):
        target = "tests.test_acceptance_runner.AlwaysPass"
        body = _issue(f"- [ ] Live runner (verify: `python3 -m unittest {target}`)")
        ok, message = merge_pr.check_acceptance(96, body, cwd=str(ROOT))
        self.assertTrue(ok, message)


class CliTests(unittest.TestCase):
    def test_json_rejected_command_exits_2(self):
        body = _issue("- [ ] Exploit (verify: `python3 -c print(1)`)")
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(body)
            path = handle.name
        try:
            argv = [
                "acceptance_runner.py", "--body-file", path, "--cwd", ".", "--json",
            ]
            with patch.object(sys, "argv", argv):
                code = acceptance_runner.main()
            self.assertEqual(code, 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
