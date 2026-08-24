# line-ceiling: 800
import json
import os
import sys
import tempfile
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
                "--agent", "  agent-1  ", "--model-family", "anthropic",
                "--verify-command", "python3 -m unittest"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr", return_value=True) as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        # A padded id must not become a second, distinct author identity.
        self.assertEqual(opened.call_args.args[3], "agent-1")

    def test_agent_is_passed_through_to_the_pr(self):
        argv = ["create_pr.py", "--issue", "7", "--title", "t", "--body", "b",
                "--agent", "agent-1", "--model-family", "anthropic",
                "--verify-command", "python3 -m unittest"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr", return_value=True) as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(opened.call_args.args[3], "agent-1")
        self.assertEqual(opened.call_args.args[4], "anthropic")

    def test_unknown_model_family_is_refused(self):
        argv = ["create_pr.py", "--issue", "7", "--agent", "a",
                "--model-family", "anthropc", "--verify-command", "true"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr") as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertNotEqual(caught.exception.code, 0)
        opened.assert_not_called()

    def test_missing_family_warns_but_proceeds(self):
        # Family only steers reviewer diversity; its absence must not block.
        argv = ["create_pr.py", "--issue", "7", "--agent", "agent-1",
                "--verify-command", "true"]
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
        with patch.object(create_pr, "run_cmd", return_value=(0, "https://x/pull/7", "")) as run, \
                patch.object(create_pr, "apply_identity", return_value=True), \
                patch.object(create_pr, "finalize_review_assignment", return_value=True) as queued:
            self.assertTrue(create_pr.create_pr(7, "t", "b", "agent-1", "anthropic"))
            queued.assert_called_once_with("https://x/pull/7", 7)
        create_cmd = run.call_args_list[0].args[0]
        self.assertEqual(create_cmd[:3], ["gh", "pr", "create"])
        self.assertIn("--draft", create_cmd)

    def test_review_service_assignment_is_stable_and_evenly_distributed(self):
        self.assertEqual(create_pr.review_service_for_issue(1), "coderabbit")
        self.assertEqual(create_pr.review_service_for_issue(2), "sourcery")
        self.assertEqual(create_pr.review_service_for_issue(3), "codeant")
        self.assertEqual(create_pr.review_service_for_issue(4), "coderabbit")

    # finalize_review_assignment() consults existing_review_assignment() (a
    # live "gh pr view --json labels" call) before anything else, and posts
    # capacity evidence as a "gh pr comment" call before the label edit. Both
    # are patched out in the mechanics tests below so call-count assertions
    # cover only the label/ready/trigger sequence they exercise; the
    # capacity-selection and immutability behavior they patch away has its
    # own dedicated tests further down.
    def canned_evidence(self, service):
        return {
            "schema": "aru.review-capacity-selection.v1",
            "as_of": "2026-08-24T12:00:00Z",
            "candidates": list(create_pr.REVIEW_SERVICES),
            "eligible": list(create_pr.REVIEW_SERVICES),
            "excluded": [],
            "selected": service,
            "rationale": f"selected '{service}' for test",
        }

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_codeant_assignment_labels_readys_and_triggers_review_after_ready(self, _existing, _label, run):
        with patch.object(create_pr, "select_review_service", return_value=self.canned_evidence("codeant")):
            run.side_effect = [
                (0, "", ""),  # capacity evidence comment
                (0, "", ""),  # add-label
                (0, "", ""),  # ready
                (0, "", ""),  # @codeant-ai: review trigger
            ]
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        evidence_cmd = run.call_args_list[0].args[0]
        self.assertEqual(evidence_cmd[:3], ["gh", "pr", "comment"])
        label_cmd = run.call_args_list[1].args[0]
        self.assertEqual(label_cmd[:4], ["gh", "pr", "edit", "https://x/pull/9"])
        self.assertIn("review:codeant", label_cmd)
        ready_cmd = run.call_args_list[2].args[0]
        self.assertEqual(ready_cmd[:4], ["gh", "pr", "ready", "https://x/pull/9"])
        comment_cmd = run.call_args_list[3].args[0]
        self.assertEqual(comment_cmd[:3], ["gh", "pr", "comment"])
        self.assertIn("@codeant-ai: review", comment_cmd)

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_review_assignment_stops_safely_when_label_or_ready_fails(self, _existing, _label, run):
        cases = (
            ([(0, "", ""), (1, "", "label failed")], 2),
            ([(0, "", ""), (0, "", ""), (1, "", "ready failed")], 3),
        )
        for side_effect, expected_calls in cases:
            with self.subTest(expected_calls=expected_calls):
                run.reset_mock(side_effect=True)
                run.side_effect = side_effect
                with patch.object(create_pr, "select_review_service",
                                   return_value=self.canned_evidence("codeant")):
                    self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
                self.assertEqual(run.call_count, expected_calls)

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_codeant_trigger_failure_restores_draft_state(self, _existing, _label, run):
        run.side_effect = [
            (0, "", ""), (0, "", ""), (0, "", ""),
            (1, "", "trigger failed"),
            (0, "", ""),
        ]

        with patch.object(create_pr, "select_review_service", return_value=self.canned_evidence("codeant")):
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))

        self.assertEqual(
            run.call_args_list[4].args[0],
            ["gh", "pr", "ready", "https://x/pull/9", "--undo"],
        )

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_codeant_trigger_failure_reports_failed_draft_rollback(self, _existing, _label, run):
        run.side_effect = [
            (0, "", ""), (0, "", ""), (0, "", ""),
            (1, "", "trigger failed"),
            (1, "", "rollback failed"),
        ]

        with patch.object(create_pr, "select_review_service", return_value=self.canned_evidence("codeant")):
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 5)

    def test_codeant_finalization_can_retry_after_trigger_rollback(self):
        """A prior codeant-trigger failure leaves the review:codeant label on the
        PR (only 'ready' was rolled back), so the retry must resume from that
        stable label -- not reselect or re-post capacity evidence."""
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "select_review_service",
                              return_value=self.canned_evidence("codeant")) as select:
            run.side_effect = [
                (0, "", ""), (0, "", ""), (0, "", ""), (1, "", "trigger failed"), (0, "", ""),
            ]
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            select.assert_called_once()

        with patch.object(create_pr, "run_cmd") as retry_run, \
                patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "existing_review_assignment", return_value="codeant"), \
                patch.object(create_pr, "select_review_service") as select:
            retry_run.side_effect = [(0, "", ""), (0, "", "")]
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            select.assert_not_called()
        retry_calls = [call.args[0] for call in retry_run.call_args_list]
        self.assertEqual(retry_calls[0][:3], ["gh", "pr", "ready"])
        self.assertEqual(retry_calls[1][:3], ["gh", "pr", "comment"])
        self.assertIn("@codeant-ai: review", retry_calls[1])

    @patch.object(create_pr, "run_cmd", return_value=(0, "", ""))
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_sourcery_assignment_marks_ready_without_codeant_trigger(self, _existing, _label, run):
        with patch.object(create_pr, "select_review_service", return_value=self.canned_evidence("sourcery")):
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/8", 8))
        self.assertEqual(len(run.call_args_list), 3)
        self.assertIn("review:sourcery", run.call_args_list[1].args[0])

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "existing_review_assignment", return_value="sourcery")
    def test_stable_existing_assignment_is_never_recomputed(self, _existing, run):
        """Authority is immutable once assigned: even if capacity now excludes the
        already-assigned service, or issue-id rotation would pick differently, a
        PR carrying review:sourcery must keep it -- no relabel, no reselection."""
        run.side_effect = [(0, "", "")]
        with patch.object(create_pr, "select_review_service") as select:
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            select.assert_not_called()
        # Only "gh pr ready" runs; no evidence comment, no add-label call.
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["gh", "pr", "ready"])

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "existing_review_assignment", return_value=None)
    def test_no_eligible_service_leaves_pr_draft_with_waiting_evidence(self, _existing, run):
        """If nothing is eligible, the PR must stay draft: no label, no 'gh pr
        ready', only the waiting-for-capacity evidence comment -- and the call
        still reports success since this is the correct governed outcome."""
        run.side_effect = [(0, "", "")]
        waiting_evidence = self.canned_evidence(None)
        waiting_evidence["eligible"] = []
        waiting_evidence["excluded"] = [
            {"service": s, "state": "outage", "reason": "r", "source": "s",
             "observed_at": "2026-08-24T11:00:00Z", "retry_at": "2026-08-24T13:00:00Z"}
            for s in create_pr.REVIEW_SERVICES
        ]
        with patch.object(create_pr, "select_review_service", return_value=waiting_evidence):
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(len(run.call_args_list), 1)
        evidence_cmd = run.call_args_list[0].args[0]
        self.assertEqual(evidence_cmd[:3], ["gh", "pr", "comment"])
        body = evidence_cmd[evidence_cmd.index("--body") + 1]
        self.assertIn('"selected": null', body)
        self.assertIn(create_pr.CAPACITY_EVIDENCE_START, body)

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


def unavailable_ledger(*entries):
    return {"schema": "aru.review-service-unavailability-ledger.v1", "entries": list(entries)}


def unavailable_entry(service, *, state="cooldown", reason="rate limited",
                       source="review-status-webhook",
                       observed_at="2026-08-24T11:30:00Z", retry_at="2026-08-24T13:00:00Z"):
    return {
        "service": service, "state": state, "reason": reason,
        "observed_at": observed_at, "retry_at": retry_at, "source": source,
    }


class CapacitySelectionTests(unittest.TestCase):
    """select_review_service() is the capacity-aware kernel: it loads fresh
    known-unavailable evidence, excludes only what that evidence proves is
    unavailable, and routes deterministically over whatever remains."""

    AS_OF = "2026-08-24T12:00:00Z"

    def test_empty_ledger_matches_legacy_rotation_exactly(self):
        # Backward compatibility: with no evidence, capacity-aware selection
        # must reproduce review_service_for_issue()'s original rotation.
        for issue_id in range(1, 8):
            with self.subTest(issue_id=issue_id):
                evidence = create_pr.select_review_service(
                    issue_id, as_of=self.AS_OF, snapshot=unavailable_ledger(),
                )
                self.assertEqual(evidence["selected"], create_pr.review_service_for_issue(issue_id))
                self.assertEqual(evidence["eligible"], list(create_pr.REVIEW_SERVICES))
                self.assertEqual(evidence["excluded"], [])

    def test_excluded_service_is_never_selected_and_pool_rotates_evenly(self):
        snapshot = unavailable_ledger(unavailable_entry("sourcery", state="quota_exhausted"))
        picks = [
            create_pr.select_review_service(issue_id, as_of=self.AS_OF, snapshot=snapshot)["selected"]
            for issue_id in range(1, 11)
        ]
        self.assertNotIn("sourcery", picks)
        self.assertEqual(set(picks), {"coderabbit", "codeant"})
        # Deterministic: the same issue id always resolves to the same service.
        self.assertEqual(
            picks,
            [create_pr.select_review_service(i, as_of=self.AS_OF, snapshot=snapshot)["selected"]
             for i in range(1, 11)],
        )
        # Approximately even across the eligible pair over 10 consecutive issues.
        self.assertEqual(picks.count("coderabbit"), 5)
        self.assertEqual(picks.count("codeant"), 5)

    def test_excluded_service_evidence_and_rationale_are_recorded(self):
        snapshot = unavailable_ledger(unavailable_entry("sourcery", state="outage", reason="5xx storm"))
        evidence = create_pr.select_review_service(2, as_of=self.AS_OF, snapshot=snapshot)
        self.assertEqual(evidence["candidates"], list(create_pr.REVIEW_SERVICES))
        self.assertEqual(evidence["eligible"], ["coderabbit", "codeant"])
        self.assertEqual(len(evidence["excluded"]), 1)
        self.assertEqual(evidence["excluded"][0]["service"], "sourcery")
        self.assertEqual(evidence["excluded"][0]["state"], "outage")
        self.assertEqual(evidence["excluded"][0]["reason"], "5xx storm")
        self.assertIn(evidence["selected"], evidence["eligible"])
        self.assertTrue(evidence["rationale"])

    def test_all_services_unavailable_selects_nothing(self):
        snapshot = unavailable_ledger(*[
            unavailable_entry(service, state="cooldown") for service in create_pr.REVIEW_SERVICES
        ])
        evidence = create_pr.select_review_service(5, as_of=self.AS_OF, snapshot=snapshot)
        self.assertIsNone(evidence["selected"])
        self.assertEqual(evidence["eligible"], [])
        self.assertEqual({item["service"] for item in evidence["excluded"]}, set(create_pr.REVIEW_SERVICES))

    def test_stale_and_malformed_evidence_does_not_exclude_indefinitely(self):
        stale = unavailable_entry(
            "coderabbit", observed_at="2026-08-24T09:00:00Z", retry_at="2026-08-24T23:00:00Z",
        )
        spoofed = {"service": "codeant", "state": "made-up-state", "reason": "x",
                   "observed_at": self.AS_OF, "retry_at": self.AS_OF, "source": "x"}
        evidence = create_pr.select_review_service(
            1, as_of=self.AS_OF, snapshot=unavailable_ledger(stale, spoofed),
        )
        self.assertEqual(evidence["selected"], create_pr.review_service_for_issue(1))
        self.assertEqual(evidence["eligible"], list(create_pr.REVIEW_SERVICES))

    def test_reads_the_real_shared_ledger_when_no_snapshot_is_given(self):
        """End-to-end through create_pr.py's lazy import of the audit module,
        proving the two files are actually wired together via the shared
        cross-repository ledger, not just unit-tested in isolation."""
        import audit_review_service_capacity as audit

        with tempfile.TemporaryDirectory() as directory:
            shared_path = Path(directory) / "shared-ledger.json"
            with patch.dict(os.environ, {audit.CAPACITY_LEDGER_ENV: str(shared_path)}):
                audit.record_unavailability(
                    "coderabbit", "outage", "provider 5xx", "2026-08-25T00:00:00Z",
                    source="status-page-poll",
                )
                evidence = create_pr.select_review_service(1)
        self.assertNotEqual(evidence["selected"], "coderabbit")
        self.assertIn("coderabbit", {item["service"] for item in evidence["excluded"]})


class FinalizeReviewCliTests(unittest.TestCase):
    def test_finalize_review_flag_retries_assignment_on_an_existing_pr(self):
        argv = ["create_pr.py", "--issue", "9", "--finalize-review", "42",
                "--agent", "agent-1", "--model-family", "anthropic"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "finalize_review_assignment", return_value=True) as finalize:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        finalize.assert_called_once_with("42", 9)

    def test_finalize_review_flag_propagates_failure_exit_code(self):
        argv = ["create_pr.py", "--issue", "9", "--finalize-review", "42",
                "--agent", "agent-1", "--model-family", "anthropic"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "finalize_review_assignment", return_value=False):
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertNotEqual(caught.exception.code, 0)


class VerificationEvidenceTests(unittest.TestCase):
    def test_collects_passing_and_failing_commands_with_duration(self):
        passing = create_pr.collect_verification_evidence([
            f'{sys.executable} -c "raise SystemExit(0)"',
        ])
        self.assertEqual(passing["status"], "passed")
        self.assertEqual(passing["commands"][0]["exit_code"], 0)
        self.assertGreaterEqual(passing["commands"][0]["duration_seconds"], 0)
        self.assertTrue(passing["head_sha"])

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
            "head_sha": evidence["head_sha"],
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

    def test_command_evidence_redacts_url_credentials_and_sensitive_queries(self):
        sanitized = common.sanitize_command([
            "curl",
            "https://oauth2:ghp_TOKEN@example.com/check?token=query-secret&ok=yes",
            "--endpoint=https://user:password@example.net/path?api_key=hidden",
            "file:///Users/example/private/config.json",
        ])
        rendered = " ".join(sanitized)
        for secret in ("ghp_TOKEN", "query-secret", "password", "hidden", "/Users/example"):
            self.assertNotIn(secret, rendered)
        self.assertIn("https://<redacted>@example.com", rendered)
        self.assertIn("token=<redacted>", rendered)
        self.assertIn("file://<local-path>/config.json", rendered)

    def test_command_evidence_redacts_http_header_values(self):
        for option in ("-H", "--header"):
            sanitized = common.sanitize_command([
                "curl", option, "Authorization: Bearer TEST_AUTH_TOKEN",
            ])
            rendered = " ".join(sanitized)
            self.assertNotIn("TEST_AUTH_TOKEN", rendered)
            self.assertIn("Authorization: <redacted>", rendered)

        attached = common.sanitize_command([
            "curl",
            "-HCookie: session=COOKIE_SECRET",
            "--header=X-Api-Key: HEADER_SECRET",
        ])
        self.assertNotIn("COOKIE_SECRET", " ".join(attached))
        self.assertNotIn("HEADER_SECRET", " ".join(attached))

    def test_command_evidence_redacts_opaque_secrets_and_embedded_paths(self):
        probes = [
            ["curl", "https://example.com?X-Amz-Signature=AWSSECRET"],
            ["curl", "https://example.com/blob?sig=AZURESECRET"],
            ["sh", "-c", "printf ghp_NESTED /Users/example/private.txt"],
            ["python", "-c", "open('/Users/example/private.txt')"],
            ["python", "-cprint('ghp_ATTACHED')"],
            ["node", "-econsole.log('NODESECRET')"],
            ["tool", "@/Users/example/response.txt"],
            ["tool", "prefix=/Users/example/project/config.json"],
            ["tool", 'quoted="/Users/example/quoted.json"'],
            ["tool", "paths=[/Users/example/list.json,/Users/example/next.json]"],
            ["tool", "payload={/Users/example/object.json}"],
            ["curl", "-H", "@/Users/example/header.txt"],
            ["curl", "--header=@/Users/example/header-equals.txt"],
            ["curl", "-H@/Users/example/header-attached.txt"],
        ]
        rendered = " ".join(
            part for probe in probes for part in common.sanitize_command(probe)
        )
        for secret in ("AWSSECRET", "AZURESECRET", "ghp_NESTED", "ghp_ATTACHED", "NODESECRET", "/Users/example"):
            self.assertNotIn(secret, rendered)
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
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "passed",
        }
        body = "Summary" + create_pr.render_verification_evidence(evidence) + "\n\nCloses #7"
        parsed, error = merge_pr.parse_verification_evidence(body)
        self.assertIsNone(error)
        self.assertEqual(parsed, evidence)
        self.assertTrue(merge_pr.check_verification({
            "body": body, "headRefOid": "head-7",
        })[0])

    def test_evidence_command_cannot_create_raw_delimiters(self):
        evidence = {
            "commands": [{
                "command": [common.VERIFICATION_EVIDENCE_START, common.VERIFICATION_EVIDENCE_END],
                "duration_seconds": 0.1,
                "exit_code": 0,
                "status": "passed",
            }],
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "passed",
        }
        body = create_pr.render_verification_evidence(evidence)
        self.assertEqual(body.count(common.VERIFICATION_EVIDENCE_START), 1)
        self.assertEqual(body.count(common.VERIFICATION_EVIDENCE_END), 1)
        parsed, error = merge_pr.parse_verification_evidence(body)
        self.assertIsNone(error)
        self.assertEqual(parsed, evidence)

    def test_gate_warns_on_missing_or_not_run_and_blocks_failed_or_malformed(self):
        missing_ok, missing_message = merge_pr.check_verification({"body": "legacy"})
        self.assertTrue(missing_ok)
        self.assertIn("warning", missing_message.lower())

        not_run = create_pr.render_verification_evidence({
            "commands": [],
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "not_run",
        })
        self.assertTrue(merge_pr.check_verification({
            "body": not_run, "headRefOid": "head-7",
        })[0])

        failed = create_pr.render_verification_evidence({
            "commands": [{
                "command": ["python3", "-m", "unittest"],
                "duration_seconds": 0.1,
                "exit_code": 1,
                "status": "failed",
            }],
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "failed",
        })
        self.assertFalse(merge_pr.check_verification({
            "body": failed, "headRefOid": "head-7",
        })[0])
        malformed = (
            f"{common.VERIFICATION_EVIDENCE_START}\n```json\n{{bad\n```\n"
            f"{common.VERIFICATION_EVIDENCE_END}"
        )
        self.assertFalse(merge_pr.check_verification({"body": malformed})[0])

    def test_gate_rejects_ambiguous_or_non_strict_evidence(self):
        def body_for(payload):
            return (
                f"{common.VERIFICATION_EVIDENCE_START}\n```json\n{payload}\n```\n"
                f"{common.VERIFICATION_EVIDENCE_END}"
            )

        invalid_payloads = [
            '{"schema":"aru.verification.v1","status":"passed","status":"failed",'
            '"head_sha":"head-7","commands":[]}',
            '{"schema":"aru.verification.v1","status":"passed","head_sha":"head-7",'
            '"commands":[{"command":["true"],"exit_code":0,"duration_seconds":NaN,'
            '"status":"passed"}]}',
            '{"schema":"aru.verification.v1","status":"passed","head_sha":"head-7",'
            '"commands":[{"command":["true"],"exit_code":false,"duration_seconds":0.1,'
            '"status":"passed"}]}',
            '{"schema":"aru.verification.v1","status":"passed","head_sha":"head-7",'
            '"commands":[{"command":["true"],"exit_code":0,"duration_seconds":true,'
            '"status":"passed"}]}',
            '{"schema":"aru.verification.v1","status":"passed","head_sha":"head-7",'
            '"commands":[{"command":[{}],"exit_code":0,"duration_seconds":0.1,'
            '"status":"passed"}]}',
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertFalse(merge_pr.check_verification({
                    "body": body_for(payload), "headRefOid": "head-7",
                })[0])

        lone_end = f"legacy prose\n{common.VERIFICATION_EVIDENCE_END}"
        self.assertFalse(merge_pr.check_verification({"body": lone_end})[0])

    def test_gate_rejects_evidence_for_a_different_pr_head(self):
        body = create_pr.render_verification_evidence({
            "commands": [{
                "command": ["python3", "-m", "unittest"],
                "duration_seconds": 0.1,
                "exit_code": 0,
                "status": "passed",
            }],
            "head_sha": "old-head",
            "schema": "aru.verification.v1",
            "status": "passed",
        })
        ok, message = merge_pr.check_verification({
            "body": body, "headRefOid": "new-head",
        })
        self.assertFalse(ok)
        self.assertIn("refresh", message)

    def test_refresh_rejects_inverted_evidence_markers_without_truncating_body(self):
        body = (
            f"summary\n{common.VERIFICATION_EVIDENCE_END}\n"
            f"stale\n{common.VERIFICATION_EVIDENCE_START}\nCloses #7"
        )
        evidence = {
            "commands": [],
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "not_run",
        }
        self.assertIsNone(create_pr.replace_verification_evidence(body, evidence))
        self.assertIn("Closes #7", body)

    def test_cli_refuses_to_open_a_pr_without_verification_commands(self):
        argv = ["create_pr.py", "--issue", "7", "--agent", "agent-1"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "create_pr") as opened:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertNotEqual(caught.exception.code, 0)
        opened.assert_not_called()

    @patch.object(create_pr, "get_current_commit", return_value="head-7")
    def test_refresh_replaces_evidence_only_for_the_live_head(self, _head):
        original = "summary" + create_pr.render_verification_evidence({
            "commands": [],
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "not_run",
        }) + "\n\nCloses #7"
        concurrent_body = original.replace("summary", "summary\nconcurrent edit")
        responses = [
            (0, '{"body": ' + json.dumps(original) + ', "headRefOid": "head-7"}', ""),
            (0, '{"body": ' + json.dumps(concurrent_body) + ', "headRefOid": "head-7"}', ""),
            (0, "", ""),
        ]
        with patch.object(create_pr, "run_cmd", side_effect=responses) as run, \
                patch.object(create_pr, "collect_verification_evidence", return_value={
                    "commands": [{"command": ["true"], "duration_seconds": 0.0,
                                  "exit_code": 0, "status": "passed"}],
                    "head_sha": "head-7",
                    "schema": "aru.verification.v1",
                    "status": "passed",
                }):
            self.assertTrue(create_pr.refresh_pr_evidence("7", ["true"]))
        edit = run.call_args_list[-1].args[0]
        refreshed, error = merge_pr.parse_verification_evidence(edit[-1])
        self.assertIsNone(error)
        self.assertEqual(refreshed["status"], "passed")
        self.assertIn("concurrent edit", edit[-1])

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_create_pr_renders_not_run_evidence_into_body(self, _branch, _issue):
        with patch.object(
            create_pr,
            "run_cmd",
            return_value=(0, "https://x/pull/7", ""),
        ) as run:
            self.assertTrue(create_pr.create_pr(7, "t", "body"))
        command = run.call_args_list[0].args[0]
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
            "head_sha": "head-7",
            "schema": "aru.verification.v1",
            "status": "not_run",
        })
        parsed, error = merge_pr.parse_verification_evidence(evidence + evidence)
        self.assertIsNone(parsed)
        self.assertIn("duplicated", error)


if __name__ == "__main__":
    unittest.main()
