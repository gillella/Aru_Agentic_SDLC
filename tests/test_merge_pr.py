import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr


class IssueLinkTests(unittest.TestCase):
    def test_extracts_closes_footer(self):
        self.assertEqual(merge_pr.linked_issue("Some body\n\nCloses #42\n"), 42)
        self.assertEqual(merge_pr.linked_issue("closes #7"), 7)

    def test_missing_footer_is_none(self):
        self.assertIsNone(merge_pr.linked_issue("Implements the thing. See #42."))

    def test_all_closed_issues_are_collected(self):
        # A PR closing three issues must have all three sets of acceptance
        # criteria checked, not just the first.
        body = "Work.\n\nCloses #4\nCloses #5\n\nCloses #3\n"
        self.assertEqual(merge_pr.linked_issues(body), [4, 5, 3])

    def test_duplicate_references_collapse(self):
        self.assertEqual(merge_pr.linked_issues("Closes #7\ncloses #7"), [7])

    def test_no_footer_yields_empty_list(self):
        self.assertEqual(merge_pr.linked_issues("See #42 for context."), [])


class AcceptanceCriteriaTests(unittest.TestCase):
    BODY = """## Summary

Do a thing.

## Acceptance Criteria

- [x] first done
- [ ] second not done
- [ ] third not done

## Verification

`pytest -q`

## Notes

- [ ] a stray checkbox that is not acceptance criteria
"""

    def test_counts_only_unticked_criteria_in_the_right_section(self):
        pending = merge_pr.unticked_criteria(self.BODY)
        self.assertEqual(len(pending), 2)
        self.assertTrue(all("stray" not in p for p in pending))

    def test_all_ticked_returns_empty(self):
        body = "## Acceptance Criteria\n\n- [x] one\n- [X] two\n\n## Verification\n\nx\n"
        self.assertEqual(merge_pr.unticked_criteria(body), [])

    def test_no_section_means_nothing_to_enforce(self):
        self.assertEqual(merge_pr.unticked_criteria("no headings here"), [])


class CiGateTests(unittest.TestCase):
    def test_all_successful_passes(self):
        pr = {"statusCheckRollup": [
            {"name": "verify", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]}
        ok, _ = merge_pr.check_ci(pr)
        self.assertTrue(ok)

    def test_failure_blocks(self):
        pr = {"statusCheckRollup": [
            {"name": "verify", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("verify", msg)

    def test_pending_blocks(self):
        pr = {"statusCheckRollup": [{"name": "verify", "status": "IN_PROGRESS"}]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("not finished", msg)

    def test_unknown_conclusions_fail_closed(self):
        # STARTUP_FAILURE and STALE are neither in the old failure list nor the
        # pending list, so a denylist reported them as green and merged an
        # unverified head. Anything not explicitly successful now blocks.
        for conclusion in ("STARTUP_FAILURE", "STALE", "SOMETHING_NEW"):
            pr = {"statusCheckRollup": [
                {"name": "verify", "status": "COMPLETED", "conclusion": conclusion}]}
            ok, msg = merge_pr.check_ci(pr)
            self.assertFalse(ok, f"{conclusion} should block")
            self.assertIn("verify", msg)

    def test_neutral_and_skipped_count_as_passing(self):
        for conclusion in ("NEUTRAL", "SKIPPED"):
            pr = {"statusCheckRollup": [
                {"name": "verify", "status": "COMPLETED", "conclusion": conclusion}]}
            self.assertTrue(merge_pr.check_ci(pr)[0], f"{conclusion} should pass")

    def test_no_checks_at_all_blocks(self):
        # A PR with zero checks is unverified, not verified-by-default. This is
        # the exact hole that let a green-looking PR certify nothing.
        ok, msg = merge_pr.check_ci({"statusCheckRollup": []})
        self.assertFalse(ok)
        self.assertIn("No CI checks", msg)


class ReviewGateTests(unittest.TestCase):
    def test_no_reviews_blocks(self):
        ok, msg = merge_pr.check_reviews({"reviews": []}, 0)
        self.assertFalse(ok)
        self.assertIn("No review", msg)

    def test_changes_requested_blocks(self):
        ok, _ = merge_pr.check_reviews({"reviews": [{"state": "CHANGES_REQUESTED"}]}, 0)
        self.assertFalse(ok)

    def test_re_approval_after_changes_requested_unblocks(self):
        # The reviews list is history, so the CHANGES_REQUESTED entry survives
        # re-approval. Reading it raw blocked the PR forever, contradicting the
        # refusal message that promised re-approval was supported.
        pr = {"reviews": [
            {"state": "CHANGES_REQUESTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-01T00:00:00Z"},
            {"state": "APPROVED", "author": {"login": "bob"},
             "submittedAt": "2026-01-02T00:00:00Z"},
        ]}
        ok, _ = merge_pr.check_reviews(pr, 0)
        self.assertTrue(ok)

    def test_another_reviewer_still_blocking_is_respected(self):
        pr = {"reviews": [
            {"state": "APPROVED", "author": {"login": "bob"},
             "submittedAt": "2026-01-02T00:00:00Z"},
            {"state": "CHANGES_REQUESTED", "author": {"login": "eve"},
             "submittedAt": "2026-01-03T00:00:00Z"},
        ]}
        ok, msg = merge_pr.check_reviews(pr, 0)
        self.assertFalse(ok)
        self.assertIn("eve", msg)

    def test_a_later_comment_does_not_clear_a_change_request(self):
        pr = {"reviews": [
            {"state": "CHANGES_REQUESTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-01T00:00:00Z"},
            {"state": "COMMENTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-05T00:00:00Z"},
        ]}
        self.assertFalse(merge_pr.check_reviews(pr, 0)[0])

    def test_unresolved_threads_block(self):
        ok, msg = merge_pr.check_reviews({"reviews": [{"state": "COMMENTED"}]}, 3)
        self.assertFalse(ok)
        self.assertIn("3 unresolved", msg)

    def test_unknown_thread_state_blocks_rather_than_guesses(self):
        ok, msg = merge_pr.check_reviews({"reviews": [{"state": "APPROVED"}]}, None)
        self.assertFalse(ok)
        self.assertIn("refusing", msg)

    def test_approved_and_resolved_passes(self):
        ok, _ = merge_pr.check_reviews({"reviews": [{"state": "APPROVED"}]}, 0)
        self.assertTrue(ok)

    def test_commented_review_with_no_open_threads_passes(self):
        # A bot review that left no unresolved threads still counts as a review.
        ok, _ = merge_pr.check_reviews({"reviews": [{"state": "COMMENTED"}]}, 0)
        self.assertTrue(ok)


def labelled(*names, reviews=None, pr_login="gillella", review_login="gillella"):
    """A PR whose reviews come from the same GitHub account by default.

    Same-account is the interesting case: every agent authenticates as one user,
    so only the identity labels distinguish them.
    """
    default = [{"state": "APPROVED", "author": {"login": review_login}}]
    return {
        "author": {"login": pr_login},
        "reviews": reviews if reviews is not None else default,
        "labels": [{"name": n} for n in names],
    }


class ExternalReviewerTests(unittest.TestCase):
    """A different GitHub account is proof enough on its own."""

    def test_a_bot_review_counts_without_any_label(self):
        # Codex and Bugbot post as their own apps and will never stamp
        # reviewed-by:. Requiring the label would block every bot-reviewed PR.
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", review_login="chatgpt-codex-connector"), 0)
        self.assertTrue(ok)
        self.assertIn("chatgpt-codex-connector", msg)

    def test_a_human_review_counts_without_any_label(self):
        ok, _ = merge_pr.check_reviews(
            labelled("author:agent-1", review_login="some-colleague"), 0)
        self.assertTrue(ok)

    def test_same_account_still_needs_the_labels(self):
        ok, msg = merge_pr.check_reviews(labelled("author:agent-1"), 0)
        self.assertFalse(ok)
        # Names the prefix claim_review actually writes, so the remedy in the
        # message is one an agent can follow.
        self.assertIn("reviewer:", msg)


class SelfReviewTests(unittest.TestCase):
    """Every agent is the same GitHub user, so GitHub cannot catch this."""

    def test_self_review_is_refused(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())

    def test_peer_review_passes(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_a_peer_alongside_a_self_review_passes(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-1", "reviewed-by:agent-3"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-3", msg)

    def test_review_without_any_reviewer_label_is_refused(self):
        # Unattributable on a stamped PR: it cannot be told apart from a
        # self-review, so it must not pass.
        ok, msg = merge_pr.check_reviews(labelled("author:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("reviewer:", msg)


    def test_unstamped_pr_falls_back_to_the_old_behaviour(self):
        # PRs predating author stamping must stay mergeable.
        ok, msg = merge_pr.check_reviews(labelled(), 0)
        self.assertTrue(ok)
        self.assertIn("unstamped", msg)

    def test_same_family_review_warns_but_does_not_refuse(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2", "same-family-review"), 0)
        self.assertTrue(ok)
        self.assertIn("Same-family", msg)

    def test_self_review_refusal_outranks_nothing_else_being_wrong(self):
        # CI green, threads resolved, criteria ticked - still refused.
        ok, _ = merge_pr.check_reviews(
            labelled("author:solo", "reviewed-by:solo",
                     reviews=[{"state": "APPROVED"}, {"state": "COMMENTED"}]), 0)
        self.assertFalse(ok)


class ReviewerLabelPrefixTests(unittest.TestCase):
    """The gate must read the label the framework writes.

    claim_review stamps `reviewer:<id>`; this file used to read only
    `reviewed-by:<id>`, which nothing wrote. A PR claimed and reviewed through
    the normal loop was refused, and merging needed a hand-added duplicate.
    """

    def test_claim_label_identifies_the_reviewer(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_claim_label_matching_the_author_is_a_self_review(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())

    def test_legacy_alias_still_merges(self):
        # PRs hand-stamped before the prefixes agreed must not strand.
        ok, _ = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)

    def test_either_prefix_satisfies_the_gate_together(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-1", "reviewed-by:agent-3"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-3", msg)


class RebaseGateTests(unittest.TestCase):
    def test_behind_blocks(self):
        ok, msg = merge_pr.check_rebased({"mergeStateStatus": "BEHIND"})
        self.assertFalse(ok)
        self.assertIn("Rebase", msg)

    def test_conflicts_block(self):
        self.assertFalse(merge_pr.check_rebased({"mergeStateStatus": "DIRTY"})[0])
        self.assertFalse(merge_pr.check_rebased({"mergeable": "CONFLICTING"})[0])

    def test_clean_passes(self):
        self.assertTrue(merge_pr.check_rebased({"mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE"})[0])


class SizeGateTests(unittest.TestCase):
    def test_oversized_diff_blocks_without_ack(self):
        ok, msg = merge_pr.check_size({"additions": 800, "deletions": 100}, forced=False)
        self.assertFalse(ok)
        self.assertIn("force-human-review", msg)

    def test_oversized_diff_passes_with_ack(self):
        ok, msg = merge_pr.check_size({"additions": 800, "deletions": 100}, forced=True)
        self.assertTrue(ok)
        self.assertIn("waived", msg)

    def test_small_diff_passes(self):
        self.assertTrue(merge_pr.check_size({"additions": 10, "deletions": 2}, forced=False)[0])


class OpenStateTests(unittest.TestCase):
    def test_draft_blocks(self):
        self.assertFalse(merge_pr.check_open({"state": "OPEN", "isDraft": True})[0])

    def test_closed_blocks(self):
        self.assertFalse(merge_pr.check_open({"state": "CLOSED"})[0])

    def test_open_passes(self):
        self.assertTrue(merge_pr.check_open({"state": "OPEN", "isDraft": False})[0])


class IssueLinkGateTests(unittest.TestCase):
    def test_missing_closes_footer_blocks(self):
        ok, msg = merge_pr.check_issue_link({"body": "no link"})
        self.assertFalse(ok)
        self.assertIn("Closes", msg)

    def test_every_linked_issue_is_reported(self):
        ok, msg = merge_pr.check_issue_link({"body": "Closes #3\nCloses #4"})
        self.assertTrue(ok)
        self.assertIn("#3", msg)
        self.assertIn("#4", msg)


if __name__ == "__main__":
    unittest.main()
