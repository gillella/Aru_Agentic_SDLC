# line-ceiling: 463
import json
import os
import subprocess
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
        with self.assertRaises(acceptance_runner.CommandRejected) as ctx:
            acceptance_runner.validate_command("bash -lc id")
        self.assertIn("allowed runners:", str(ctx.exception))
        self.assertIn("ruff", str(ctx.exception))
        self.assertIn("pytest", str(ctx.exception))

    def test_ruff_check_command_is_allowed(self):
        argv = acceptance_runner.validate_command("ruff check .")
        self.assertEqual(argv, ["ruff", "check", "."])

    def test_ruff_with_flags_and_safe_target_is_allowed(self):
        argv = acceptance_runner.validate_command("ruff check scripts/ tests/ --no-fix")
        self.assertEqual(argv, ["ruff", "check", "scripts/", "tests/", "--no-fix"])

    def test_ruff_disallowed_subcommand_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("ruff clean .")

    def test_ruff_disallowed_flag_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("ruff check --add-noqa .")

    def test_python_dash_c_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 -c print(1)")

    def test_python_other_module_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 -m http.server")

    def test_timeout_is_recorded_as_failure(self):
        body = _issue("- [ ] Slow (verify: `python3 -m unittest tests.ok`)")

        def boom(*_args, **_kwargs):
            raise acceptance_runner.subprocess.TimeoutExpired(
                ["python3"], acceptance_runner.VERIFY_TIMEOUT_SECONDS
            )

        with patch.object(
            acceptance_runner.subprocess, "run", side_effect=boom
        ) as run:
            ok, message, result = acceptance_runner.evaluate_issue(body, cwd=".")
        self.assertFalse(ok)
        self.assertEqual(acceptance_runner.VERIFY_TIMEOUT_SECONDS, 300)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        self.assertIn("timed out after 300s", message)
        self.assertEqual(result["errors"][0][0], "failed")
        self.assertEqual(result["records"][0]["exit_code"], 124)
        self.assertEqual(result["records"][0]["status"], "failed")
        self.assertEqual(result["records"][0]["failure_reason"], "timeout")

    def test_exit_124_cannot_spoof_timeout_message(self):
        body = _issue("- [ ] Fails (verify: `python3 -m unittest tests.ok`)")

        def failed(argv, check=False, cwd=None, evidence=None):
            if evidence is not None:
                evidence.append({
                    "command": argv, "duration_seconds": 0.01,
                    "exit_code": 124, "status": "failed",
                })
            return 124, "", "timed out after 300s"

        ok, message, _ = acceptance_runner.evaluate_issue(
            body, cwd=".", run_cmd_fn=failed
        )
        self.assertFalse(ok)
        self.assertNotIn("timed out after", message)

    def test_side_effectful_python_script_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command(
                "python3 scripts/slack_control_room.py stop"
            )

    def test_absolute_runner_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("/usr/bin/python3 -m unittest tests.x")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(acceptance_runner.CommandRejected):
            acceptance_runner.validate_command("python3 tests/../scripts/merge_pr.py")


class RunTests(unittest.TestCase):
    def test_malicious_command_never_executes(self):
        body = _issue("- [ ] Exploit (verify: `python3 -c print(1)`)")
        with patch.object(acceptance_runner, "_run_verify") as run:
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

        ok, message = merge_pr.check_acceptance(
            96, body, cwd=".", execute=True, run_cmd_fn=fake_run
        )
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

        ok, message = merge_pr.check_acceptance(
            96, body, cwd=".", execute=True, run_cmd_fn=fake_run
        )
        self.assertFalse(ok)
        self.assertIn("failed", message)

    def test_malicious_command_refuses_merge_without_running(self):
        body = _issue("- [x] Exploit (verify: `rm -rf /`)")
        with patch.object(acceptance_runner, "_run_verify") as run:
            ok, message = merge_pr.check_acceptance(96, body, execute=False)
        run.assert_not_called()
        self.assertFalse(ok)
        self.assertIn("illegal", message)

    def test_execute_without_checkout_fails_closed(self):
        body = _issue("- [ ] Runner works (verify: `python3 -m unittest tests.ok`)")
        ok, message = merge_pr.check_acceptance(96, body, cwd=None, execute=True)
        self.assertFalse(ok)
        self.assertIn("no verified PR-head checkout", message)

    def test_evaluate_dod_does_not_execute_commands(self):
        body = _issue("- [ ] Runner works (verify: `python3 -m unittest tests.ok`)")
        pr = {
            "state": "OPEN", "isDraft": False, "body": "Closes #96",
            "headRefOid": "abc", "baseRefOid": "abc",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            "reviews": [], "labels": [{"name": "author:a"}, {"name": "reviewed-by:b"}],
            "additions": 1, "deletions": 0, "files": [],
            "mergeStateStatus": "CLEAN",
        }
        with patch.object(acceptance_runner, "_run_verify") as run:
            ok, gates = merge_pr.evaluate_dod(
                pr, {96: body},
                {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": True},
            )
        run.assert_not_called()
        accept = [gate for gate in gates if gate[0] == "accept #96"][0]
        self.assertTrue(accept[1], accept[2])
        self.assertIn("deferred", accept[2])

    def test_ensure_pr_head_checkout_fails_closed_without_sha(self):
        path, err = merge_pr.ensure_pr_head_checkout({})
        self.assertIsNone(path)
        self.assertIn("missing a head SHA", err)

    def test_ensure_pr_head_checkout_does_not_shallow_a_full_clone(self):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

        def git(cwd, *args, capture=False):
            result = subprocess.run(
                ["git", *args], cwd=cwd, env=env, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            return result.stdout.strip() if capture else None

        with tempfile.TemporaryDirectory() as temp_dir:
            origin = Path(temp_dir) / "origin"
            clone = Path(temp_dir) / "clone"
            origin.mkdir()
            git(origin, "init", "-b", "main")
            (origin / "readme").write_text("base\n")
            git(origin, "add", "readme")
            git(origin, "commit", "-m", "base")
            git(temp_dir, "clone", str(origin), str(clone))
            git(origin, "checkout", "-b", "feat")
            (origin / "readme").write_text("feature\n")
            git(origin, "add", "readme")
            git(origin, "commit", "-m", "feature")
            head = git(origin, "rev-parse", "HEAD", capture=True)
            git(clone, "fetch", "origin", "feat")
            self.assertEqual(
                git(clone, "rev-parse", "--is-shallow-repository", capture=True),
                "false",
            )
            before_base = git(clone, "merge-base", "origin/main", head, capture=True)
            self.assertTrue(before_base)

            path, err = merge_pr.ensure_pr_head_checkout(
                {"headRefOid": head, "headRefName": "feat"},
                repo_root=str(clone),
            )
            self.assertIsNone(err, err)
            self.assertTrue(path)
            try:
                self.assertEqual(git(path, "rev-parse", "HEAD", capture=True), head)
                self.assertEqual(git(path, "status", "--porcelain", capture=True), "")
                listed = git(clone, "worktree", "list", "--porcelain", capture=True)
                self.assertIn(str(Path(path).resolve()), listed)
                self.assertEqual(
                    git(clone, "rev-parse", "--is-shallow-repository", capture=True),
                    "false",
                )
                self.assertEqual(
                    git(clone, "merge-base", "origin/main", head, capture=True),
                    before_base,
                )
            finally:
                merge_pr.release_pr_head_checkout(path, repo_root=str(clone))

            self.assertFalse(Path(path).exists())
            listed = git(clone, "worktree", "list", "--porcelain", capture=True)
            self.assertNotIn(str(Path(path).resolve()), listed)
            self.assertEqual(
                git(clone, "rev-parse", "--is-shallow-repository", capture=True),
                "false",
            )
            self.assertEqual(
                git(clone, "merge-base", "origin/main", head, capture=True),
                before_base,
            )

    def test_persist_acceptance_evidence_writes_records(self):
        records = [{
            "command": ["python3", "-m", "unittest", "tests.ok"],
            "duration_seconds": 0.1,
            "exit_code": 0,
            "status": "passed",
        }]
        pr = {"headRefOid": "abc123"}
        existing = {
            "schema": "aru.verification.v1",
            "status": "passed",
            "head_sha": "abc123",
            "commands": [{
                "command": ["ruff", "check", "."],
                "duration_seconds": 1,
                "exit_code": 0,
                "status": "passed",
            }],
        }
        body = (
            "hello\n"
            f"{merge_pr.VERIFICATION_EVIDENCE_START}\n"
            f"```json\n{json.dumps(existing)}\n```\n"
            f"{merge_pr.VERIFICATION_EVIDENCE_END}\n"
        )
        with patch.object(merge_pr, "_gh_json", return_value={"body": body, "headRefOid": "abc123"}), \
                patch.object(merge_pr, "run_cmd", return_value=(0, "", "")) as edited:
            ok, message = merge_pr.persist_acceptance_evidence(227, pr, records)
        self.assertTrue(ok, message)
        self.assertIn("persisted", message)
        self.assertEqual(edited.call_args.args[0][0:3], ["gh", "pr", "edit"])
        written = edited.call_args.args[0][edited.call_args.args[0].index("--body") + 1]
        self.assertIn("tests.ok", written)

    def test_absent_command_unticked_still_blocks(self):
        body = (
            "## Acceptance Criteria\n\n- [x] first done\n- [ ] second not done\n\n"
            "## Verification\n\nx\n"
        )
        ok, message = merge_pr.check_acceptance(7, body)
        self.assertFalse(ok)
        self.assertIn("unticked", message)

    def test_live_allowlisted_unittest_passes(self):
        target = "tests.test_acceptance_runner.AlwaysPass"
        body = _issue(f"- [ ] Live runner (verify: `python3 -m unittest {target}`)")
        ok, message = merge_pr.check_acceptance(96, body, cwd=str(ROOT), execute=True)
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
