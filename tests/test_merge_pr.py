import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr


def _gate(pr, threads=0, **overrides):
    """Runs the review gate with evidence defaulting to a clean pull request.

    Most cases below exercise reviewer identity and label logic rather than
    thread evidence, so they should not have to spell out every signal.
    ``threads`` keeps its original positional meaning - the unresolved count,
    or None when the query failed.
    """
    if threads is None:
        return merge_pr.check_reviews(pr, None)
    evidence = {
        "unresolved": threads,
        "unfixed": 0,
        "withdrawn": 0,
        "reviewed_head": True,
    }
    evidence.update(overrides)
    return merge_pr.check_reviews(pr, evidence)

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


class UnresolvedThreadQueryTests(unittest.TestCase):
    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_paginates_and_counts_unresolved_threads_on_later_pages(
        self, gh_json, _slug
    ):
        gh_json.side_effect = [
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [{"isResolved": True, "isOutdated": False}],
                "pageInfo": {"hasNextPage": True, "endCursor": "next-page"},
            }}}}},
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [{"isResolved": False, "isOutdated": False}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}},
        ]

        self.assertEqual(merge_pr.unresolved_threads(57), 1)
        self.assertIn("cursor=next-page", gh_json.call_args_list[1].args[0])

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_missing_pagination_proof_returns_unknown(self, gh_json, _slug):
        gh_json.return_value = {"data": {"repository": {"pullRequest": {
            "reviewThreads": {
                "nodes": [{"isResolved": True, "isOutdated": False}],
            }
        }}}}

        self.assertIsNone(merge_pr.unresolved_threads(57))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_multi_cursor_cycle_returns_unknown_without_another_request(
        self, gh_json, _slug
    ):
        def page(next_cursor):
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": next_cursor},
            }}}}}

        gh_json.side_effect = [page("A"), page("B"), page("A")]

        self.assertIsNone(merge_pr.unresolved_threads(57))
        self.assertEqual(gh_json.call_count, 3)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_partial_data_with_graphql_errors_returns_unknown(self, gh_json, _slug):
        gh_json.return_value = {
            "errors": [{"message": "partial result"}],
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}},
        }

        self.assertIsNone(merge_pr.unresolved_threads(57))


class ReviewEvidencePaginationTests(unittest.TestCase):
    @staticmethod
    def review_page(head="head123", nodes=None, has_next=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviews": {
                "nodes": [] if nodes is None else nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }}}}

    @staticmethod
    def thread_page(head="head123"):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "commits": {"nodes": []},
            "reviewThreads": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}}

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_review_evidence_paginates_reviews_beyond_first_page(
        self, gh_json, _slug
    ):
        stale_reviews = [
            {
                "state": "COMMENTED",
                "author": {"login": f"reviewer-{index}"},
                "commit": {"oid": "old-head"},
            }
            for index in range(100)
        ]
        stale_reviews[0] = {
            "state": "COMMENTED",
            "author": {"login": "coderabbitai[bot]"},
            "commit": {"oid": "head123"},
        }
        stale_reviews[1] = {
            "state": "PENDING",
            "author": {"login": "draft-reviewer"},
            "commit": {"oid": "head123"},
        }
        current_review = {
            "state": "COMMENTED",
            "author": {"login": "independent-agent"},
            "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(
                nodes=stale_reviews, has_next=True, cursor="review-page-2"
            ),
            self.review_page(nodes=[current_review]),
            self.thread_page(),
        ]

        evidence = merge_pr.review_evidence(162)

        self.assertTrue(evidence["reviewed_head"])
        self.assertEqual(evidence["head_oid"], "head123")
        self.assertIn("cursor=review-page-2", gh_json.call_args_list[1].args[0])
        self.assertEqual(gh_json.call_count, 3)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_review_pagination_failures_return_unknown(self, gh_json, _slug):
        malformed_cases = [
            None,
            {"errors": [{"message": "rate limited"}]},
            {"data": {"repository": {"pullRequest": {
                "headRefOid": "head123",
                "reviews": {"nodes": []},
            }}}},
            self.review_page(nodes="not-a-list"),
            self.review_page(nodes=[None]),
            self.review_page(has_next=True, cursor=None),
        ]
        for page in malformed_cases:
            with self.subTest(page=page):
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.return_value = page
                self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_repeated_review_cursor_returns_unknown(self, gh_json, _slug):
        gh_json.side_effect = [
            self.review_page(has_next=True, cursor="same"),
            self.review_page(has_next=True, cursor="same"),
        ]

        self.assertIsNone(merge_pr.review_evidence(162))
        self.assertEqual(gh_json.call_count, 2)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_review_head_change_returns_unknown(self, gh_json, _slug):
        gh_json.side_effect = [
            self.review_page(has_next=True, cursor="next"),
            self.review_page(head="pushed-head"),
        ]

        self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_thread_page_head_change_after_review_pagination_returns_unknown(
        self, gh_json, _slug
    ):
        gh_json.side_effect = [
            self.review_page(),
            self.thread_page(head="pushed-head"),
        ]

        self.assertIsNone(merge_pr.review_evidence(162))


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
        ok, msg = _gate({"reviews": []}, 0)
        self.assertFalse(ok)
        self.assertIn("No review", msg)

    def test_changes_requested_blocks(self):
        ok, _ = _gate({"reviews": [{"state": "CHANGES_REQUESTED"}]}, 0)
        self.assertFalse(ok)

    def test_advisory_bot_changes_requested_does_not_block_after_threads_resolve(self):
        reviews = [{
            "state": "CHANGES_REQUESTED",
            "author": {"login": "chatgpt-codex-connector"},
        }]
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2", reviews=reviews), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_re_approval_after_changes_requested_unblocks(self):
        # The reviews list is history, so the CHANGES_REQUESTED entry survives
        # re-approval. Reading it raw blocked the PR forever, contradicting the
        # refusal message that promised re-approval was supported.
        pr = {"author": {"login": "alice"},
              "labels": [{"name": "author:agent-1"}],
              "reviews": [
            {"state": "CHANGES_REQUESTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-01T00:00:00Z"},
            {"state": "APPROVED", "author": {"login": "bob"},
             "submittedAt": "2026-01-02T00:00:00Z"},
        ]}
        ok, _ = _gate(pr, 0)
        self.assertTrue(ok)

    def test_another_reviewer_still_blocking_is_respected(self):
        pr = {"reviews": [
            {"state": "APPROVED", "author": {"login": "bob"},
             "submittedAt": "2026-01-02T00:00:00Z"},
            {"state": "CHANGES_REQUESTED", "author": {"login": "eve"},
             "submittedAt": "2026-01-03T00:00:00Z"},
        ]}
        ok, msg = _gate(pr, 0)
        self.assertFalse(ok)
        self.assertIn("eve", msg)

    def test_a_later_comment_does_not_clear_a_change_request(self):
        pr = {"reviews": [
            {"state": "CHANGES_REQUESTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-01T00:00:00Z"},
            {"state": "COMMENTED", "author": {"login": "bob"},
             "submittedAt": "2026-01-05T00:00:00Z"},
        ]}
        self.assertFalse(_gate(pr, 0)[0])

    def test_unresolved_threads_block(self):
        ok, msg = _gate({"reviews": [{"state": "COMMENTED"}]}, 3)
        self.assertFalse(ok)
        self.assertIn("3 unresolved", msg)

    def test_unknown_thread_state_blocks_rather_than_guesses(self):
        ok, msg = _gate({"reviews": [{"state": "APPROVED"}]}, None)
        self.assertFalse(ok)
        self.assertIn("refusing", msg)

    def test_approved_and_resolved_passes(self):
        ok, _ = _gate(
            labelled("author:agent-1", review_login="some-colleague"), 0)
        self.assertTrue(ok)

    def test_commented_review_with_no_open_threads_passes(self):
        # Same-account agents cannot APPROVE through GitHub, so their governed
        # reviewed-by attribution remains the proof of completed peer review.
        ok, _ = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2",
                     reviews=[{"state": "COMMENTED"}]), 0)
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
    """Only approving, non-automation external reviewers count on their own."""

    def test_a_bot_review_is_advisory_without_any_label(self):
        reviews = [{"state": "COMMENTED", "author": {
            "login": "chatgpt-codex-connector"}}]
        ok, msg = _gate(
            labelled("author:agent-1", reviews=reviews), 0)
        self.assertFalse(ok)
        self.assertIn("chatgpt-codex-connector", msg)
        self.assertIn("advisory", msg)

    def test_a_bot_approval_is_still_advisory(self):
        ok, msg = _gate(
            labelled("author:agent-1", review_login="chatgpt-codex-connector"), 0)
        self.assertFalse(ok)
        self.assertIn("advisory", msg)

    def test_an_external_approval_counts_without_any_label(self):
        ok, _ = _gate(
            labelled("author:agent-1", review_login="some-colleague"), 0)
        self.assertTrue(ok)

    def test_an_external_comment_does_not_count_without_approval(self):
        reviews = [{"state": "COMMENTED", "author": {"login": "some-colleague"}}]
        ok, msg = _gate(
            labelled("author:agent-1", reviews=reviews), 0)
        self.assertFalse(ok)
        self.assertIn("reviewed-by:", msg)

    def test_same_account_still_needs_the_labels(self):
        ok, msg = _gate(labelled("author:agent-1"), 0)
        self.assertFalse(ok)
        # Names the attribution the gate reads and the command that writes it,
        # so the remedy is executable rather than a label to invent.
        self.assertIn("reviewed-by:", msg)
        self.assertIn("--complete-review", msg)


class SelfReviewTests(unittest.TestCase):
    """Every agent is the same GitHub user, so GitHub cannot catch this."""

    def test_self_review_is_refused(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())

    def test_peer_review_passes(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_a_peer_alongside_a_self_review_passes(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-1", "reviewed-by:agent-3"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-3", msg)

    def test_review_without_attribution_is_refused(self):
        # Unattributable on a stamped PR: it cannot be told apart from a
        # self-review, so it must not pass.
        ok, msg = _gate(labelled("author:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("reviewed-by:", msg)


    def test_unstamped_pr_fails_closed(self):
        ok, msg = _gate(labelled(), 0)
        self.assertFalse(ok)
        self.assertIn("author:<id>", msg)
        self.assertIn("create_pr.py", msg)

    def test_unstamped_pr_with_external_approval_still_fails_closed(self):
        ok, msg = _gate(
            labelled(review_login="some-colleague"), 0)
        self.assertFalse(ok)
        self.assertIn("author:<id>", msg)

    def test_same_family_review_warns_but_does_not_refuse(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2", "same-family-review"), 0)
        self.assertTrue(ok)
        self.assertIn("Same-family", msg)

    def test_self_review_refusal_outranks_nothing_else_being_wrong(self):
        # CI green, threads resolved, criteria ticked - still refused.
        ok, _ = _gate(
            labelled("author:solo", "reviewed-by:solo",
                     reviews=[{"state": "APPROVED"}, {"state": "COMMENTED"}]), 0)
        self.assertFalse(ok)


class ClaimIsNotAttestationTests(unittest.TestCase):
    """A review claim records queue occupancy, not that anyone read the diff.

    An earlier revision of this fix accepted `reviewer:` as proof of review.
    That let the author leave a same-account COMMENTED review, any peer claim
    the PR, and the gate pass before that peer had looked at anything. On the
    repository's only merge gate.
    """

    def test_a_peer_claim_alone_does_not_satisfy_the_gate(self):
        # The exploit, verbatim: author's own review + a peer's bare claim.
        ok, msg = _gate(
            labelled("author:agent-1", "reviewer:agent-2"), 0)
        self.assertFalse(ok)
        self.assertIn("complete-review", msg)

    def test_the_refusal_names_the_claimant_and_the_command(self):
        _, msg = _gate(
            labelled("author:agent-1", "reviewer:agent-2"), 0)
        self.assertIn("agent-2", msg)
        self.assertIn("--complete-review", msg)

    def test_completed_attribution_satisfies_the_gate(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_a_claim_alongside_completed_attribution_still_blocks(self):
        # A held claim is live queue ownership and must be released even if an
        # earlier reviewer already completed a separate review.
        ok, msg = _gate(
            labelled("author:agent-1", "reviewer:agent-2", "reviewed-by:agent-2"), 0)
        self.assertFalse(ok)
        self.assertIn("still in progress", msg)

    def test_bot_comment_plus_peer_claim_blocks(self):
        reviews = [{"state": "COMMENTED", "author": {
            "login": "chatgpt-codex-connector"}}]
        ok, msg = _gate(
            labelled("author:agent-1", "reviewer:agent-2", reviews=reviews), 0)
        self.assertFalse(ok)
        self.assertIn("agent-2", msg)
        self.assertIn("still in progress", msg)

    def test_external_approval_plus_peer_claim_blocks(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewer:agent-2",
                     review_login="some-colleague"), 0)
        self.assertFalse(ok)
        self.assertIn("agent-2", msg)

    def test_self_attribution_is_still_a_self_review(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())


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
    def test_oversized_diff_passes_with_visible_independent_review_warning(self):
        ok, msg = merge_pr.check_size({"additions": 800, "deletions": 100})
        self.assertTrue(ok)
        self.assertIn("soft limit", msg)
        self.assertIn("Independent review remains mandatory", msg)
        self.assertNotIn("human", msg.lower())

    def test_small_diff_passes(self):
        self.assertTrue(merge_pr.check_size({"additions": 10, "deletions": 2})[0])


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


def merged_pr():
    return {
        "number": 9,
        "title": "merged",
        "body": "Closes #7",
        "state": "MERGED",
        "mergedAt": "2026-08-10T00:00:00Z",
        "mergeCommit": {"oid": "merge-sha"},
        "headRefName": "fix/issue-7-example",
        "headRefOid": "gated-sha",
        "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
        "headRepositoryOwner": {"login": "owner"},
    }


class MergeExecutionRecoveryTests(unittest.TestCase):
    @patch.object(merge_pr, "fetch_pr", return_value=merged_pr())
    @patch.object(merge_pr.subprocess, "run")
    def test_nonzero_merge_command_recovers_when_server_reports_merged(self, run, _fetch):
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="delete failed")
        final, message = merge_pr.execute_merge(
            9, {"headRefOid": "gated-sha"}, "squash"
        )

        self.assertEqual(final["state"], "MERGED")
        self.assertIn("reports merged", message)
        command = run.call_args.args[0]
        self.assertNotIn("--delete-branch", command)
        self.assertIn("--match-head-commit", command)

    @patch.object(merge_pr, "fetch_pr", return_value={"state": "OPEN"})
    @patch.object(merge_pr.subprocess, "run")
    def test_nonzero_merge_command_distinguishes_not_merged(self, run, _fetch):
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="refused")
        final, message = merge_pr.execute_merge(9, {"headRefOid": "sha"}, "squash")

        self.assertIsNone(final)
        self.assertIn("still reports OPEN", message)

    @patch.object(merge_pr, "run_closeout", return_value=True)
    @patch.object(merge_pr, "repository_root", return_value="/repo")
    @patch.object(merge_pr, "execute_merge")
    @patch.object(merge_pr, "fetch_pr", return_value=merged_pr())
    def test_rerun_of_merged_pr_skips_second_merge(
        self, _fetch, execute, _root, closeout
    ):
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_OK)

        execute.assert_not_called()
        closeout.assert_called_once()

    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "review clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(False, "delete failed"))
    @patch.object(merge_pr, "retain_local_branch", return_value=(True, "local retained"))
    @patch.object(merge_pr, "prune_worktree", return_value=(True, "worktree pruned"))
    @patch.object(merge_pr.os, "chdir")
    @patch.object(merge_pr, "repository_root", return_value="/repo")
    @patch.object(merge_pr, "execute_merge", return_value=(merged_pr(), "merged"))
    @patch.object(
        merge_pr,
        "review_evidence",
        return_value={
            "head_oid": "gated-sha", "unresolved": 0, "unfixed": 0,
            "withdrawn": 0, "reviewed_head": True,
        },
    )
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    def test_successful_merge_with_branch_delete_failure_is_resumable(
        self, fetch, _json, _threads, execute, _root, _chdir, _prune, _local,
        _remote, _close, _done, _issue_claim, _review_claim,
    ):
        fetch.return_value = {
            "number": 9,
            "title": "open",
            "body": "Closes #7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "fix/issue-7-example",
            "headRefOid": "gated-sha",
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [{"state": "APPROVED", "author": {"login": "peer"}}],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_ERROR)

        execute.assert_called_once_with(9, fetch.return_value, "merge")
        _close.assert_called_once_with(7)
        _done.assert_called_once_with(7)
        _issue_claim.assert_called_once_with(7)
        _review_claim.assert_called_once_with(9)

    @patch.object(merge_pr, "run_closeout", return_value=True)
    @patch.object(merge_pr, "repository_root", return_value="/repo")
    @patch.object(merge_pr, "execute_merge", return_value=(merged_pr(), "merged"))
    @patch.object(
        merge_pr,
        "review_evidence",
        return_value={
            "head_oid": "gated-sha", "unresolved": 0, "unfixed": 0,
            "withdrawn": 0, "reviewed_head": True,
        },
    )
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    def test_default_merge_method_is_merge(
        self, fetch, _json, _threads, execute, _root, closeout
    ):
        fetch.return_value = {
            "number": 9,
            "title": "open",
            "body": "Closes #7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "fix/issue-7-example",
            "headRefOid": "gated-sha",
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [{"state": "APPROVED", "author": {"login": "peer"}}],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_OK)

        execute.assert_called_once_with(9, fetch.return_value, "merge")


class CloseOutRecoveryTests(unittest.TestCase):
    def _run(self, failing):
        outcomes = {
            "prune_worktree": (True, "worktree ok"),
            "retain_local_branch": (True, "local ok"),
            "delete_remote_branch": (True, "remote ok"),
            "ensure_issue_closed": (True, "closed"),
            "reconcile_issue_done": (True, "done"),
            "clear_issue_claims": (True, "issue claim clear"),
            "clear_review_claims": (True, "review claim clear"),
            "clear_merger_claims": (True, "merger claim clear"),
        }
        outcomes[failing] = (False, f"{failing} failed")
        patches = {
            name: patch.object(merge_pr, name, return_value=value)
            for name, value in outcomes.items()
        }
        mocks = {name: item.start() for name, item in patches.items()}
        try:
            with patch.object(merge_pr.os, "chdir"):
                ok = merge_pr.run_closeout(merged_pr(), [7], "/repo")
        finally:
            for item in patches.values():
                item.stop()
        return ok, mocks

    def test_merge_success_plus_remote_branch_failure_runs_remaining_closeout(self):
        ok, mocks = self._run("delete_remote_branch")
        self.assertFalse(ok)
        mocks["ensure_issue_closed"].assert_called_once_with(7)
        mocks["reconcile_issue_done"].assert_called_once_with(7)
        mocks["clear_review_claims"].assert_called_once_with(9)
        mocks["clear_merger_claims"].assert_not_called()

    def test_worktree_failure_does_not_skip_branch_or_board_cleanup(self):
        ok, mocks = self._run("prune_worktree")
        self.assertFalse(ok)
        mocks["retain_local_branch"].assert_called_once()
        mocks["delete_remote_branch"].assert_called_once()
        mocks["reconcile_issue_done"].assert_called_once_with(7)

    def test_board_failure_does_not_skip_claim_cleanup(self):
        ok, mocks = self._run("reconcile_issue_done")
        self.assertFalse(ok)
        mocks["clear_issue_claims"].assert_called_once_with(7)
        mocks["clear_review_claims"].assert_called_once_with(9)
        mocks["clear_merger_claims"].assert_not_called()

    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear"))
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "review clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(True, "remote"))
    @patch.object(merge_pr, "retain_local_branch", return_value=(True, "local"))
    @patch.object(merge_pr, "prune_worktree", return_value=(True, "worktree"))
    @patch.object(merge_pr.os, "chdir")
    def test_changes_to_surviving_root_before_pruning_caller_worktree(
        self, chdir, prune, _local, _remote, _close, _done, _issue, _review, _merger
    ):
        def after_chdir(*_args):
            chdir.assert_called_once_with("/repo")
            return True, "worktree"

        prune.side_effect = after_chdir
        self.assertTrue(merge_pr.run_closeout(merged_pr(), [7], "/repo"))
        chdir.assert_called_once_with("/repo")


class IdempotentCloseOutStepTests(unittest.TestCase):
    @staticmethod
    def _git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "run_cmd", return_value=(0, "", ""))
    def test_absent_remote_branch_is_already_done(self, run, _slug):
        ok, message = merge_pr.delete_remote_branch(
            "/repo", "fix/issue-7-x", "gated-sha", "owner/repo"
        )
        self.assertTrue(ok)
        self.assertIn("already absent", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "run_cmd", return_value=(1, "", "missing"))
    def test_absent_local_branch_is_already_done(self, run):
        ok, message = merge_pr.retain_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertTrue(ok)
        self.assertIn("already absent", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "run_cmd", return_value=(0, "worktree /repo\nbranch refs/heads/main\n", ""))
    def test_absent_issue_worktree_is_already_done(self, _run):
        ok, message = merge_pr.prune_worktree(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertTrue(ok)
        self.assertIn("already absent", message)

    @patch.object(merge_pr, "run_cmd")
    def test_prefix_branch_worktree_is_not_selected(self, run):
        run.return_value = (
            0,
            "worktree /repo/.worktrees/fix-xyz\n"
            "HEAD other-sha\n"
            "branch refs/heads/fix/xyz\n",
            "",
        )
        ok, message = merge_pr.prune_worktree("/repo", "fix/x", "gated-sha")
        self.assertTrue(ok)
        self.assertIn("already absent", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "run_cmd")
    def test_empty_branch_fails_closed_without_git_mutation(self, run):
        ok, message = merge_pr.prune_worktree("/repo", "", "gated-sha")
        self.assertFalse(ok)
        self.assertIn("required", message)
        run.assert_not_called()

    @patch.object(merge_pr, "run_cmd")
    def test_reused_worktree_branch_at_new_sha_is_preserved(self, run):
        run.return_value = (
            0,
            "worktree /repo/.worktrees/reused\n"
            "HEAD new-sha\n"
            "branch refs/heads/fix/issue-7-x\n",
            "",
        )
        ok, message = merge_pr.prune_worktree(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertFalse(ok)
        self.assertIn("left untouched", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "run_cmd", return_value=(0, "new-sha\n", ""))
    def test_reused_local_branch_at_new_sha_is_preserved(self, run):
        ok, message = merge_pr.retain_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertTrue(ok)
        self.assertIn("unrelated ref retained", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/base")
    @patch.object(merge_pr, "run_cmd", return_value=(0, "new-sha\trefs/heads/fix/x\n", ""))
    def test_reused_fork_branch_at_new_sha_is_preserved(self, run, _slug):
        ok, message = merge_pr.delete_remote_branch(
            "/repo", "fix/x", "gated-sha", "contributor/fork"
        )
        self.assertFalse(ok)
        self.assertIn("left untouched", message)
        command = run.call_args.args[0]
        self.assertIn("https://github.com/contributor/fork.git", command)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "run_cmd")
    def test_remote_branch_moved_after_observation_survives_lease_delete(
        self, run, _slug
    ):
        run.side_effect = [
            (0, "gated-sha\trefs/heads/fix/x\n", ""),
            (1, "", "stale info"),
        ]
        ok, message = merge_pr.delete_remote_branch(
            "/repo", "fix/x", "gated-sha", "owner/repo"
        )
        self.assertFalse(ok)
        self.assertIn("atomically", message)
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "git", "push",
                "--force-with-lease=refs/heads/fix/x:gated-sha",
                "origin", ":refs/heads/fix/x",
            ],
        )

    def test_real_worktree_attachment_interleaving_keeps_local_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            worktree = repo / ".worktrees" / "race"
            self._git(directory, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            self._git(repo, "commit", "--allow-empty", "-m", "seed")
            expected_sha = self._git(repo, "rev-parse", "HEAD")
            branch = "fix/race"
            self._git(repo, "branch", branch, expected_sha)
            worktree.parent.mkdir()

            real_run_cmd = merge_pr.run_cmd
            raced = False

            def inject_attachment(command, **kwargs):
                nonlocal raced
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "rev-parse", "--verify"]:
                    self._git(repo, "worktree", "add", str(worktree), branch)
                    raced = True
                return result

            with patch.object(merge_pr, "run_cmd", side_effect=inject_attachment):
                ok, message = merge_pr.retain_local_branch(
                    repo, branch, expected_sha
                )

            self.assertTrue(raced)
            self.assertTrue(ok)
            self.assertIn("Retained local branch", message)
            self.assertTrue(worktree.exists())
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"),
                expected_sha,
            )

    def test_real_remote_ref_replacement_survives_lease_delete_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            repo = root / "repo"
            self._git(root, "init", "--bare", "--initial-branch=main", str(origin))
            self._git(root, "clone", str(origin), str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            self._git(repo, "commit", "--allow-empty", "-m", "old")
            old_sha = self._git(repo, "rev-parse", "HEAD")
            branch = "fix/race"
            self._git(repo, "push", "origin", f"{old_sha}:refs/heads/{branch}")
            self._git(repo, "commit", "--allow-empty", "-m", "replacement")
            replacement_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "push", "origin", "main")

            real_run_cmd = merge_pr.run_cmd
            raced = False

            def inject_replacement(command, **kwargs):
                nonlocal raced
                if command[:2] == ["git", "push"] and any(
                    item.startswith("--force-with-lease=") for item in command
                ):
                    self._git(
                        repo, "push", "--force", "origin",
                        f"{replacement_sha}:refs/heads/{branch}",
                    )
                    raced = True
                return real_run_cmd(command, **kwargs)

            with (
                patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"),
                patch.object(merge_pr, "run_cmd", side_effect=inject_replacement),
            ):
                ok, message = merge_pr.delete_remote_branch(
                    repo, branch, old_sha, "owner/repo"
                )

            self.assertTrue(raced)
            self.assertFalse(ok)
            self.assertIn("atomically", message)
            remote = self._git(
                repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
            )
            self.assertEqual(remote.split()[0], replacement_sha)

    def test_real_dirty_worktree_preserves_file_and_local_branch_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "dirty"
            branch = "fix/dirty"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            tracked = repo / "tracked.txt"
            tracked.write_text("clean\n")
            self._git(repo, "add", "tracked.txt")
            self._git(repo, "commit", "-m", "seed")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            dirty_file = worktree / "tracked.txt"
            dirty_file.write_text("user change\n")

            pruned, _ = merge_pr.prune_worktree(repo, branch, expected_sha)
            retained, message = merge_pr.retain_local_branch(
                repo, branch, expected_sha
            )

            self.assertFalse(pruned)
            self.assertTrue(retained)
            self.assertIn("Retained local branch", message)
            self.assertEqual(dirty_file.read_text(), "user change\n")
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"),
                expected_sha,
            )

    def test_real_ignored_file_survives_worktree_prune_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "ignored"
            branch = "fix/ignored"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            (repo / ".gitignore").write_text("secret.txt\n")
            self._git(repo, "add", ".gitignore")
            self._git(repo, "commit", "-m", "ignore local secret")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            secret = worktree / "secret.txt"
            secret.write_text("keep me\n")

            pruned, message = merge_pr.prune_worktree(repo, branch, expected_sha)

            self.assertFalse(pruned)
            self.assertIn("ignored", message)
            self.assertTrue(secret.exists())
            self.assertEqual(secret.read_text(), "keep me\n")

    def test_ignored_file_created_after_preflight_is_atomically_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "race-secret"
            branch = "fix/race-secret"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            (repo / ".gitignore").write_text("secret.txt\n")
            self._git(repo, "add", ".gitignore")
            self._git(repo, "commit", "-m", "ignore local secret")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            secret = worktree / "secret.txt"
            retained = (
                repo / ".worktrees" / ".retained" /
                f"{expected_sha[:12]}-{worktree.name}"
            )
            real_run_cmd = merge_pr.run_cmd
            raced = False

            def inject_secret_after_preflight(command, **kwargs):
                nonlocal raced
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "status", "--porcelain"]:
                    secret.write_text("late secret\n")
                    raced = True
                return result

            with patch.object(
                merge_pr, "run_cmd", side_effect=inject_secret_after_preflight
            ):
                pruned, message = merge_pr.prune_worktree(
                    repo, branch, expected_sha
                )

            self.assertTrue(raced)
            self.assertTrue(pruned)
            self.assertIn("Retained worktree", message)
            self.assertFalse(worktree.exists())
            self.assertEqual((retained / "secret.txt").read_text(), "late secret\n")
            listed = self._git(repo, "worktree", "list", "--porcelain")
            self.assertNotIn(f"refs/heads/{branch}", listed)

    def test_branch_switch_after_discovery_is_revalidated_under_head_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "switch-race"
            branch = "fix/switch-race"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            self._git(repo, "commit", "--allow-empty", "-m", "seed")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            real_run_cmd = merge_pr.run_cmd
            switched = False

            def switch_after_discovery(command, **kwargs):
                nonlocal switched
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "worktree", "list"] and not switched:
                    self._git(worktree, "switch", "-c", "unrelated")
                    switched = True
                return result

            with patch.object(
                merge_pr, "run_cmd", side_effect=switch_after_discovery
            ):
                pruned, message = merge_pr.prune_worktree(
                    repo, branch, expected_sha
                )

            self.assertTrue(switched)
            self.assertFalse(pruned)
            self.assertIn("ownership changed", message)
            self.assertTrue(worktree.exists())
            self.assertEqual(self._git(worktree, "branch", "--show-current"), "unrelated")
            listed = self._git(repo, "worktree", "list", "--porcelain")
            self.assertIn("refs/heads/unrelated", listed)

    def test_same_tree_ref_update_is_blocked_through_deregistration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "ref-race"
            branch = "fix/ref-race"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            self._git(repo, "commit", "--allow-empty", "-m", "seed")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            self._git(repo, "commit", "--allow-empty", "-m", "same tree replacement")
            replacement_sha = self._git(repo, "rev-parse", "HEAD")
            retained = (
                repo / ".worktrees" / ".retained" /
                f"{expected_sha[:12]}-{worktree.name}"
            )
            real_run_cmd = merge_pr.run_cmd
            update_attempt = None

            def update_after_locked_sha_read(command, **kwargs):
                nonlocal update_attempt
                result = real_run_cmd(command, **kwargs)
                if command == ["git", "rev-parse", "HEAD"]:
                    update_attempt = subprocess.run(
                        [
                            "git", "-C", str(repo), "update-ref",
                            f"refs/heads/{branch}", replacement_sha, expected_sha,
                        ],
                        text=True,
                        capture_output=True,
                    )
                return result

            with patch.object(
                merge_pr, "run_cmd", side_effect=update_after_locked_sha_read
            ):
                pruned, message = merge_pr.prune_worktree(
                    repo, branch, expected_sha
                )

            self.assertIsNotNone(update_attempt)
            self.assertNotEqual(update_attempt.returncode, 0)
            self.assertTrue(pruned)
            self.assertIn("Retained worktree", message)
            self.assertTrue(retained.is_dir())
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"),
                expected_sha,
            )
            listed = self._git(repo, "worktree", "list", "--porcelain")
            self.assertNotIn(f"refs/heads/{branch}", listed)

    @patch.object(merge_pr, "_gh_json", return_value={"state": "CLOSED"})
    def test_closed_issue_is_already_done(self, _gh):
        ok, message = merge_pr.ensure_issue_closed(7)
        self.assertTrue(ok)
        self.assertIn("already closed", message)

    @patch.object(merge_pr, "_gh_json", return_value={"labels": []})
    def test_absent_claim_labels_are_already_done(self, _gh):
        self.assertTrue(merge_pr.clear_issue_claims(7)[0])
        self.assertTrue(merge_pr.clear_review_claims(9)[0])



class ExpectedHeadGateTests(unittest.TestCase):
    @staticmethod
    def open_pr(head):
        return {
            "number": 9, "title": "t", "body": "Closes #7", "state": "OPEN",
            "headRefOid": head, "labels": [], "reviews": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "additions": 1, "deletions": 1, "author": {"login": "gillella"},
        }

    @patch.object(merge_pr, "fetch_pr")
    def test_expected_head_mismatch_blocks_before_merge(self, fetch_pr):
        pr = {
            "number": 9, "title": "t", "body": "Closes #7", "state": "OPEN",
            "headRefOid": "live-b", "labels": [], "reviews": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "additions": 1, "deletions": 1, "author": {"login": "gillella"},
        }
        fetch_pr.return_value = pr
        with patch("sys.argv", ["merge_pr.py", "--pr", "9", "--expected-head", "reviewed-a"]):
            rc = merge_pr.main()
        self.assertEqual(rc, merge_pr.EXIT_BLOCKED)

    @patch.object(merge_pr, "execute_merge")
    @patch.object(merge_pr, "evaluate_dod")
    @patch.object(merge_pr, "review_evidence", return_value={"head_oid": "H2"})
    @patch.object(merge_pr, "_gh_json", return_value={"body": ""})
    @patch.object(merge_pr, "fetch_pr")
    def test_push_before_first_review_page_cannot_mix_gate_snapshots(
        self, fetch_pr, _issue, _evidence, evaluate, execute
    ):
        fetch_pr.return_value = self.open_pr("H1")
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            rc = merge_pr.main()

        self.assertEqual(rc, merge_pr.EXIT_BLOCKED)
        evaluate.assert_not_called()
        execute.assert_not_called()

    @patch.object(merge_pr, "execute_merge")
    @patch.object(merge_pr, "evaluate_dod", return_value=(True, []))
    @patch.object(merge_pr, "review_evidence", return_value={"head_oid": "H1"})
    @patch.object(merge_pr, "_gh_json", return_value={"body": ""})
    @patch.object(merge_pr, "fetch_pr")
    def test_push_after_review_pages_cannot_merge_new_head_without_expected_head(
        self, fetch_pr, _issue, _evidence, _evaluate, execute
    ):
        fetch_pr.side_effect = [self.open_pr("H1"), self.open_pr("H2")]
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            rc = merge_pr.main()

        self.assertEqual(rc, merge_pr.EXIT_BLOCKED)
        execute.assert_not_called()


class CloseoutMarkerTests(unittest.TestCase):
    def test_failed_board_reconcile_keeps_recovery_discoverable(self):
        pr = {
            "number": 9, "title": "t", "body": "Closes #7\n", "state": "MERGED",
            "mergedAt": "t", "labels": [],
        }
        with patch.object(merge_pr, "_gh_json", return_value={
            "state": "CLOSED",
            "labels": [{"name": "status:in-review"}],
        }):
            self.assertTrue(merge_pr.closeout_incomplete(pr))


if __name__ == "__main__":
    unittest.main()


class ResolutionIsNotProofTests(unittest.TestCase):
    """Resolving a thread must not, by itself, certify that a finding was fixed.

    PR #62 merged with five blocking findings intact. Nothing was bypassed:
    the reviews were substantive, no latest verdict was CHANGES_REQUESTED
    (the same-account path posts findings as COMMENTED with unresolved
    threads), the threads were resolved, attribution was present, CI was
    green. Zero-unresolved was doing work it cannot do - resolution is a UI
    toggle with no relationship to the diff.
    """

    PASSING = ("author:agent-1", "reviewed-by:agent-2")

    def test_a_resolved_finding_with_no_commit_after_it_blocks(self):
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 1, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertFalse(ok)
        self.assertIn("no commit after the finding", msg)

    def test_the_refusal_names_both_remedies(self):
        """A refusal an agent cannot act on becomes a workaround."""
        _, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 2, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertIn("Push the fix", msg)
        self.assertIn("Withdrawn:", msg)

    def test_a_resolved_finding_followed_by_a_commit_passes(self):
        ok, _ = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertTrue(ok)

    def test_an_explicitly_withdrawn_finding_passes_without_a_commit(self):
        """The escape hatch that keeps the commit rule from forcing no-op commits.

        Without it a reviewer who withdraws a finding leaves the PR unmergeable,
        and the rational response is to manufacture an empty commit - an audit
        trail that lies, which is worse than the gap being closed.
        """
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 1, "reviewed_head": True},
        )
        self.assertTrue(ok)
        self.assertIn("withdrawn, not fixed", msg)

    def test_the_audit_line_distinguishes_fixed_from_withdrawn(self):
        """A later reader must be able to tell why the merge was allowed."""
        _, fixed = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": True},
        )
        _, withdrawn = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 2, "reviewed_head": True},
        )
        self.assertNotIn("withdrawn", fixed)
        self.assertIn("2 finding(s) withdrawn", withdrawn)

    def test_unresolved_still_blocks_before_the_new_checks(self):
        """Ordering matters: the clearer refusal should win."""
        _, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 1, "unfixed": 1, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertIn("unresolved review thread", msg)


class ReviewMustCoverHeadTests(unittest.TestCase):
    """A review attests to the commit it was submitted against.

    Once head moves, the attestation covers code that is no longer proposed,
    so a reviewed PR could be force-pushed and merged on the stale verdict.
    """

    PASSING = ("author:agent-1", "reviewed-by:agent-2")

    def test_a_review_of_an_earlier_head_blocks(self):
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": False},
        )
        self.assertFalse(ok)
        self.assertIn("predates the current head", msg)

    def test_a_review_at_head_passes(self):
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertTrue(ok)
        self.assertIn("reviewed at head", msg)


class WithdrawnMarkerTests(unittest.TestCase):
    """What counts as declaring a finding withdrawn."""

    def test_marker_matches_at_the_start_of_a_reply(self):
        self.assertTrue(merge_pr.WITHDRAWN_MARKER.search("Withdrawn: not a real issue"))

    def test_bare_or_later_marker_does_not_count(self):
        self.assertIsNone(merge_pr.WITHDRAWN_MARKER.search("withdrawn"))
        self.assertIsNone(merge_pr.WITHDRAWN_MARKER.search("withdrawn - my mistake"))
        self.assertIsNone(
            merge_pr.WITHDRAWN_MARKER.search("Finding details\nWithdrawn: reason")
        )
        self.assertIsNone(
            merge_pr.WITHDRAWN_MARKER.search("Finding details\n> Withdrawn: reason")
        )

    def test_marker_matches_through_bold_formatting(self):
        # Agents routinely write **Withdrawn:**; the convention should not
        # hinge on markdown.
        self.assertTrue(merge_pr.WITHDRAWN_MARKER.search("**Withdrawn:** superseded"))

    def test_the_word_in_prose_does_not_count(self):
        """Otherwise discussing withdrawal would silently satisfy the gate."""
        self.assertIsNone(
            merge_pr.WITHDRAWN_MARKER.search("I do not think this should be withdrawn.")
        )


class OutdatedThreadEvidenceTests(unittest.TestCase):
    """An outdated thread stops gating only when evidence shows it was addressed or withdrawn."""

    PASSING = ("author:agent-1", "reviewed-by:agent-2")

    def test_outdated_unresolved_thread_without_commit_blocks(self):
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 1, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertFalse(ok)
        self.assertIn("outdated review thread(s) without evidence", msg)

    def test_pr_140_shape_four_findings_one_outdated_unfixed_accounted_for(self):
        """Reproduces PR #140 shape: 4 findings, 1 anchor line deleted, 0 commits after finding."""
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 3, "unfixed": 0, "outdated_unfixed": 1, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertFalse(ok)
        self.assertIn("4 unresolved review thread(s) (1 outdated without evidence)", msg)

    def test_outdated_unresolved_thread_with_commit_after_finding_passes(self):
        ok, _ = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertTrue(ok)

    @patch("merge_pr.get_repo_slug", return_value="owner/repo")
    @patch("merge_pr._gh_json")
    def test_review_evidence_parses_outdated_threads_with_and_without_evidence(self, mock_gh_json, _mock_slug):
        gql_data = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": "head123",
                        "reviews": {
                            "nodes": [{"state": "COMMENTED", "author": {"login": "agent-2"}, "commit": {"oid": "head123"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "commits": {"nodes": [{"commit": {"committedDate": "2026-08-10T10:00:00Z"}}]},
                        "reviewThreads": {
                            "nodes": [
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 1"}]}},
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 2"}]}},
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 3"}]}},
                                {"isResolved": False, "isOutdated": True, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 4 (anchor line deleted)"}]}},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        }
        mock_gh_json.return_value = gql_data
        evidence = merge_pr.review_evidence(140)
        self.assertEqual(evidence["unresolved"], 3)
        self.assertEqual(evidence["outdated_unfixed"], 1)
        self.assertEqual(evidence["outdated_addressed"], 0)

        gql_data_with_commit = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": "head123",
                        "reviews": {
                            "nodes": [{"state": "COMMENTED", "author": {"login": "agent-2"}, "commit": {"oid": "head123"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "commits": {
                            "nodes": [
                                {"commit": {"committedDate": "2026-08-10T10:00:00Z"}},
                                {"commit": {"committedDate": "2026-08-10T12:00:00Z"}},
                            ]
                        },
                        "reviewThreads": {
                            "nodes": [
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 1"}]}},
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 2"}]}},
                                {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 3"}]}},
                                {"isResolved": False, "isOutdated": True, "comments": {"nodes": [{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 4 (anchor line deleted)"}]}},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        }
        mock_gh_json.return_value = gql_data_with_commit
        evidence_after_commit = merge_pr.review_evidence(140)
        self.assertEqual(evidence_after_commit["unresolved"], 3)
        self.assertEqual(evidence_after_commit["outdated_unfixed"], 0)
        self.assertEqual(evidence_after_commit["outdated_addressed"], 1)

    def test_evidence_note_formats_outdated_threads(self):
        note = merge_pr._evidence_note({"outdated_addressed": 2, "withdrawn": 0})
        self.assertEqual(
            note,
            "reviewed at head, no blocking unresolved threads "
            "(2 outdated with commit evidence).",
        )

        note_clean = merge_pr._evidence_note({"outdated_addressed": 0, "withdrawn": 1})
        self.assertEqual(note_clean, "reviewed at head, no unresolved threads, 1 finding(s) withdrawn, not fixed.")


def checkpoint_pr():
    """A merged PR carrying the identity labels the checkpoint has to record."""
    pr = merged_pr()
    pr["title"] = "feat: the thing"
    pr["labels"] = [
        {"name": "author:cursor-1"},
        {"name": "family:xai"},
        {"name": "reviewed-by:claude-rev-9"},
    ]
    return pr


CHECKPOINT_GATES = [
    ("open", True, "PR is open."),
    ("ci", True, "CI green (4 checks)."),
    ("review", True, "1 review(s) from claude-rev-9."),
    ("size", True, "Diff is 59 lines."),
]


class CheckpointTagNameTests(unittest.TestCase):
    def test_name_pairs_the_pr_with_its_merge_commit(self):
        self.assertEqual(
            merge_pr.checkpoint_tag_name(120, "9ac804ae774509b47cbd1e735e4cfbb50edebc6b"),
            "ckpt/120-9ac804a",
        )

    def test_the_same_merge_commit_always_yields_the_same_name(self):
        """Idempotency rests on this: a resumed close-out must not mint a second tag."""
        first = merge_pr.checkpoint_tag_name(9, "abcdef1234567890")
        second = merge_pr.checkpoint_tag_name(9, "abcdef1234567890")
        self.assertEqual(first, second)

    def test_an_unknown_merge_sha_has_no_name(self):
        self.assertEqual(merge_pr.checkpoint_tag_name(9, ""), "")
        self.assertEqual(merge_pr.checkpoint_tag_name(9, "unknown"), "")


class CheckpointMessageTests(unittest.TestCase):
    def _message(self, gates=CHECKPOINT_GATES):
        return merge_pr.checkpoint_message(
            checkpoint_pr(), [7], gates, "gated-sha", "merge-sha"
        )

    def test_records_the_whole_verdict_set(self):
        message = self._message()
        for fragment in ("#9", "feat: the thing", "#7", "cursor-1",
                         "claude-rev-9", "gated-sha", "merge-sha"):
            self.assertIn(fragment, message)
        for name, _, _ in CHECKPOINT_GATES:
            self.assertIn(name, message)

    def test_records_the_gated_head_not_only_the_merge_commit(self):
        """The gated head is the commit the gate actually approved."""
        self.assertIn("gated-sha", self._message())

    def test_a_resumed_closeout_admits_the_gap_rather_than_inventing_verdicts(self):
        """Re-deriving gates post-merge would record failures that never happened.

        ``check_open`` fails on a closed PR, so a re-evaluated verdict block on
        the resume path would be actively false. The checkpoint says so instead.
        """
        message = self._message(gates=None)
        self.assertNotIn("✅", message)
        self.assertIn("not reproducible", message.lower())

    def test_a_failed_gate_is_recorded_as_failed(self):
        message = self._message(gates=[("size", False, "Diff is 2000 lines.")])
        self.assertIn("size", message)
        self.assertIn("❌", message)


class WriteCheckpointTagTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _run_cmd(self, missing_tag=True, tag_rc=0, push_rc=0, object_present=True):
        def fake(cmd, check=True, cwd=None):
            self.calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return 0, "", ""
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                return (1, "", "not found") if missing_tag else (0, "tagsha", "")
            if cmd[:2] == ["git", "cat-file"]:
                return (0, "", "") if object_present else (1, "", "missing object")
            if cmd[:2] == ["git", "tag"]:
                return tag_rc, "", ("tag failed" if tag_rc else "")
            if cmd[:2] == ["git", "push"]:
                return push_rc, "", ("push failed" if push_rc else "")
            return 0, "", ""
        return fake

    def _write(self, **kwargs):
        with patch.object(merge_pr, "run_cmd", side_effect=self._run_cmd(**kwargs)):
            return merge_pr.write_checkpoint_tag(
                "/repo", checkpoint_pr(), [7], CHECKPOINT_GATES, "gated-sha", "merge-sha"
            )

    def _tag_calls(self):
        return [c for c in self.calls if c[:2] == ["git", "tag"]]

    def test_writes_an_annotated_tag_at_the_merge_commit(self):
        ok, message = self._write()
        self.assertTrue(ok)
        tag_calls = self._tag_calls()
        self.assertEqual(len(tag_calls), 1)
        self.assertIn("-a", tag_calls[0])
        self.assertIn("ckpt/9-merge-s", tag_calls[0])
        self.assertIn("merge-sha", tag_calls[0])
        self.assertIn("ckpt/9-merge-s", message)

    def test_an_existing_checkpoint_is_never_rewritten_or_moved(self):
        """Re-running the helper must not duplicate, move, or force-update it."""
        ok, message = self._write(missing_tag=False)
        self.assertTrue(ok)
        self.assertEqual(self._tag_calls(), [])
        self.assertIn("already", message.lower())

    def test_no_code_path_force_updates_a_tag(self):
        self._write()
        for cmd in self.calls:
            self.assertNotIn("-f", cmd)
            self.assertNotIn("--force", cmd)

    def test_a_tag_write_failure_is_reported_not_raised(self):
        ok, message = self._write(tag_rc=1)
        self.assertFalse(ok)
        self.assertIn("tag failed", message)

    def test_a_push_failure_leaves_the_local_checkpoint_intact(self):
        """The local tag satisfies the contract; the push is best effort."""
        ok, message = self._write(push_rc=1)
        self.assertTrue(ok)
        self.assertIn("push", message.lower())

    def test_a_merge_commit_absent_locally_is_reported(self):
        ok, message = self._write(object_present=False)
        self.assertFalse(ok)
        self.assertIn("merge-sha", message)

    def test_an_unknown_merge_sha_writes_nothing(self):
        with patch.object(merge_pr, "run_cmd", side_effect=self._run_cmd()) as run:
            ok, _ = merge_pr.write_checkpoint_tag(
                "/repo", checkpoint_pr(), [7], CHECKPOINT_GATES, "gated-sha", "unknown"
            )
        self.assertFalse(ok)
        run.assert_not_called()


class CheckpointCallSiteTests(unittest.TestCase):
    """The tag is written after close-out, never before, and never on a dry run."""

    def _main(self, argv, closeout_ok=True):
        with patch.object(sys, "argv", argv), \
             patch.object(merge_pr, "fetch_pr", return_value=checkpoint_pr()), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "run_closeout", return_value=closeout_ok), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "written")) as tag:
            code = merge_pr.main()
        return code, tag

    def test_a_completed_closeout_writes_the_checkpoint(self):
        code, tag = self._main(["merge_pr.py", "--pr", "9"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        tag.assert_called_once()

    def test_a_failed_closeout_writes_no_checkpoint(self):
        """Otherwise the checkpoint would attest to a success that did not happen."""
        code, tag = self._main(["merge_pr.py", "--pr", "9"], closeout_ok=False)
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        tag.assert_not_called()

    def test_dry_run_writes_no_checkpoint(self):
        code, tag = self._main(["merge_pr.py", "--pr", "9", "--dry-run"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        tag.assert_not_called()

    def test_a_checkpoint_failure_does_not_fail_a_completed_merge(self):
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", return_value=checkpoint_pr()), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(False, "git tag failed")):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_OK)

    def test_the_resume_path_passes_no_fabricated_gates(self):
        _, tag = self._main(["merge_pr.py", "--pr", "9"])
        self.assertIsNone(tag.call_args.args[3])


class CheckpointPublishRetryTests(unittest.TestCase):
    """B1/B2: existence must not strand the push, and the fetch can create the tag."""

    def setUp(self):
        self.calls = []

    def _run_cmd(self, missing_tag=True, tag_rc=0, push_rc=0, tag_after_fail=True):
        def fake(cmd, check=True, cwd=None):
            self.calls.append(cmd)
            if cmd[:2] == ["git", "fetch"]:
                return 0, "", ""
            if cmd[:3] == ["git", "rev-parse", "--verify"]:
                if not missing_tag:
                    return 0, "tagsha", ""
                # The re-check after a failed `git tag` sees the concurrent tag.
                seen = [c for c in self.calls if c[:3] == ["git", "rev-parse", "--verify"]]
                if len(seen) > 1 and tag_after_fail:
                    return 0, "tagsha", ""
                return 1, "", "not found"
            if cmd[:2] == ["git", "cat-file"]:
                return 0, "", ""
            if cmd[:2] == ["git", "tag"]:
                return tag_rc, "", ("already exists" if tag_rc else "")
            if cmd[:2] == ["git", "push"]:
                return push_rc, "", ("push failed" if push_rc else "")
            return 0, "", ""
        return fake

    def _write(self, **kwargs):
        with patch.object(merge_pr, "run_cmd", side_effect=self._run_cmd(**kwargs)):
            return merge_pr.write_checkpoint_tag(
                "/repo", checkpoint_pr(), [7], CHECKPOINT_GATES, "gated-sha", "merge-sha"
            )

    def _kinds(self):
        return [" ".join(c[:2]) for c in self.calls]

    def test_an_already_recorded_checkpoint_is_still_pushed(self):
        """Otherwise one transient push failure un-publishes it permanently."""
        ok, message = self._write(missing_tag=False)
        self.assertTrue(ok)
        self.assertIn("git push", self._kinds())
        self.assertNotIn("git tag", self._kinds())
        self.assertIn("already recorded", message)

    def test_the_fetch_precedes_the_existence_check(self):
        """A fetch auto-follows tags, so checking first false-fails a healthy merge."""
        self._write()
        kinds = self._kinds()
        self.assertLess(kinds.index("git fetch"), kinds.index("git rev-parse"))

    def test_a_tag_that_appeared_concurrently_is_not_reported_as_failure(self):
        ok, message = self._write(tag_rc=1)
        self.assertTrue(ok)
        self.assertIn("already recorded", message)

    def test_a_genuine_tag_failure_is_still_reported(self):
        ok, message = self._write(tag_rc=1, tag_after_fail=False)
        self.assertFalse(ok)
        self.assertIn("already exists", message)

    def test_a_push_failure_promises_a_later_retry(self):
        ok, message = self._write(push_rc=1)
        self.assertTrue(ok)
        self.assertIn("retries", message.lower())


class GateVerdictPersistenceTests(unittest.TestCase):
    """B3: the resume path writes the real record instead of a permanent blank."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = patch.object(
            merge_pr, "run_cmd", return_value=(0, self.dir, "")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verdicts_survive_a_round_trip(self):
        self.assertTrue(merge_pr.save_gate_verdicts(self.dir, 9, CHECKPOINT_GATES))
        self.assertEqual(merge_pr.load_gate_verdicts(self.dir, 9), CHECKPOINT_GATES)

    def test_absent_verdicts_read_as_none_not_as_an_empty_pass(self):
        """None makes the checkpoint admit the gap; [] would read as 'no gates ran'."""
        self.assertIsNone(merge_pr.load_gate_verdicts(self.dir, 404))

    def test_malformed_verdicts_read_as_none(self):
        path = merge_pr.gate_verdict_path(self.dir, 9)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(merge_pr.load_gate_verdicts(self.dir, 9))

    def test_discard_is_safe_when_nothing_was_parked(self):
        merge_pr.discard_gate_verdicts(self.dir, 404)

    def test_discard_removes_the_parked_verdicts(self):
        merge_pr.save_gate_verdicts(self.dir, 9, CHECKPOINT_GATES)
        merge_pr.discard_gate_verdicts(self.dir, 9)
        self.assertIsNone(merge_pr.load_gate_verdicts(self.dir, 9))


class CheckpointMergePathCallSiteTests(unittest.TestCase):
    """B4: the resume branch is not the merge branch; both need the guarantees."""

    def _main(self, argv, closeout_ok=True):
        open_pr = checkpoint_pr()
        open_pr["state"] = "OPEN"
        open_pr.pop("mergedAt", None)
        with patch.object(sys, "argv", argv), \
             patch.object(merge_pr, "fetch_pr", return_value=open_pr), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "evaluate_dod",
                          return_value=(True, list(CHECKPOINT_GATES))), \
             patch.object(merge_pr, "execute_merge",
                          return_value=(merged_pr(), "merged")), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "save_gate_verdicts", return_value=True), \
             patch.object(merge_pr, "load_gate_verdicts", return_value=None), \
             patch.object(merge_pr, "discard_gate_verdicts"), \
             patch.object(merge_pr, "run_closeout", return_value=closeout_ok), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "written")) as tag:
            code = merge_pr.main()
        return code, tag

    def test_the_merge_path_writes_the_checkpoint_with_real_verdicts(self):
        code, tag = self._main(["merge_pr.py", "--pr", "9"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        tag.assert_called_once()
        self.assertEqual(tag.call_args.args[3], list(CHECKPOINT_GATES))

    def test_dry_run_on_the_merge_path_writes_no_checkpoint(self):
        """The resume branch returns earlier, so only this reaches the merge path."""
        code, tag = self._main(["merge_pr.py", "--pr", "9", "--dry-run"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        tag.assert_not_called()

    def test_a_failed_closeout_on_the_merge_path_writes_no_checkpoint(self):
        code, tag = self._main(["merge_pr.py", "--pr", "9"], closeout_ok=False)
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        tag.assert_not_called()

    def test_the_resume_path_loads_parked_verdicts_when_they_exist(self):
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", return_value=checkpoint_pr()), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "load_gate_verdicts",
                          return_value=list(CHECKPOINT_GATES)) as load, \
             patch.object(merge_pr, "discard_gate_verdicts"), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "written")) as tag:
            merge_pr.main()
        load.assert_called_once()
        self.assertEqual(tag.call_args.args[3], list(CHECKPOINT_GATES))


class DryRunJsonTests(unittest.TestCase):
    """merge_pr --dry-run --json exposes first failing gate without merging."""

    def test_dry_run_json_payload_shape(self):
        pr = {"number": 42, "title": "feat(x): y"}
        gates = [
            ("open", True, "open"),
            ("ci", False, "CI is red"),
            ("review", True, "ok"),
        ]
        payload = merge_pr.dry_run_json_payload(pr, gates, False)
        self.assertEqual(payload["pr"], 42)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["first_blocking"], "ci")
        self.assertEqual(payload["gates"][1]["name"], "ci")
        self.assertFalse(payload["gates"][1]["passed"])

    def test_json_without_dry_run_is_refused(self):
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9", "--json"]), \
             patch.object(merge_pr, "fetch_pr") as fetch:
            code = merge_pr.main()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        fetch.assert_not_called()

    def test_dry_run_json_prints_payload_and_skips_merge(self):
        pr = {
            "number": 9,
            "title": "feat",
            "body": "Closes #1",
            "state": "OPEN",
            "mergedAt": None,
            "headRefOid": "abc",
            "labels": [{"name": "author:a"}, {"name": "reviewed-by:b"}],
        }
        gates = [
            ("open", True, "open"),
            ("issue link", True, "linked"),
            ("verification", True, "ok"),
            ("ci", True, "green"),
            ("review", False, "missing review"),
            ("rebased", True, "clean"),
            ("size", True, "ok"),
            ("accept #1", True, "done"),
        ]
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9", "--dry-run", "--json"]), \
             patch.object(merge_pr, "fetch_pr", return_value=pr), \
             patch.object(merge_pr, "is_merged", return_value=False), \
             patch.object(merge_pr, "_gh_json", return_value={"body": "- [x] done"}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "abc", "unresolved": 0,
                                        "unfixed": 0, "withdrawn": 0}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(False, gates)), \
             patch.object(merge_pr, "execute_merge") as execute_merge, \
             patch("builtins.print") as printer:
            code = merge_pr.main()
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute_merge.assert_not_called()
        printed = " ".join(str(c.args[0]) for c in printer.call_args_list if c.args)
        self.assertIn('"first_blocking": "review"', printed)
        self.assertIn('"ok": false', printed)

    def test_already_merged_dry_run_json_is_pure_json(self):
        pr = {
            "number": 9,
            "title": "feat",
            "body": "Closes #1",
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefOid": "abc",
            "labels": [],
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9", "--dry-run", "--json"]), \
             patch.object(merge_pr, "fetch_pr", return_value=pr), \
             patch.object(merge_pr, "is_merged", return_value=True), \
             patch("builtins.print") as printer:
            code = merge_pr.main()
        self.assertEqual(code, merge_pr.EXIT_OK)
        printed = [str(c.args[0]) for c in printer.call_args_list if c.args]
        self.assertEqual(len(printed), 1)
        payload = __import__("json").loads(printed[0])
        self.assertTrue(payload["already_merged"])
        self.assertTrue(payload["ok"])
