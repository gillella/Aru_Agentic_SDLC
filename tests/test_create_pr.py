# +16 for the #344 terminal merge lease tests.
# +85 for #472 deterministic external review-pool assignment coverage.
# +238 for #472 serialized review-reservation, draft-rollback ownership, and
# exactly-once CodeAnt trigger coverage.
# line-ceiling: 1040
import json
import sys
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import create_pr  # noqa: E402
import common  # noqa: E402
import merge_pr  # noqa: E402


def setUpModule():
    """Reservations settle across reads separated by a real sleep; tests do not."""
    settle = patch.object(create_pr.time, "sleep")
    settle.start()
    unittest.addModuleCleanup(settle.stop)


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
    def setUp(self):
        lock = patch.object(create_pr, "review_assignment_lock",
                            return_value=nullcontext(True))
        lock.start()
        self.addCleanup(lock.stop)

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
                patch.object(create_pr, "finalize_review_assignment") as finalize:
            self.assertFalse(create_pr.create_pr(7, "t", "b", "agent-1", "anthropic"))
            finalize.assert_not_called()

    @patch.object(create_pr, "get_issue", return_value={"title": "t"})
    @patch.object(create_pr, "get_current_branch", return_value="fix/issue-7-x")
    def test_successful_open_enqueues_review(self, _branch, _issue):
        with patch.object(create_pr, "terminal_merge_lease", return_value=None), \
                patch.object(create_pr, "run_cmd", return_value=(0, "https://x/pull/7", "")) as run, \
                patch.object(create_pr, "apply_identity", return_value=True), \
                patch.object(create_pr, "finalize_review_assignment", return_value=True) as queued:
            self.assertTrue(create_pr.create_pr(7, "t", "b", "agent-1", "anthropic"))
            queued.assert_called_once_with("https://x/pull/7", 7)
        create_cmd = run.call_args_list[0].args[0]
        self.assertEqual(create_cmd[:3], ["gh", "pr", "create"])
        self.assertIn("--draft", create_cmd)

    @patch.object(create_pr, "_confirm_reservation", return_value="held")
    @patch.object(create_pr, "select_review_service", return_value="sourcery")
    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "sourcery"])
    def test_new_assignment_adds_selected_service_and_marks_ready(
        self, _existing, label, run, _select, _confirm,
    ):
        run.side_effect = [(0, "", ""), (0, "", "")]
        self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        label.assert_called_once_with(
            "review:sourcery", "0e8a16", "Authoritative review service: sourcery",
        )
        label_cmd = run.call_args_list[0].args[0]
        self.assertEqual(label_cmd[:4], ["gh", "pr", "edit", "https://x/pull/9"])
        self.assertEqual(label_cmd[-2:], ["--add-label", "review:sourcery"])
        ready_cmd = run.call_args_list[1].args[0]
        self.assertEqual(ready_cmd[:4], ["gh", "pr", "ready", "https://x/pull/9"])

    @patch.object(create_pr, "_confirm_reservation", return_value="held")
    @patch.object(create_pr, "select_review_service", return_value="codeant")
    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "codeant"])
    def test_codeant_assignment_triggers_only_after_ready(
        self, _existing, _label, run, _select, _confirm,
    ):
        run.side_effect = [(0, "", ""), (0, "", ""), (0, '{"comments": []}', ""),
                           (0, "", "")]
        self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[1][:3], ["gh", "pr", "ready"])
        self.assertEqual(commands[2][:3], ["gh", "pr", "view"])
        self.assertEqual(commands[3][:3], ["gh", "pr", "comment"])
        self.assertEqual(commands[3][-1], "@codeant-ai: review")

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "select_review_service", return_value="coderabbit")
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None])
    def test_failed_label_write_stops_before_ready(self, _existing, _select, _label, run):
        run.return_value = (1, "", "label failed")
        self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 1)
        self.assertIn("--add-label", run.call_args.args[0])

    @patch.object(create_pr, "_confirm_reservation", return_value="held")
    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label", return_value=True)
    @patch.object(create_pr, "select_review_service", return_value="coderabbit")
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=[None, None, "coderabbit"])
    def test_failed_ready_state_is_not_reported_as_finalized(
        self, _existing, _select, _label, run, _confirm,
    ):
        run.side_effect = [
            (0, "", ""),
            (1, "", "ready failed"),
            (0, '{"isDraft": true}', ""),
        ]
        self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_args_list[-1].args[0][-1], "isDraft")

    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label")
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=["sourcery", "sourcery", "sourcery"])
    def test_retry_preserves_an_already_ready_assignment(self, _existing, label, run):
        run.side_effect = [
            (1, "", "already ready"),
            (0, '{"isDraft": false}', ""),
        ]
        self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        label.assert_not_called()
        self.assertEqual(run.call_args_list[0].args[0][:3], ["gh", "pr", "ready"])
        self.assertEqual(run.call_args_list[1].args[0][-1], "isDraft")

    @patch("builtins.print")
    @patch.object(create_pr, "run_cmd")
    @patch.object(create_pr, "ensure_label")
    @patch.object(create_pr, "existing_review_assignment",
                  side_effect=["codeant", "codeant", None])
    def test_already_ready_retry_reports_lost_assignment(self, _existing, label, run, output):
        run.side_effect = [(1, "", "already ready"), (0, '{"isDraft": false}', "")]
        self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        label.assert_not_called()
        self.assertTrue(any("no longer carries review:codeant" in str(call)
                            for call in output.call_args_list))

class FailClosedAssignmentTests(unittest.TestCase):
    """Every ambiguous or unreadable authority state must stop finalization.

    The dangerous shape is not "the lookup failed" -- it is a failed lookup
    that is indistinguishable from "this PR has no reviewer yet", because the
    next step is to pick one and label it. That silently switches or
    duplicates an authority the routing contract promises is immutable.
    """

    def setUp(self):
        lock = patch.object(create_pr, "review_assignment_lock",
                            return_value=nullcontext(True))
        lock.start()
        self.addCleanup(lock.stop)

    def test_failed_label_lookup_is_not_read_as_unassigned(self):
        with patch.object(create_pr, "run_cmd", return_value=(1, "", "gh: not authenticated")):
            with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                create_pr.existing_review_assignment("https://x/pull/9")

    def test_malformed_label_json_is_not_read_as_unassigned(self):
        # Valid exit code, unusable payload: HTML error pages, truncated
        # output, and a labels field of the wrong type all land here.
        for payload in (
            "not json", "", "[]", '{"labels": null}',
            '{"labels": "review:coderabbit"}', '{"labels": [null]}', '{"labels": [{}]}',
        ):
            with self.subTest(payload=payload):
                with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
                    with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                        create_pr.existing_review_assignment("https://x/pull/9")

    def test_unknown_agent_and_case_variant_review_labels_are_rejected(self):
        for review_label in (
            "review:agent", "review:unknown",
            "Review:coderabbit", "review:coderabbit ",
        ):
            with self.subTest(review_label=review_label):
                payload = json.dumps({"labels": [{"name": review_label}]})
                with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
                    with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                        create_pr.existing_review_assignment("https://x/pull/9")

    def test_duplicate_or_mixed_review_labels_are_rejected(self):
        for review_labels in (
            ["review:coderabbit", "review:coderabbit"],
            ["review:coderabbit", "review:sourcery"],
            ["review:unknown", "review:codeant"],
        ):
            with self.subTest(review_labels=review_labels):
                payload = json.dumps({"labels": [{"name": name} for name in review_labels]})
                with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
                    with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                        create_pr.existing_review_assignment("https://x/pull/9")

    def test_each_exact_external_label_resolves(self):
        for service in create_pr.REVIEW_SERVICES:
            with self.subTest(service=service):
                payload = json.dumps({"labels": [
                    {"name": "needs-review"}, {"name": f"review:{service}"},
                ]})
                with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
                    self.assertEqual(
                        create_pr.existing_review_assignment("https://x/pull/9"), service)

    def test_no_review_label_is_the_only_unassigned_answer(self):
        payload = json.dumps({"labels": [{"name": "needs-review"}]})
        with patch.object(create_pr, "run_cmd", return_value=(0, payload, "")):
            self.assertIsNone(create_pr.existing_review_assignment("https://x/pull/9"))

    @patch.object(create_pr, "ensure_label")
    def test_unreadable_authority_blocks_assignment_entirely(self, label):
        error = create_pr.ReviewAssignmentLookupError("gh failed")
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment", side_effect=error):
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        run.assert_not_called()
        label.assert_not_called()

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_concurrent_identical_assignment_is_idempotent(self, _label):
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, "sourcery", "sourcery"]), \
                patch.object(create_pr, "select_review_service", return_value="sourcery"):
            run.return_value = (0, "", "")
            self.assertTrue(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:3], ["gh", "pr", "ready"])

    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_concurrent_legacy_assignment_blocks_before_write(self, _label):
        conflict = create_pr.ReviewAssignmentLookupError("unsupported review label")
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, conflict]), \
                patch.object(create_pr, "select_review_service", return_value="sourcery"):
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        run.assert_not_called()

    @patch.object(create_pr, "_confirm_reservation", return_value="held")
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_concurrent_assignment_landing_during_the_write_is_not_marked_ready(
        self, _label, _confirm,
    ):
        """The tightest race: the other finalizer's label lands between the
        pre-write check and this add-label, so both labels now exist. The
        post-write read reports the conflict and this PR is not marked ready."""
        conflict = create_pr.ReviewAssignmentLookupError("conflicting review labels")
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, conflict]), \
                patch.object(create_pr, "select_review_service", return_value="sourcery"):
            run.return_value = (0, "", "")
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertIn("--add-label", commands[0])
        self.assertNotIn(["gh", "pr", "ready", "https://x/pull/9"], commands)

    @patch.object(create_pr, "_confirm_reservation", return_value="held")
    @patch.object(create_pr, "ensure_label", return_value=True)
    def test_assignment_that_does_not_stick_is_not_marked_ready(self, _label, _confirm):
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[None, None, None]), \
                patch.object(create_pr, "select_review_service", return_value="codeant"):
            run.return_value = (0, "", "")
            self.assertFalse(create_pr.finalize_review_assignment("https://x/pull/9", 9))
        self.assertEqual(run.call_count, 1)


class CodeAntTriggerOwnershipTests(unittest.TestCase):
    """Finalization owns only its transitions and trusted exact trigger."""

    NO_TRIGGER_YET = (0, '{"comments": []}', "")

    def _finalize(self, ready=(0, "", ""), history=NO_TRIGGER_YET,
                  comment=(0, "", ""), existing="codeant", trusted_trigger=True):
        # A failing `gh pr ready` means the PR was already out of draft, and
        # finalization confirms that with an extra isDraft read before it reads
        # the comment history for an existing trigger.
        results = [ready]
        if ready[0] != 0:
            results.append((0, '{"isDraft": false}', ""))
        results += [history, comment, (0, "", "")]
        with patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "review_trigger_authorized", return_value=trusted_trigger), \
                patch.object(create_pr, "existing_review_assignment",
                              side_effect=[existing, existing, existing]):
            run.side_effect = results
            ok = create_pr.finalize_review_assignment("https://x/pull/9", 9)
        return ok, [call.args[0] for call in run.call_args_list]

    @staticmethod
    def comments(*bodies, login="gillella"):
        comments = [{"body": body, "author": {"login": login}} for body in bodies]
        return (0, json.dumps({"comments": comments}), "")

    def test_trigger_failure_on_an_already_ready_pr_never_redrafts_it(self):
        """The reported defect: a transient comment failure on a PR this retry
        found already ready must not push it back out of merge admission."""
        ok, commands = self._finalize(ready=(1, "", "not a draft"),
                                      comment=(1, "", "rate limited"))
        self.assertFalse(ok)
        self.assertEqual([c for c in commands if "--undo" in c], [])

    def test_trigger_failure_after_this_run_undrafted_it_rolls_back(self):
        ok, commands = self._finalize(comment=(1, "", "rate limited"))
        self.assertFalse(ok)
        self.assertEqual(commands[-1], ["gh", "pr", "ready", "https://x/pull/9", "--undo"])

    def test_successful_trigger_never_touches_draft_state(self):
        ok, commands = self._finalize()
        self.assertTrue(ok)
        self.assertEqual([c for c in commands if "--undo" in c], [])

    def test_an_existing_trigger_is_not_posted_a_second_time(self):
        ok, commands = self._finalize(history=self.comments("@codeant-ai: review\n"))
        self.assertTrue(ok)
        self.assertEqual([c for c in commands if c[:3] == ["gh", "pr", "comment"]], [])

    def test_an_untrusted_exact_trigger_cannot_suppress_the_real_trigger(self):
        ok, commands = self._finalize(history=self.comments(
            "@codeant-ai: review", login="drive-by"), trusted_trigger=False)
        self.assertTrue(ok)
        self.assertEqual(commands[-1][-1], create_pr.CODEANT_TRIGGER)

    def test_a_quoted_trigger_in_a_provider_comment_is_not_a_trigger(self):
        ok, commands = self._finalize(history=self.comments(
            "Retrigger review by typing:\n@codeant-ai: review\nin a comment."))
        self.assertTrue(ok)
        self.assertEqual(commands[-1][-1], create_pr.CODEANT_TRIGGER)

    def test_an_unreadable_comment_history_never_guesses(self):
        for history in ((1, "", "gh: rate limited"), (0, "not json", ""),
                        (0, '{"comments": null}', ""), self.comments(None)):
            with self.subTest(history=history):
                ok, commands = self._finalize(history=history)
                self.assertFalse(ok)
                self.assertEqual(
                    [c for c in commands if c[:3] == ["gh", "pr", "comment"]], [])


class ReviewReservationTests(unittest.TestCase):
    """Selection reads a snapshot, so the label is only a tentative claim.

    Without a compare-and-set two finalizers pick the same least-loaded service
    and both apply its label, oversubscribing the pool while each pull request
    still looks individually valid.
    """

    @staticmethod
    def capacity(counts, holders):
        return {"counts": counts, "holders": holders}

    @contextmanager
    def reserving(self, confirm, selected="codeant", results=(0, "", "")):
        """The reservation loop with its collaborators stubbed, yielding run_cmd.

        A list argument is a per-call sequence, an exception is raised, and
        anything else is a fixed value.
        """
        def stub(name, value):
            key = "side_effect" if isinstance(value, (list, BaseException)) else "return_value"
            return patch.object(create_pr, name, **{key: value})

        with patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "existing_review_assignment", return_value=None), \
                patch.object(create_pr, "review_assignment_lock",
                             return_value=nullcontext(True)), \
                stub("select_review_service", selected), \
                stub("_confirm_reservation", confirm), \
                stub("run_cmd", results) as run:
            yield run

    @staticmethod
    def label_moves(run):
        return [call.args[0][-2:] for call in run.call_args_list
                if {"--add-label", "--remove-label"} & set(call.args[0])]

    def test_pr_number_is_read_from_a_url_or_a_bare_number(self):
        with patch.object(create_pr, "run_cmd") as run:
            self.assertEqual(create_pr.pr_number("https://x/y/pull/476"), 476)
            self.assertEqual(create_pr.pr_number("476"), 476)
        run.assert_not_called()

    def test_pr_number_falls_back_to_a_lookup_for_a_branch_reference(self):
        with patch.object(create_pr, "run_cmd", return_value=(0, '{"number": 9}', "")):
            self.assertEqual(create_pr.pr_number("feat/issue-1-x"), 9)
        for result in ((1, "", "no pr"), (0, "not json", ""), (0, '{"number": 0}', "")):
            with self.subTest(result=result), \
                    patch.object(create_pr, "run_cmd", return_value=result):
                self.assertIsNone(create_pr.pr_number("feat/issue-1-x"))

    def test_atomic_lock_contention_refuses_before_applying_a_label(self):
        """A bounded sequence of inventory reads cannot exclude a contender
        that lands just after the last read; the server-side ref claim must be
        acquired before any authority label is written."""
        with patch.object(create_pr, "pr_number", return_value=9), \
                patch.object(create_pr, "ensure_label", return_value=True), \
                patch.object(create_pr, "existing_review_assignment",
                             return_value=None), \
                patch.object(create_pr, "select_review_service",
                             return_value="coderabbit"), \
                patch.object(create_pr, "run_cmd",
                             return_value=(1, "", "reference already exists")) as run:
            self.assertIsNone(create_pr.reserve_review_service("9", 472))
        commands = [item.args[0] for item in run.call_args_list]
        self.assertEqual(commands[0][:4], ["gh", "api", "--method", "POST"])
        self.assertEqual([cmd for cmd in commands if cmd[:3] == ["gh", "pr", "edit"]], [])

    def test_reservation_holds_when_it_is_still_the_deterministic_choice(self):
        snapshot = self.capacity({"coderabbit": 1, "sourcery": 1, "codeant": 1},
                                 {"coderabbit": [1], "sourcery": [2], "codeant": [9]})
        with patch.object(create_pr, "review_capacity", return_value=snapshot):
            self.assertEqual(create_pr._confirm_reservation(9, 3, "codeant"), "held")

    def test_reservation_not_yet_visible_in_the_inventory_is_unconfirmed(self):
        snapshot = self.capacity({"coderabbit": 0, "sourcery": 0, "codeant": 0},
                                 {"coderabbit": [], "sourcery": [], "codeant": []})
        with patch.object(create_pr, "review_capacity", return_value=snapshot):
            self.assertEqual(create_pr._confirm_reservation(9, 1, "coderabbit"),
                             "unconfirmed")

    def test_the_higher_numbered_rival_yields_and_the_lower_one_holds(self):
        # Both finalizers saw an empty pool and both reserved coderabbit.
        snapshot = self.capacity({"coderabbit": 2, "sourcery": 0, "codeant": 0},
                                 {"coderabbit": [8, 9], "sourcery": [], "codeant": []})
        with patch.object(create_pr, "review_capacity", return_value=snapshot):
            self.assertEqual(create_pr._confirm_reservation(9, 1, "coderabbit"), "yield")
            self.assertEqual(create_pr._confirm_reservation(8, 1, "coderabbit"), "held")

    RESELECTED = [["--add-label", "review:coderabbit"],
                  ["--remove-label", "review:coderabbit"],
                  ["--add-label", "review:sourcery"]]

    def test_a_yielding_reservation_is_released_and_reselected(self):
        """One sole-holder read is not settled either: a lower-numbered
        contender can land immediately after it and make this one the loser,
        so a verdict only commits once consecutive reads agree."""
        for verdicts in (["yield", "held", "held"], ["held", "yield", "held", "held"]):
            with self.subTest(verdicts=verdicts), \
                    self.reserving(verdicts, selected=["coderabbit", "sourcery"]) as run:
                self.assertEqual(
                    create_pr.reserve_review_service("https://x/pull/9", 9), "sourcery")
                self.assertEqual(self.label_moves(run), self.RESELECTED)

    def test_a_reservation_that_never_confirms_fails_closed(self):
        with self.reserving("unconfirmed") as run:
            self.assertIsNone(create_pr.reserve_review_service("https://x/pull/9", 9))
        self.assertEqual([move for move in self.label_moves(run)
                          if "--remove-label" in move], [])

    def test_endless_contention_fails_closed_instead_of_spinning(self):
        with self.reserving("yield") as run:
            self.assertIsNone(create_pr.reserve_review_service("https://x/pull/9", 9))
        adds = [move for move in self.label_moves(run) if "--add-label" in move]
        self.assertEqual(len(adds), create_pr.REVIEW_RESERVATION_ATTEMPTS)

    def test_a_failed_release_stops_rather_than_leaving_a_silent_extra_label(self):
        with self.reserving("yield", results=[(0, "", ""), (1, "", "denied")]):
            self.assertIsNone(create_pr.reserve_review_service("https://x/pull/9", 9))

    def test_an_unreadable_pr_number_reserves_nothing(self):
        with patch.object(create_pr, "pr_number", return_value=None), \
                patch.object(create_pr, "run_cmd") as run, \
                patch.object(create_pr, "ensure_label") as label:
            self.assertIsNone(create_pr.reserve_review_service("feat/x", 9))
        run.assert_not_called()
        label.assert_not_called()

    def test_an_unreadable_inventory_during_confirmation_fails_closed(self):
        error = create_pr.ReviewAssignmentLookupError("inventory unreadable")
        with self.reserving(error):
            self.assertIsNone(create_pr.reserve_review_service("https://x/pull/9", 9))

    def test_capacity_excludes_only_the_named_pull_request(self):
        inventory = [
            {"number": 1, "labels": [{"name": "review:coderabbit"}]},
            {"number": 9, "labels": [{"name": "review:coderabbit"}]},
        ]
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=inventory):
            full = create_pr.review_capacity()
            without_self = create_pr.review_capacity(exclude_pr=9)
        self.assertEqual(full["counts"]["coderabbit"], 2)
        self.assertEqual(full["holders"]["coderabbit"], [1, 9])
        self.assertEqual(without_self["counts"]["coderabbit"], 1)
        self.assertEqual(without_self["holders"]["coderabbit"], [1])

    def test_excluding_a_pr_never_hides_ambiguous_authority(self):
        inventory = [{"number": 9, "labels": [{"name": "review:coderabbit"},
                                              {"name": "review:sourcery"}]}]
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=inventory):
            with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                create_pr.review_capacity(exclude_pr=9)


class BalancedReviewSelectionTests(unittest.TestCase):
    @staticmethod
    def open_pr(number, *labels):
        return {"number": number, "labels": [{"name": label} for label in labels]}

    def test_empty_pool_uses_stable_even_issue_rotation(self):
        expected = ["coderabbit", "sourcery", "codeant"] * 2
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=[]):
            actual = [create_pr.select_review_service(issue) for issue in range(1, 7)]
        self.assertEqual(actual, expected)

    def test_least_loaded_service_is_selected(self):
        inventory = [
            self.open_pr(1, "review:coderabbit"),
            self.open_pr(2, "review:coderabbit"),
            self.open_pr(3, "review:codeant"),
        ]
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=inventory):
            self.assertEqual(create_pr.select_review_service(472), "sourcery")

    def test_tied_minimum_load_uses_issue_number_deterministically(self):
        inventory = [self.open_pr(1, "review:coderabbit")]
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=inventory):
            first = create_pr.select_review_service(1)
            second = create_pr.select_review_service(2)
            repeat = create_pr.select_review_service(2)
        self.assertEqual((first, second, repeat), ("sourcery", "codeant", "codeant"))

    def test_terminal_agent_fallback_does_not_consume_external_capacity(self):
        inventory = [
            self.open_pr(1, "review:agent", "reviewer:agent-2"),
            self.open_pr(2, "review:coderabbit"),
        ]
        with patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(create_pr, "open_pull_requests", return_value=inventory):
            self.assertEqual(create_pr.select_review_service(472), "codeant")

    def test_unreadable_or_ambiguous_inventory_fails_closed(self):
        bad_inventories = [
            None,
            [self.open_pr(1, "review:coderabbit", "review:sourcery")],
            [self.open_pr(1, "review:unknown")],
        ]
        for inventory in bad_inventories:
            with self.subTest(inventory=inventory), \
                    patch.object(create_pr, "get_repo_slug", return_value="owner/repo"), \
                    patch.object(create_pr, "open_pull_requests", return_value=inventory):
                with self.assertRaises(create_pr.ReviewAssignmentLookupError):
                    create_pr.select_review_service(472)


class FinalizeReviewCliTests(unittest.TestCase):
    def test_codex_is_a_supported_author_model_family(self):
        self.assertIn("codex", create_pr.MODEL_FAMILIES)

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
        """Retry retains a real positive linked-issue contract."""
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
        with patch.object(create_pr, "terminal_merge_lease", return_value=None), \
             patch.object(
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


class TerminalLeasePrCreationTests(unittest.TestCase):
    """#344 criterion 2: merged work cannot be re-proposed under a spent branch."""

    LEASE = {"branch": "fix/issue-87-x", "pr": 89, "gated_sha": "a" * 40,
             "merged_sha": "b" * 40, "holder": "codex-1"}

    def test_pr_creation_from_a_leased_branch_is_refused(self):
        with patch.object(create_pr, "get_current_branch", return_value="fix/issue-87-x"), \
             patch.object(create_pr, "terminal_merge_lease", return_value=self.LEASE), \
             patch.object(create_pr, "run_cmd") as run:
            self.assertFalse(create_pr.create_pr(87, "t", "b", "agent-1", "anthropic"))
        run.assert_not_called()
