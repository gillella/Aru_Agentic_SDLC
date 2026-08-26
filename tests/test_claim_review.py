import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue
import fetch_pr_feedback


def feedback_page(nodes, has_next=False, cursor=None, head="head-oid", errors=None,
                  reviews=None, review_has_next=False, review_cursor=None):
    data = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviewThreads": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
            "reviews": {
                "nodes": reviews or [],
                "pageInfo": {
                    "hasNextPage": review_has_next,
                    "endCursor": review_cursor,
                },
            },
        }}},
    }
    if errors is not None:
        data["errors"] = errors
    return data


def review_thread(*, resolved=False, outdated=False, body="fix this", oid="head-oid"):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [{
            "databaseId": 17,
            "body": body,
            "path": "scripts/example.py",
            "line": 12,
            "originalLine": 10,
            "url": "https://example.test/thread/17",
            "author": {"login": "reviewer"},
            "commit": {"oid": oid},
        }]},
    }


def pull_review(*, state="CHANGES_REQUESTED", body="change this", oid="head-oid",
                comments=0, login="reviewer", submitted="2026-01-01T00:00:00Z",
                review_id=23):
    return {
        "databaseId": review_id,
        "state": state,
        "body": body,
        "submittedAt": submitted,
        "url": f"https://example.test/review/{review_id}",
        "author": {"login": login},
        "commit": {"oid": oid},
        "comments": {"totalCount": comments},
    }


class ActiveReviewFeedbackTests(unittest.TestCase):
    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_returns_root_comments_from_active_threads(self, run_json, _slug):
        run_json.return_value = feedback_page([review_thread()])

        result = fetch_pr_feedback.fetch_active_review_feedback(7)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["body"], "fix this")
        self.assertEqual(result[0]["path"], "scripts/example.py")
        self.assertEqual(result[0]["user"]["login"], "reviewer")
        self.assertEqual(result[0]["head_oid"], "head-oid")

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_resolved_and_superseded_threads_are_not_feedback(self, run_json, _slug):
        run_json.return_value = feedback_page([
            review_thread(resolved=True),
            review_thread(outdated=True, oid="old-head"),
        ])

        self.assertEqual(fetch_pr_feedback.fetch_active_review_feedback(7), [])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_current_body_only_change_request_is_feedback(self, run_json, _slug):
        run_json.return_value = feedback_page([], reviews=[pull_review()])

        result = fetch_pr_feedback.fetch_active_review_feedback(7)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["body"], "change this")
        self.assertEqual(result[0]["path"], "Pull request review")

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_old_head_or_inline_change_review_is_not_duplicated(self, run_json, _slug):
        run_json.return_value = feedback_page([], reviews=[
            pull_review(oid="old-head", review_id=23),
            pull_review(comments=2, login="another", review_id=24),
        ])

        self.assertEqual(fetch_pr_feedback.fetch_active_review_feedback(7), [])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_later_approval_supersedes_change_request(self, run_json, _slug):
        run_json.return_value = feedback_page([], reviews=[
            pull_review(review_id=23),
            pull_review(state="APPROVED", body="looks good", review_id=24,
                        submitted="2026-01-02T00:00:00Z"),
        ])

        self.assertEqual(fetch_pr_feedback.fetch_active_review_feedback(7), [])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_later_comment_does_not_clear_formal_change_request(self, run_json, _slug):
        run_json.return_value = feedback_page([], reviews=[
            pull_review(review_id=23),
            pull_review(state="COMMENTED", body="follow-up", review_id=24,
                        submitted="2026-01-02T00:00:00Z"),
        ])

        result = fetch_pr_feedback.fetch_active_review_feedback(7)

        self.assertEqual([item["body"] for item in result], ["change this"])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_advisory_bot_change_request_is_not_author_feedback(self, run_json, _slug):
        run_json.return_value = feedback_page([], reviews=[
            pull_review(login="chatgpt-codex-connector"),
        ])

        self.assertEqual(fetch_pr_feedback.fetch_active_review_feedback(7), [])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_plain_issue_comments_are_outside_the_review_thread_query(self, run_json, _slug):
        page = feedback_page([])
        page["data"]["repository"]["pullRequest"]["comments"] = {
            "nodes": [{"body": "approval-style bot message"}]
        }
        run_json.return_value = page

        self.assertEqual(fetch_pr_feedback.fetch_active_review_feedback(7), [])
        query_arg = next(arg for arg in run_json.call_args.args[0] if arg.startswith("query="))
        self.assertNotIn("pullRequest(number:$pr) {\n          comments", query_arg)

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_paginates_all_threads(self, run_json, _slug):
        run_json.side_effect = [
            feedback_page([review_thread(body="first")], True, "A"),
            feedback_page([review_thread(body="second")]),
        ]

        result = fetch_pr_feedback.fetch_active_review_feedback(7)

        self.assertEqual([item["body"] for item in result], ["first", "second"])
        self.assertIn("threadCursor=A", run_json.call_args_list[1].args[0])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_paginates_all_reviews(self, run_json, _slug):
        run_json.side_effect = [
            feedback_page([], reviews=[pull_review(body="first", login="first")],
                          review_has_next=True, review_cursor="R"),
            feedback_page([], reviews=[pull_review(body="second", login="second")]),
        ]

        result = fetch_pr_feedback.fetch_active_review_feedback(7)

        self.assertEqual([item["body"] for item in result], ["first", "second"])
        self.assertIn("reviewCursor=R", run_json.call_args_list[1].args[0])

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_review_cursor_cycles_fail_closed(self, run_json, _slug):
        run_json.side_effect = [
            feedback_page([], review_has_next=True, review_cursor="A"),
            feedback_page([], review_has_next=True, review_cursor="B"),
            feedback_page([], review_has_next=True, review_cursor="A"),
        ]

        self.assertIsNone(fetch_pr_feedback.fetch_active_review_feedback(7))
        self.assertEqual(run_json.call_count, 3)

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_cursor_cycles_fail_closed(self, run_json, _slug):
        run_json.side_effect = [
            feedback_page([], True, "A"),
            feedback_page([], True, "B"),
            feedback_page([], True, "A"),
        ]

        self.assertIsNone(fetch_pr_feedback.fetch_active_review_feedback(7))
        self.assertEqual(run_json.call_count, 3)

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_head_change_between_pages_fails_closed(self, run_json, _slug):
        run_json.side_effect = [
            feedback_page([], True, "A", head="old-head"),
            feedback_page([], head="new-head"),
        ]

        self.assertIsNone(fetch_pr_feedback.fetch_active_review_feedback(7))
        self.assertEqual(run_json.call_count, 2)

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_partial_graphql_errors_fail_closed(self, run_json, _slug):
        run_json.return_value = feedback_page([], errors=[{"message": "partial"}])

        self.assertIsNone(fetch_pr_feedback.fetch_active_review_feedback(7))

    @patch.object(fetch_pr_feedback, "get_repo_slug", return_value="owner/repo")
    @patch.object(fetch_pr_feedback, "run_gh_json")
    def test_unknown_resolution_state_fails_closed(self, run_json, _slug):
        thread = review_thread()
        del thread["isResolved"]
        run_json.return_value = feedback_page([thread])

        self.assertIsNone(fetch_pr_feedback.fetch_active_review_feedback(7))


class RetiredCodingAgentReviewTests(unittest.TestCase):
    """#414: nothing here can claim, complete, or reap a coding-agent review.

    The behaviour these replace was real and tested: an optimistic reviewer:<id>
    claim, a reviewed-by:<id> completion stamp with an exact-head attestation
    comment, and a reaper that released both. Deleting the code is only half the
    job -- this asserts the surface cannot come back by accident, because a
    re-added claim path would silently start competing with CodeRabbit for the
    same PR.
    """

    RETIRED_ATTRIBUTES = (
        "claim_review", "complete_review", "release_review",
        "reap_stale_reviews", "review_claimant", "reviewed_by",
        "reviewer_labels", "_remove_reviewer_label", "_reviewer_label_for",
        "_reviewed_by_label_for", "_reviewer_family_label_for",
        "_review_head_attestation", "_stamp_reviewer_family",
        "_reviewed_head_for_completion", "REVIEWER_LABEL_PREFIX",
        "REVIEWED_BY_LABEL_PREFIX", "REVIEWER_FAMILY_LABEL_PREFIX",
        "AGENT_REVIEW_ATTESTATION_VERSION", "REVIEW_HEAD_ATTESTATION_VERSION",
        "AGENT_REVIEW_DISPOSITIONS", "AGENT_REVIEW_LABEL",
    )

    def test_no_review_claim_surface_remains_on_claim_issue(self):
        for name in self.RETIRED_ATTRIBUTES:
            self.assertFalse(hasattr(claim_issue, name),
                             f"claim_issue still exposes {name}")

    def test_the_helper_source_writes_no_reviewer_label(self):
        source = Path(claim_issue.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"reviewer:', source)
        self.assertNotIn('"reviewed-by:', source)
        self.assertNotIn("aru-agent-review", source)


class MergeClaimTests(unittest.TestCase):
    """Issue #43: optimistic merger:<id> claims."""

    def setUp(self):
        self.labels = []

    def _view(self, *_args, **_kwargs):
        return 0, "\n".join(self.labels), ""

    @patch.object(claim_issue, "CONFIRM_DELAY_S", 0)
    @patch.object(claim_issue, "READBACK_DELAY_S", 0)
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "run_cmd")
    def test_claim_merge_race_lowest_id_wins(self, run_cmd, _ensure):
        # First read: unclaimed. After write: both labels present; agent-b loses.
        sequence = [
            (0, "author:agent-a\nreviewed-by:peer\n", ""),
            (0, "", ""),  # add-label
            (0, "author:agent-a\nmerger:agent-a\nmerger:agent-b\nreviewed-by:peer\n", ""),
            (0, "", ""),  # remove loser
        ]
        run_cmd.side_effect = sequence
        self.assertEqual(claim_issue.claim_merge(7, "agent-b"), claim_issue.EXIT_CONFLICT)

    @patch.object(claim_issue, "CONFIRM_DELAY_S", 0)
    @patch.object(claim_issue, "READBACK_DELAY_S", 0)
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "run_cmd")
    def test_author_may_claim_merge_with_peer_review(self, run_cmd, _ensure):
        run_cmd.side_effect = [
            (0, "author:agent-a\nreviewed-by:peer\n", ""),
            (0, "", ""),
            (0, "author:agent-a\nmerger:agent-a\nreviewed-by:peer\n", ""),
            (0, "author:agent-a\nmerger:agent-a\nreviewed-by:peer\n", ""),
        ]
        self.assertEqual(claim_issue.claim_merge(7, "agent-a"), claim_issue.EXIT_OK)

    @patch.object(claim_issue, "CONFIRM_DELAY_S", 0)
    @patch.object(claim_issue, "READBACK_DELAY_S", 0)
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "run_cmd")
    def test_author_may_claim_merge_without_legacy_peer_label(self, run_cmd, _ensure):
        run_cmd.side_effect = [
            (0, "author:agent-a\n", ""), (0, "", ""),
            (0, "author:agent-a\nmerger:agent-a\n", ""),
            (0, "author:agent-a\nmerger:agent-a\n", ""),
        ]
        self.assertEqual(claim_issue.claim_merge(7, "agent-a"), claim_issue.EXIT_OK)

    @patch.object(claim_issue, "run_cmd")
    def test_stale_merge_claims_are_reaped(self, run_cmd):
        with patch.object(
            claim_issue,
            "fetch_paginated_gh_api",
            return_value=[{
                "event": "labeled",
                "label": {"name": "merger:stale"},
                "created_at": "2020-01-01T00:00:00Z",
            }],
        ):
            run_cmd.side_effect = [
                (0, '[{"number": 5, "labels": [{"name": "merger:stale"}]}]', ""),
                (0, "[]", ""),  # merged list
                (0, "", ""),
            ]
            self.assertEqual(claim_issue.reap_stale_merges(4), [5])

    def test_reaping_merges_is_off_by_default(self):
        self.assertEqual(claim_issue.reap_stale_merges(0), [])

    @patch.object(claim_issue, "run_cmd")
    def test_stale_merge_claims_on_merged_prs_are_reaped(self, run_cmd):
        with patch.object(
            claim_issue,
            "fetch_paginated_gh_api",
            return_value=[{
                "event": "labeled",
                "label": {"name": "merger:dead"},
                "created_at": "2020-01-01T00:00:00Z",
            }],
        ):
            run_cmd.side_effect = [
                # open list empty
                (0, "[]", ""),
                # merged list with stale claimant
                (0, '[{"number": 5, "labels": [{"name": "merger:dead"}], '
                    '"state": "MERGED", '
                    '"mergedAt": "2020-01-01T00:00:00Z"}]', ""),
                (0, "", ""),  # remove label
            ]
            self.assertEqual(claim_issue.reap_stale_merges(4), [5])


if __name__ == "__main__":
    unittest.main()
