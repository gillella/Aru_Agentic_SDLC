import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import create_pr  # noqa: E402
import common  # noqa: E402
import merge_pr  # noqa: E402


class AgentFlagTests(unittest.TestCase):
    """--agent is what the merge gate reads to tell peer review from self-review.

    It was optional and defaulted to "", so a caller who simply forgot opened a
    PR with no author:<id>, and check_reviews then accepted any review on it.
    An identity a gate depends on cannot be opt-in.
    """

    def test_missing_agent_is_refused_at_the_cli(self):
        argv = ["create_pr.py", "--issue", "7", "--title", "t", "--body", "b"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr") as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertNotEqual(caught.exception.code, 0)
        opened.assert_not_called()  # nothing was opened unstamped

    def test_empty_agent_is_refused_at_the_cli(self):
        # required=True only proves the token was typed. `--agent ""` slips past
        # it and lands an unstamped PR, which is the hole this change closes.
        # The realistic source is `--agent "$AGENT_ID"` with the variable unset.
        for empty in ("", "   ", "\t"):
            with self.subTest(agent=repr(empty)):
                argv = ["create_pr.py", "--issue", "7", "--title", "t",
                        "--body", "b", "--agent", empty]
                with patch.object(sys, "argv", argv), \
                        patch.object(create_pr, "create_pr") as opened:
                    with self.assertRaises(SystemExit) as caught:
                        create_pr.main()
                self.assertNotEqual(caught.exception.code, 0)
                opened.assert_not_called()

    def test_surrounding_whitespace_is_stripped_from_agent(self):
        argv = ["create_pr.py", "--issue", "7", "--title", "t", "--body", "b",
                "--agent", "  agent-1  ", "--model-family", "anthropic"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr", return_value=True) as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        # A padded id must not become a second, distinct author identity.
        self.assertEqual(opened.call_args.args[3], "agent-1")

    def test_agent_is_passed_through_to_the_pr(self):
        argv = ["create_pr.py", "--issue", "7", "--title", "t", "--body", "b",
                "--agent", "agent-1", "--model-family", "anthropic"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr", return_value=True) as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(opened.call_args.args[3], "agent-1")
        self.assertEqual(opened.call_args.args[4], "anthropic")

    def test_unknown_model_family_is_refused(self):
        argv = ["create_pr.py", "--issue", "7", "--agent", "a",
                "--model-family", "anthropc"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr") as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertNotEqual(caught.exception.code, 0)
        opened.assert_not_called()

    def test_missing_family_warns_but_proceeds(self):
        # Family only steers reviewer diversity; its absence must not block.
        argv = ["create_pr.py", "--issue", "7", "--agent", "agent-1"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr", return_value=True) as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        opened.assert_called_once()


class IdentityStampTests(unittest.TestCase):
    @patch.object(create_pr, "run_cmd", return_value=(0, "", ""))
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_successful_stamp_reports_success(self, _label, _run):
        self.assertTrue(create_pr.apply_identity("7", "agent-1", "anthropic"))

    @patch.object(create_pr, "run_cmd", return_value=(1, "", "label does not exist"))
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_failed_label_write_is_an_error_not_a_warning(self, _label, _run):
        """An unstamped PR is a hole in the gate, not a cosmetic problem.

        This was best-effort and returned success, so a caller moved on
        believing the identity had landed while merge_pr.py would accept any
        review on the PR, self-review included.
        """
        self.assertFalse(create_pr.apply_identity("7", "agent-1", "anthropic"))

    @patch.object(create_pr, "run_cmd", return_value=(0, "", ""))
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_nothing_to_stamp_is_not_a_failure(self, _label, run):
        self.assertTrue(create_pr.apply_identity("7", "", ""))
        run.assert_not_called()

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_create_pr_propagates_a_failed_stamp(self, _branch, _issue):
        with patch.object(create_pr, "run_cmd", return_value=(0, "https://x/pull/7", "")), \
                patch.object(create_pr, "apply_identity", return_value=False), \
                patch.object(create_pr, "enqueue_review") as queued:
            self.assertFalse(create_pr.create_pr(7, "t", "b", "agent-1", "anthropic"))
            queued.assert_not_called()

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_successful_open_enqueues_review(self, _branch, _issue):
        with patch.object(create_pr, "run_cmd", return_value=(0, "https://x/pull/7", "")), \
                patch.object(create_pr, "apply_identity", return_value=True), \
                patch.object(create_pr, "enqueue_review", return_value=True) as queued:
            self.assertTrue(create_pr.create_pr(7, "t", "b", "agent-1", "anthropic"))
            queued.assert_called_once_with("https://x/pull/7")

    @patch.object(create_pr, "run_cmd", return_value=(0, "", ""))
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_enqueue_review_comments_queued_at_and_needs_review(self, _label, run):
        self.assertTrue(create_pr.enqueue_review("https://x/pull/7"))
        edited = run.call_args_list[0].args[0]
        self.assertEqual(edited[:4], ["gh", "pr", "edit", "https://x/pull/7"])
        self.assertIn("needs-review", edited)
        commented = run.call_args_list[1].args[0]
        self.assertEqual(commented[:3], ["gh", "pr", "comment"])
        body = commented[commented.index("--body") + 1]
        self.assertIn("review-queued-at:", body)
        self.assertIn("No automated account should post a review", body)


class VerificationEvidenceTests(unittest.TestCase):
    def test_collects_passing_and_failing_commands_with_duration(self):
        passing = create_pr.collect_verification_evidence([
            f'{sys.executable} -c "raise SystemExit(0)"',
        ])
        self.assertEqual(passing["status"], "passed")
        self.assertEqual(passing["commands"][0]["exit_code"], 0)
        self.assertGreaterEqual(passing["commands"][0]["duration_seconds"], 0)

        failing = create_pr.collect_verification_evidence([
            f'{sys.executable} -c "raise SystemExit(3)"',
        ])
        self.assertEqual(failing["status"], "failed")
        self.assertEqual(failing["commands"][0]["exit_code"], 3)
        self.assertEqual(failing["commands"][0]["status"], "failed")

    def test_no_commands_is_explicitly_not_run(self):
        evidence = create_pr.collect_verification_evidence([])
        self.assertEqual(evidence, {
            "commands": [],
            "schema": "aru.verification.v1",
            "status": "not_run",
        })

    def test_command_evidence_redacts_secrets_and_absolute_paths(self):
        sanitized = common.sanitize_command([
            "/Users/example/project/.venv/bin/python",
            "--token",
            "super-secret-value",
            "--config=/private/tmp/project/settings.json",
        ])
        rendered = " ".join(sanitized)
        self.assertNotIn("/Users/example", rendered)
        self.assertNotIn("/private/tmp", rendered)
        self.assertNotIn("super-secret-value", rendered)
        self.assertIn("<redacted>", rendered)
        self.assertIn("<local-path>", rendered)

    def test_rendered_json_is_parseable_without_prose_scraping(self):
        evidence = {
            "commands": [{
                "command": ["python3", "-m", "unittest"],
                "duration_seconds": 1.25,
                "exit_code": 0,
                "status": "passed",
            }],
            "schema": "aru.verification.v1",
            "status": "passed",
        }
        body = "Summary" + create_pr.render_verification_evidence(evidence) + "\n\nCloses #7"
        parsed, error = merge_pr.parse_verification_evidence(body)
        self.assertIsNone(error)
        self.assertEqual(parsed, evidence)
        self.assertTrue(merge_pr.check_verification({"body": body})[0])

    def test_gate_warns_on_missing_or_not_run_and_blocks_failed_or_malformed(self):
        missing_ok, missing_message = merge_pr.check_verification({"body": "legacy"})
        self.assertTrue(missing_ok)
        self.assertIn("warning", missing_message.lower())

        not_run = create_pr.render_verification_evidence({
            "commands": [],
            "schema": "aru.verification.v1",
            "status": "not_run",
        })
        self.assertTrue(merge_pr.check_verification({"body": not_run})[0])

        failed = create_pr.render_verification_evidence({
            "commands": [{
                "command": ["python3", "-m", "unittest"],
                "duration_seconds": 0.1,
                "exit_code": 1,
                "status": "failed",
            }],
            "schema": "aru.verification.v1",
            "status": "failed",
        })
        self.assertFalse(merge_pr.check_verification({"body": failed})[0])
        malformed = (
            f"{common.VERIFICATION_EVIDENCE_START}\n```json\n{{bad\n```\n"
            f"{common.VERIFICATION_EVIDENCE_END}"
        )
        self.assertFalse(merge_pr.check_verification({"body": malformed})[0])

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_create_pr_renders_not_run_evidence_into_body(self, _branch, _issue):
        with patch.object(
            create_pr,
            "run_cmd",
            return_value=(0, "https://x/pull/7", ""),
        ) as run:
            self.assertTrue(create_pr.create_pr(7, "t", "body"))
        command = run.call_args.args[0]
        body = command[command.index("--body") + 1]
        evidence, error = merge_pr.parse_verification_evidence(body)
        self.assertIsNone(error)
        self.assertEqual(evidence["status"], "not_run")
        self.assertIn("Closes #7", body)

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_create_pr_rejects_reserved_evidence_markers(self, _branch, _issue):
        with patch.object(create_pr, "run_cmd") as run:
            opened = create_pr.create_pr(
                7,
                "t",
                f"forged {common.VERIFICATION_EVIDENCE_START}",
            )
        self.assertFalse(opened)
        run.assert_not_called()

    def test_merge_parser_rejects_multiple_evidence_blocks(self):
        evidence = create_pr.render_verification_evidence({
            "commands": [],
            "schema": "aru.verification.v1",
            "status": "not_run",
        })
        parsed, error = merge_pr.parse_verification_evidence(evidence + evidence)
        self.assertIsNone(parsed)
        self.assertIn("multiple", error)


if __name__ == "__main__":
    unittest.main()
