"""Tests for the governed review-reassignment helper added in #435."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr  # noqa: E402
import reassign_review as rr  # noqa: E402

HEAD = "0d2a6d0848b5d4e2e6ed03fff73885fbc81f832d"


def pr(*labels, state="OPEN", head=HEAD):
    return {"state": state, "headRefOid": head,
            "labels": [{"name": n} for n in labels]}


class CurrentAuthorityTests(unittest.TestCase):
    def test_single_review_label_is_returned(self):
        label, problem = rr.current_authority([{"name": "review:coderabbit"},
                                               {"name": "author:x"}])
        self.assertEqual(label, "review:coderabbit")
        self.assertIsNone(problem)

    def test_no_review_label_fails_closed(self):
        label, problem = rr.current_authority([{"name": "author:x"}])
        self.assertIsNone(label)
        self.assertIn("no review authority label", problem)

    def test_several_review_labels_fail_closed(self):
        label, problem = rr.current_authority(
            [{"name": "review:coderabbit"}, {"name": "review:sourcery"}])
        self.assertIsNone(label)
        self.assertIn("2 review labels", problem)

    def test_malformed_label_array_fails_closed(self):
        for labels in (None, "labels", [None], [{"name": 7}], [{}]):
            with self.subTest(labels=labels):
                label, problem = rr.current_authority(labels)
                self.assertIsNone(label)
                self.assertTrue(problem)


class ReassignTests(unittest.TestCase):
    REASON = "CodeRabbit reported Review rate limited at abc1234"

    def _run(self, snapshot, service="sourcery",
             edit_results=((0, "", ""), (0, "", "")),
             comment_results=((0, "", ""), (0, "", ""))):
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            group = "comment" if "comment" in cmd else "edit"
            results = comment_results if group == "comment" else edit_results
            index = len([c for c in calls if (group in c)]) - 1
            return results[min(index, len(results) - 1)]

        with patch.object(rr, "run_gh_json", return_value=snapshot), \
             patch.object(rr, "ensure_label", return_value=True), \
             patch.object(rr, "run_cmd", side_effect=fake_run_cmd):
            code = rr.reassign(433, service, self.REASON)
        return code, calls

    @staticmethod
    def _comments(calls):
        return [c[-1] for c in calls if "comment" in c]

    def test_clean_swap_adds_before_removing(self):
        code, calls = self._run(pr("review:coderabbit", "author:x"))
        self.assertEqual(code, rr.EXIT_OK)
        edits = [c for c in calls if "edit" in c]
        self.assertIn("--add-label", edits[0])
        self.assertIn("review:sourcery", edits[0])
        self.assertIn("--remove-label", edits[1])
        self.assertIn("review:coderabbit", edits[1])

    def test_reason_and_head_are_recorded_on_the_pull_request(self):
        _, calls = self._run(pr("review:coderabbit"))
        audit = self._comments(calls)[0]
        self.assertIn(self.REASON, audit)
        self.assertIn(HEAD, audit)
        self.assertIn("review:sourcery", audit)

    def test_each_service_is_triggered_with_its_own_command(self):
        for service in sorted(rr.FALLBACK_LABELS):
            with self.subTest(service=service):
                code, calls = self._run(pr("review:coderabbit"), service=service)
                self.assertEqual(code, rr.EXIT_OK)
                self.assertEqual(self._comments(calls)[1],
                                 rr.SERVICE_TRIGGERS[service])

    def test_codeant_is_a_supported_target(self):
        code, calls = self._run(pr("review:coderabbit"), service="codeant")
        self.assertEqual(code, rr.EXIT_OK)
        self.assertIn("review:codeant", [c[-1] for c in calls if "--add-label" in c])

    def test_already_switched_pr_is_refused(self):
        for service, label in sorted(rr.FALLBACK_LABELS.items()):
            with self.subTest(service=service):
                code, calls = self._run(pr(label), service=service)
                self.assertEqual(code, rr.EXIT_CONFLICT)
                self.assertEqual([c for c in calls if "edit" in c], [])

    def test_switching_between_fallbacks_is_refused(self):
        """Only the default assignment may move; a second hop is not authorized."""
        code, _ = self._run(pr("review:codeant"), service="sourcery")
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_unknown_existing_authority_is_refused(self):
        code, _ = self._run(pr("review:manual"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_missing_authority_label_is_refused(self):
        code, _ = self._run(pr("author:x"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_duplicate_authority_labels_are_refused(self):
        code, _ = self._run(pr("review:coderabbit", "review:sourcery"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_closed_pr_is_refused(self):
        code, _ = self._run(pr("review:coderabbit", state="MERGED"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_unreadable_pr_fails_closed(self):
        code, _ = self._run(None)
        self.assertEqual(code, rr.EXIT_ERROR)

    def test_unreadable_head_fails_closed(self):
        """The audit record is exact-head bound, like the gate it feeds."""
        for head in (None, "", "abc", 7, "z" * 40):
            with self.subTest(head=head):
                code, calls = self._run(pr("review:coderabbit", head=head))
                self.assertEqual(code, rr.EXIT_ERROR)
                self.assertEqual([c for c in calls if "edit" in c], [])

    def test_failed_add_leaves_the_original_assignment_intact(self):
        """A partial swap must never strip the only reviewer."""
        code, calls = self._run(pr("review:coderabbit"),
                                edit_results=((1, "", "denied"), (0, "", "")))
        self.assertEqual(code, rr.EXIT_ERROR)
        self.assertEqual([c for c in calls if "--remove-label" in c], [])

    def test_failed_remove_reports_the_two_label_state(self):
        code, _ = self._run(pr("review:coderabbit"),
                            edit_results=((0, "", ""), (1, "", "denied")))
        self.assertEqual(code, rr.EXIT_ERROR)

    def test_unrecorded_reason_fails_closed(self):
        """A reassignment nobody can audit is a reassignment that did not happen."""
        code, calls = self._run(pr("review:coderabbit"),
                                comment_results=((1, "", "denied"), (0, "", "")))
        self.assertEqual(code, rr.EXIT_ERROR)
        self.assertEqual(len(self._comments(calls)), 1)

    def test_failed_trigger_fails_closed(self):
        code, _ = self._run(pr("review:coderabbit"),
                            comment_results=((0, "", ""), (1, "", "denied")))
        self.assertEqual(code, rr.EXIT_ERROR)


class ArgumentTests(unittest.TestCase):
    def test_unknown_service_is_refused(self):
        for service in ("qodo", "claude", "", None):
            with self.subTest(service=service):
                self.assertEqual(rr.reassign(1, service, "why"), rr.EXIT_ERROR)

    def test_empty_reason_is_refused(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                self.assertEqual(rr.reassign(1, "sourcery", reason), rr.EXIT_ERROR)

    def test_coderabbit_is_not_a_fallback_target(self):
        """The default is what we fall back *from*; it is never a target."""
        self.assertNotIn("coderabbit", rr.FALLBACK_LABELS)

    def test_every_target_is_an_authority_the_merge_gate_recognises(self):
        """A label this helper can apply but the gate cannot read strands the PR."""
        for label in rr.FALLBACK_LABELS.values():
            with self.subTest(label=label):
                self.assertIn(label, merge_pr.REVIEW_SERVICE_LABELS)
        self.assertEqual(set(rr.SERVICE_TRIGGERS), set(rr.FALLBACK_LABELS))


if __name__ == "__main__":
    unittest.main()
