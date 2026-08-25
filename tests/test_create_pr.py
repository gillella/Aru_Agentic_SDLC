# line-ceiling: 1180
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import create_pr  # noqa: E402
import common  # noqa: E402
import merge_pr  # noqa: E402


def patch_linked_issues():
    """Stub the live PR-body read that binds an assignment to its issue set.

    _acquire_review_assignment() reads the PR body so the evidence names the
    exact issues merge_pr.py will recompute from. These fixtures name the PR
    by URL and pass the matching issue number, so the stub derives the set
    from the reference; the real read has its own tests in
    LinkedIssueBindingTests.
    """
    return patch.object(
        create_pr, "linked_issues_for_pr",
        side_effect=lambda pr_ref: [int(str(pr_ref).rsplit("/", 1)[-1])],
    )


def capacity_evidence(selected, *, issues=(9,), eligible=None, excluded=(),
                      as_of="2026-08-24T12:00:00Z"):
    """A complete v2 selection record, sealed the way create_pr.py seals one."""
    evidence = {
        "schema": create_pr.CAPACITY_SELECTION_SCHEMA,
        "as_of": as_of,
        "candidates": list(create_pr.REVIEW_SERVICES),
        "eligible": list(create_pr.REVIEW_SERVICES if eligible is None else eligible),
        "excluded": [dict(item) for item in excluded],
        "issues": list(issues),
        "selected": selected,
        "rationale": f"selected {selected!r} for test",
    }
    evidence["snapshot"] = {
        "source": "aru.review-service-unavailability-ledger.v1",
        "observed_at": as_of,
        "max_age_seconds": create_pr.CAPACITY_MAX_AGE_SECONDS,
        "excluded_count": len(evidence["excluded"]),
        "digest": create_pr.capacity_selection_digest(evidence),
    }
    return evidence


def comment_evidence(selection, *, created_at=None, author=None, last_edited=None):
    """Wrap one selection the way merge_pr.py reads it off the live PR.

    Rendered through create_pr.render_capacity_evidence() rather than
    hand-written, so these tests break if the two sides ever stop agreeing on
    the comment format itself.
    """
    if created_at is None:
        moment = datetime.fromisoformat(selection["as_of"].replace("Z", "+00:00"))
        created_at = (moment + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    return {"capacity_selection_comments": [{
        "body": create_pr.render_capacity_evidence(selection),
        "createdAt": created_at,
        "lastEditedAt": last_edited,
        "author": author if author is not None else {"login": "gillella", "__typename": "User"},
    }]}


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
    #
    # The lookup is patched with a *sequence*, not a constant, because
    # assignment now re-reads the live labels immediately before and after the
    # add-label write to detect a concurrent finalizer. The realistic sequence
    # for an unassigned PR that this call assigns is therefore
    # (None, None, <service>): unassigned, still unassigned, then ours.
    def setUp(self):
        patcher = patch_linked_issues()
        patcher.start()
        self.addCleanup(patcher.stop)

    def canned_evidence(self, service):
        return capacity_evidence(service)

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "codeant"])
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
    def test_review_assignment_stops_safely_when_label_or_ready_fails(self, _label, run):
        # A failed add-label never reaches the post-write confirmation, so that
        # case sees only the two pre-write lookups.
        cases = (
            ([(0, "", ""), (1, "", "label failed")], [None, None], 2),
            ([(0, "", ""), (0, "", ""), (1, "", "ready failed")], [None, None, "codeant"], 3),
        )
        for side_effect, seen, expected_calls in cases:
            with self.subTest(expected_calls=expected_calls):
                run.reset_mock(side_effect=True)
                run.side_effect = side_effect
                with patch.object(create_pr, "select_review_service",
                                   return_value=self.canned_evidence("codeant")), \
                        patch.object(create_pr, "existing_review_assignment",
                                      side_effect=seen):
                    self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
                self.assertEqual(run.call_count, expected_calls)

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "codeant"])
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
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "codeant"])
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
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, "codeant"]), \
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
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "sourcery"])
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
                    [issue_id], as_of=self.AS_OF, snapshot=unavailable_ledger(),
                )
                self.assertEqual(evidence["selected"], create_pr.review_service_for_issue(issue_id))
                self.assertEqual(evidence["eligible"], list(create_pr.REVIEW_SERVICES))
                self.assertEqual(evidence["excluded"], [])

    def test_excluded_service_is_never_selected_and_pool_rotates_evenly(self):
        snapshot = unavailable_ledger(unavailable_entry("sourcery", state="quota_exhausted"))
        picks = [
            create_pr.select_review_service([issue_id], as_of=self.AS_OF, snapshot=snapshot)["selected"]
            for issue_id in range(1, 11)
        ]
        self.assertNotIn("sourcery", picks)
        self.assertEqual(set(picks), {"coderabbit", "codeant"})
        # Deterministic: the same issue id always resolves to the same service.
        self.assertEqual(
            picks,
            [create_pr.select_review_service([i], as_of=self.AS_OF, snapshot=snapshot)["selected"]
             for i in range(1, 11)],
        )
        # Approximately even across the eligible pair over 10 consecutive issues.
        self.assertEqual(picks.count("coderabbit"), 5)
        self.assertEqual(picks.count("codeant"), 5)

    def test_excluded_service_evidence_and_rationale_are_recorded(self):
        snapshot = unavailable_ledger(unavailable_entry("sourcery", state="outage", reason="5xx storm"))
        evidence = create_pr.select_review_service([2], as_of=self.AS_OF, snapshot=snapshot)
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
        evidence = create_pr.select_review_service([5], as_of=self.AS_OF, snapshot=snapshot)
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
            [1], as_of=self.AS_OF, snapshot=unavailable_ledger(stale, spoofed),
        )
        self.assertEqual(evidence["selected"], create_pr.review_service_for_issue(1))
        self.assertEqual(evidence["eligible"], list(create_pr.REVIEW_SERVICES))

    def test_reads_the_real_shared_ledger_when_no_snapshot_is_given(self):
        """End-to-end through create_pr.py's lazy import of the audit module,
        proving the two files are actually wired together via the shared
        cross-repository ledger, not just unit-tested in isolation."""
        import audit_review_service_capacity as audit

        # Relative to now, not a fixed date: this exclusion has to still be
        # unexpired when the test runs, and a hardcoded retry_at silently
        # stops excluding anything the moment the wall clock passes it -- at
        # which point the assertions below fail for a reason that has nothing
        # to do with the wiring they exist to prove.
        retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            shared_path = Path(directory) / "shared-ledger.json"
            with patch.dict(os.environ, {audit.CAPACITY_LEDGER_ENV: str(shared_path)}):
                audit.record_unavailability(
                    "coderabbit", "outage", "provider 5xx", retry_at,
                    source="status-page-poll",
                )
                evidence = create_pr.select_review_service([1])
        self.assertNotEqual(evidence["selected"], "coderabbit")
        self.assertIn("coderabbit", {item["service"] for item in evidence["excluded"]})


class FailClosedAssignmentTests(unittest.TestCase):
    """Every ambiguous or unreadable authority state must stop finalization.

    The dangerous shape is not "the lookup failed" -- it is a failed lookup
    that is indistinguishable from "this PR has no reviewer yet", because the
    next step is to pick one and label it. That silently switches or
    duplicates an authority the routing contract promises is immutable.
    """

    def setUp(self):
        patcher = patch_linked_issues()
        patcher.start()
        self.addCleanup(patcher.stop)

    def canned_evidence(self, service):
        return capacity_evidence(service)

    def test_failed_label_lookup_is_not_read_as_unassigned(self):
        with patch.object(create_pr, "run_cmd", return_value=(1, "", "gh: not authenticated")):
            with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                create_pr.existing_review_assignment("https://x/pull/9")

    def test_malformed_label_json_is_not_read_as_unassigned(self):
        # Valid exit code, unusable payload: HTML error pages, truncated
        # output, and a labels field of the wrong type all land here.
        for payload in ("not json", "", "[]", '{"labels": null}', '{"labels": "review:sourcery"}'):
            with self.subTest(payload=payload):
                with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
                    with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                        create_pr.existing_review_assignment("https://x/pull/9")

    def test_multiple_review_labels_are_rejected_not_silently_resolved(self):
        # Returning the first match in REVIEW_SERVICES order would hand back a
        # single confident answer for a PR whose reviewer is genuinely
        # ambiguous, and leave both labels in place for later consumers.
        payload = json.dumps({"labels": [
            {"name": "review:codeant"}, {"name": "review:sourcery"}, {"name": "needs-review"},
        ]})
        with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
            with self.assertRaises(create_pr.ReviewAssignmentLookupError) as caught:
                create_pr.existing_review_assignment("https://x/pull/9")
        message = str(caught.exception)
        self.assertIn("review:sourcery", message)
        self.assertIn("review:codeant", message)

    def test_exactly_one_review_label_still_resolves(self):
        payload = json.dumps({"labels": [{"name": "needs-review"}, {"name": "review:sourcery"}]})
        with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
            self.assertEqual(create_pr.existing_review_assignment("https://x/pull/9"), "sourcery")

    def test_no_review_label_is_the_only_unassigned_answer(self):
        payload = json.dumps({"labels": [{"name": "needs-review"}, None]})
        with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
            self.assertIsNone(create_pr.existing_review_assignment("https://x/pull/9"))

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_unreadable_authority_blocks_assignment_entirely(self, _label):
        error = create_pr.ReviewAssignmentLookupError("gh failed")
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", side_effect=error), \
                patch.object(create_pr, "select_review_service") as select:
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            select.assert_not_called()
        run.assert_not_called()  # no comment, no label, no ready

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_missing_capacity_evidence_comment_stops_before_labeling(self, _label):
        """The evidence comment is the audit record; assigning without it would
        leave a PR whose reviewer cannot be justified after the fact."""
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "select_review_service",
                              return_value=self.canned_evidence("codeant")):
            run.side_effect = [(1, "", "comment rejected")]
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["gh", "pr", "comment"])

    def test_failed_waiting_evidence_is_not_reported_as_success(self):
        """The bounded waiting-for-capacity state is only real if it was
        recorded; a failed comment leaves a draft PR with no explanation."""
        waiting = self.canned_evidence(None)
        waiting["eligible"] = []
        with patch.object(create_pr, "run_cmd", return_value=(1, "", "comment rejected")) as run, \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "select_review_service", return_value=waiting):
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 1)

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_concurrent_assignment_detected_before_the_write_is_not_overwritten(self, _label):
        """A second finalizer that labelled the PR while this one was selecting
        wins; this one must not add a competing label."""
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, "sourcery"]), \
                patch.object(create_pr, "select_review_service",
                              return_value=self.canned_evidence("codeant")):
            run.side_effect = [(0, "", "")]
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:3], ["gh", "pr", "comment"])
        self.assertNotIn("--add-label", commands[0])

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_concurrent_assignment_landing_during_the_write_is_not_marked_ready(self, _label):
        """The tightest race: the other finalizer's label lands between the
        pre-write check and this add-label, so both labels now exist. The
        post-write read reports the conflict and this PR is not marked ready."""
        conflict = create_pr.ReviewAssignmentLookupError("conflicting review labels")
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, conflict]), \
                patch.object(create_pr, "select_review_service",
                              return_value=self.canned_evidence("codeant")):
            run.side_effect = [(0, "", ""), (0, "", "")]
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertNotIn(["gh", "pr", "ready", "https://x/pull/9"], commands)

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_assignment_that_does_not_stick_is_not_marked_ready(self, _label):
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, "sourcery"]), \
                patch.object(create_pr, "select_review_service",
                              return_value=self.canned_evidence("codeant")):
            run.side_effect = [(0, "", ""), (0, "", "")]
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 2)


class CapacityRoutedAssignmentTests(unittest.TestCase):
    """A capacity-rerouted assignment is now shipped, not withheld.

    Before #403 the merge gate recomputed review_service_for_issue() and
    rejected any label that disagreed, so create_pr.py had to withhold every
    selection a real exclusion moved -- correct routing was unmergeable.
    merge_pr.py now validates the assignment against this evidence, so the
    reroute is assigned and the evidence carries what proves it.
    """

    def setUp(self):
        patcher = patch_linked_issues()
        patcher.start()
        self.addCleanup(patcher.stop)

    AS_OF = "2026-08-24T12:00:00Z"

    def rerouted(self, issue_id):
        """Selection for an issue whose full-pool service is excluded."""
        excluded = create_pr.review_service_for_issue(issue_id)
        snapshot = unavailable_ledger(unavailable_entry(excluded, state="quota_exhausted"))
        return excluded, create_pr.select_review_service(
            [issue_id], as_of=self.AS_OF, snapshot=snapshot,
        )

    def test_divergent_selection_is_assigned_rather_than_withheld(self):
        formula, evidence = self.rerouted(9)
        self.assertEqual(formula, "codeant")
        self.assertNotEqual(evidence["selected"], formula)
        self.assertIn(evidence["selected"], evidence["eligible"])
        self.assertNotIn("withheld_selection", evidence)

    def test_a_rerouted_assignment_is_accepted_by_the_live_merge_gate(self):
        """The end the withholding gate existed to prevent: proves the label
        create_pr.py now applies is one merge_pr.py actually accepts."""
        _formula, evidence = self.rerouted(9)
        service = evidence["selected"]
        pr = {"labels": [{"name": f"review:{service}"}], "body": "Closes #9"}
        # Without the evidence the legacy rotation still rules, and rejects it.
        self.assertIsNone(merge_pr.assigned_review_service(pr))
        self.assertEqual(
            merge_pr.assigned_review_service(pr, comment_evidence(evidence)), service,
        )

    def test_evidence_records_the_issue_set_and_snapshot_provenance(self):
        evidence = create_pr.select_review_service(
            [9], as_of=self.AS_OF, snapshot=unavailable_ledger(),
        )
        self.assertEqual(evidence["schema"], "aru.review-capacity-selection.v2")
        self.assertEqual(evidence["issues"], [9])
        snapshot = evidence["snapshot"]
        self.assertEqual(snapshot["source"], "aru.review-service-unavailability-ledger.v1")
        self.assertEqual(snapshot["observed_at"], evidence["as_of"])
        self.assertEqual(snapshot["max_age_seconds"], create_pr.CAPACITY_MAX_AGE_SECONDS)
        self.assertEqual(snapshot["excluded_count"], 0)
        self.assertEqual(snapshot["digest"], create_pr.capacity_selection_digest(evidence))

    def test_digest_changes_when_any_sealed_field_changes(self):
        evidence = create_pr.select_review_service(
            [9], as_of=self.AS_OF, snapshot=unavailable_ledger(),
        )
        baseline = create_pr.capacity_selection_digest(evidence)
        for field, value in (
            ("selected", "sourcery"), ("issues", [10]), ("eligible", ["codeant"]),
            ("as_of", "2026-08-24T13:00:00Z"), ("candidates", ["codeant"]),
            ("excluded", [unavailable_entry("sourcery")]),
        ):
            with self.subTest(field=field):
                tampered = {**evidence, field: value}
                self.assertNotEqual(create_pr.capacity_selection_digest(tampered), baseline)

    def test_rationale_is_not_sealed_so_prose_alone_cannot_break_a_digest(self):
        evidence = create_pr.select_review_service(
            [9], as_of=self.AS_OF, snapshot=unavailable_ledger(),
        )
        reworded = {**evidence, "rationale": "different prose entirely"}
        self.assertEqual(
            create_pr.capacity_selection_digest(reworded),
            create_pr.capacity_selection_digest(evidence),
        )

    def test_multi_issue_selection_requires_one_service_for_every_issue(self):
        agreeing = create_pr.select_review_service(
            [1, 4], as_of=self.AS_OF, snapshot=unavailable_ledger(),
        )
        self.assertEqual(agreeing["selected"], "coderabbit")
        self.assertEqual(agreeing["issues"], [1, 4])

    def test_multi_issue_pr_spanning_services_selects_nothing(self):
        """#1 rotates to coderabbit and #2 to sourcery over the full pool, so
        no single service is authoritative for both and the PR stays draft."""
        evidence = create_pr.select_review_service(
            [1, 2], as_of=self.AS_OF, snapshot=unavailable_ledger(),
        )
        self.assertIsNone(evidence["selected"])
        self.assertIn("different services", evidence["rationale"])

    def test_exclusion_can_make_a_mixed_issue_set_agree(self):
        """Not a special case bolted on: with sourcery excluded the pool is
        two wide, and #1 and #2 land on different members -- but #1 and #3 now
        agree where the full pool would have split them."""
        snapshot = unavailable_ledger(unavailable_entry("sourcery"))
        evidence = create_pr.select_review_service([1, 3], as_of=self.AS_OF, snapshot=snapshot)
        self.assertEqual(evidence["eligible"], ["coderabbit", "codeant"])
        self.assertEqual(evidence["selected"], "coderabbit")

    def test_empty_issue_set_is_refused_rather_than_defaulted(self):
        with self.assertRaises(ValueError):
            create_pr.select_review_service([])

    def test_selection_is_bound_to_the_live_pr_body_not_the_issue_flag(self):
        """--issue names one issue; the body is what the merge gate reads. The
        evidence must be bound to the body's full set or it proves nothing
        about the PR the gate will validate."""
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "linked_issues_for_pr", return_value=[1, 4]), \
                patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, "coderabbit"]):
            run.side_effect = [(0, "", ""), (0, "", ""), (0, "", "")]
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 4))
        body = run.call_args_list[0].args[0][-1]
        self.assertIn('"issues": [\n    1,\n    4\n  ]', body)

    def test_assignment_refuses_an_issue_the_pr_does_not_close(self):
        """A PR body that does not close --issue would bind evidence to an
        issue set the merge gate never recomputes; nothing is written."""
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "linked_issues_for_pr", return_value=[4]), \
                patch.object(create_pr, "ensure_label") as label:
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            label.assert_not_called()
        run.assert_not_called()

    def test_unreadable_pr_body_stops_before_any_write(self):
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "linked_issues_for_pr", return_value=None), \
                patch.object(create_pr, "ensure_label") as label:
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
            label.assert_not_called()
        run.assert_not_called()


class LinkedIssueBindingTests(unittest.TestCase):
    """linked_issues_for_pr() must read exactly what merge_pr.linked_issues()
    reads, or evidence gets bound to one issue set and validated against
    another."""

    def read(self, body):
        payload = json.dumps({"body": body})
        with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
            return create_pr.linked_issues_for_pr("https://x/pull/9")

    def test_matches_the_merge_gate_parser_on_the_same_bodies(self):
        for body in (
            "Closes #9", "closes #9\nCloses #4", "Closes #9\nCloses #9",
            "See #9 for context.", "", "Closes #12 and closes #7",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.read(body), merge_pr.linked_issues(body))

    def test_order_of_appearance_is_preserved_and_duplicates_collapse(self):
        self.assertEqual(self.read("Closes #7\nCloses #3\nCloses #7"), [7, 3])

    def test_unreadable_or_unparseable_responses_are_none_not_empty(self):
        """None means "could not tell" and stops assignment; [] would mean
        "this PR closes nothing", which is a different, quieter failure."""
        cases = ((1, "", "gh: not authenticated"), (0, "not json", ""),
                 (0, "[]", ""), (0, '{"body": null}', ""))
        for code, out, err in cases:
            with self.subTest(out=out):
                with patch.object(create_pr, "run_cmd", return_value=(code, out, err)):
                    self.assertIsNone(create_pr.linked_issues_for_pr("https://x/pull/9"))


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

    def test_non_positive_issue_is_refused_before_any_assignment(self):
        """`(issue_id - 1) % len(eligible)` is a valid index for 0 and for
        negatives, so an invalid issue number produces a confident assignment
        for an issue that does not exist rather than an error."""
        for issue in ("0", "-3"):
            with self.subTest(issue=issue):
                argv = ["create_pr.py", "--issue", issue, "--finalize-review", "42",
                        "--agent", "agent-1", "--model-family", "anthropic"]
                with patch.object(sys, "argv", argv), \
                        patch.object(create_pr, "finalize_review_assignment") as finalize:
                    with self.assertRaises(SystemExit) as caught:
                        create_pr.main()
                self.assertNotEqual(caught.exception.code, 0)
                finalize.assert_not_called()

    def test_positive_issue_still_reaches_assignment(self):
        argv = ["create_pr.py", "--issue", "1", "--finalize-review", "42",
                "--agent", "agent-1", "--model-family", "anthropic"]
        with patch.object(sys, "argv", argv), \
                patch.object(create_pr, "finalize_review_assignment", return_value=True) as finalize:
            with self.assertRaises(SystemExit) as caught:
                create_pr.main()
        self.assertEqual(caught.exception.code, 0)
        finalize.assert_called_once_with("42", 1)

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
        ) as run, patch.object(create_pr, "finalize_review_assignment", return_value=True):
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
