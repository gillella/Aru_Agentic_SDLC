# line-ceiling: 627
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue
import agent_presence as ap
import fetch_pr_feedback


_PRESENCE_TEMPORARY = None
_PRESENCE_PATCHER = None


def setUpModule():
    global _PRESENCE_TEMPORARY, _PRESENCE_PATCHER
    _PRESENCE_TEMPORARY = tempfile.TemporaryDirectory()
    _PRESENCE_PATCHER = patch.object(
        ap,
        "DEFAULT_PRESENCE_PATH",
        Path(_PRESENCE_TEMPORARY.name) / "agent-presence.json",
    )
    _PRESENCE_PATCHER.start()


def tearDownModule():
    _PRESENCE_PATCHER.stop()
    _PRESENCE_TEMPORARY.cleanup()


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


class ReviewLabelParsingTests(unittest.TestCase):
    def test_reads_the_holder(self):
        self.assertEqual(claim_issue.reviewed_by(["reviewer:agent-3", "type:feat"]), "agent-3")

    def test_no_holder(self):
        self.assertIsNone(claim_issue.reviewed_by(["type:feat", "author:agent-1"]))

    def test_holders_are_sorted_so_the_tie_break_is_deterministic(self):
        held = claim_issue.reviewer_labels(["reviewer:zeta", "reviewer:alpha"])
        self.assertEqual(held, ["reviewer:alpha", "reviewer:zeta"])


class ClaimReviewTests(unittest.TestCase):
    def test_normal_coding_agent_review_is_still_refused(self):
        labels = ["review:coderabbit", "author:agent-1"]
        with patch.object(claim_issue, "_pr_labels", return_value=labels):
            self.assertEqual(claim_issue.claim_review(7, "agent-2"),
                             claim_issue.EXIT_CONFLICT)

    def test_preassigned_emergency_reviewer_resumes_without_mutation(self):
        labels = ["review:agent", "reviewer:agent-2", "author:agent-1"]
        with patch.object(claim_issue, "_pr_labels", return_value=labels), \
                patch.object(claim_issue, "run_cmd") as run:
            self.assertEqual(claim_issue.claim_review(7, "agent-2"),
                             claim_issue.EXIT_OK)
        run.assert_not_called()

    def test_wrong_or_self_reviewer_is_refused(self):
        for agent, labels in (
            ("agent-3", ["review:agent", "reviewer:agent-2", "author:agent-1"]),
            ("agent-1", ["review:agent", "reviewer:agent-1", "author:agent-1"]),
        ):
            with self.subTest(agent=agent), \
                    patch.object(claim_issue, "_pr_labels", return_value=labels):
                self.assertEqual(claim_issue.claim_review(7, agent),
                                 claim_issue.EXIT_CONFLICT)

    def test_complete_emergency_review_stamps_exact_head_contract(self):
        labels = ["review:agent", "reviewer:agent-2", "author:agent-1"]
        calls = []

        def run(cmd, **_kwargs):
            calls.append(cmd)
            return 0, "", ""

        with patch.object(claim_issue, "_pr_labels", return_value=labels), \
                patch.object(claim_issue, "_reviewed_head_for_completion",
                             return_value="a" * 40), \
                patch.object(claim_issue.merge_pr, "review_evidence", return_value={
                    "head_oid": "a" * 40, "agent_review_attestations": [],
                    "agent_review_marker_errors": 0,
                }), \
                patch.object(claim_issue, "ensure_label", return_value=True), \
                patch.object(claim_issue, "run_cmd", side_effect=run), \
                patch.object(claim_issue, "_remove_reviewer_label", return_value=True):
            code = claim_issue.complete_review(
                7, "agent-2", "openai", "no-findings")
        self.assertEqual(code, claim_issue.EXIT_OK)
        comment = next(cmd[-1] for cmd in calls if "comment" in cmd)
        for expected in ("aru-agent-review:v1", "agent-2", "openai",
                         "no-findings", "a" * 40):
            self.assertIn(expected, comment)

    def test_completion_retry_reuses_matching_marker(self):
        labels = ["review:agent", "reviewer:agent-2", "author:agent-1"]
        record = {"head": "a" * 40, "agent": "agent-2", "family": "openai",
                  "disposition": "no-findings"}
        calls = []
        with patch.object(claim_issue, "_pr_labels", return_value=labels), \
                patch.object(claim_issue, "_reviewed_head_for_completion",
                             return_value="a" * 40), \
                patch.object(claim_issue.merge_pr, "review_evidence", return_value={
                    "head_oid": "a" * 40, "agent_review_attestations": [record],
                    "agent_review_marker_errors": 0,
                }), \
                patch.object(claim_issue, "ensure_label", return_value=True), \
                patch.object(claim_issue, "run_cmd",
                             side_effect=lambda cmd, **_kwargs: (calls.append(cmd) or (0, "", ""))), \
                patch.object(claim_issue, "_remove_reviewer_label", return_value=True):
            code = claim_issue.complete_review(7, "agent-2", "openai", "no-findings")
        self.assertEqual(code, claim_issue.EXIT_OK)
        self.assertFalse(any("comment" in cmd for cmd in calls))

    def test_completion_requires_agent_authority_family_and_disposition(self):
        cases = (
            (["review:coderabbit", "reviewer:agent-2", "author:agent-1"],
             "openai", "no-findings"),
            (["review:agent", "reviewer:agent-2", "author:agent-1"],
             "", "no-findings"),
            (["review:agent", "reviewer:agent-2", "author:agent-1"],
             "openai", ""),
        )
        for labels, family, disposition in cases:
            with self.subTest(labels=labels, family=family, disposition=disposition), \
                    patch.object(claim_issue, "_pr_labels", return_value=labels):
                self.assertEqual(
                    claim_issue.complete_review(7, "agent-2", family, disposition),
                    claim_issue.EXIT_CONFLICT)


class ReleaseReviewTests(unittest.TestCase):
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "_pr_labels", return_value=["reviewer:agent-2"])
    def test_holder_can_release(self, _labels, _run):
        self.assertEqual(claim_issue.release_review(7, "agent-2"), claim_issue.EXIT_OK)

    @patch.object(claim_issue, "_pr_labels", return_value=["reviewer:agent-9"])
    def test_non_holder_cannot_release(self, _labels):
        self.assertEqual(claim_issue.release_review(7, "agent-2"), claim_issue.EXIT_CONFLICT)


class ReapStaleReviewsTests(unittest.TestCase):
    OLD = "2020-01-01T00:00:00Z"

    def setUp(self):
        timeline_patch = patch.object(
            claim_issue,
            "fetch_paginated_gh_api",
            return_value=[{
                "event": "labeled",
                "label": {"name": "reviewer:dead"},
                "created_at": self.OLD,
            }],
        )
        self.timeline = timeline_patch.start()
        self.addCleanup(timeline_patch.stop)

    def _prs(self, payload):
        import json
        return (0, json.dumps(payload), "")

    @patch.object(claim_issue, "run_cmd")
    def test_idle_unreviewed_claim_is_released(self, run_cmd):
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [{"name": "reviewer:dead"}],
                        "updatedAt": self.OLD, "reviews": []}]),
            (0, "", ""),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [5])

    @patch.object(claim_issue, "run_cmd")
    def test_a_recent_review_means_the_claim_is_spent_not_stale(self, run_cmd):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.timeline.return_value = [{
            "event": "labeled",
            "label": {"name": "reviewer:done"},
            "created_at": self.OLD,
        }]
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [{"name": "reviewer:done"}],
                        "updatedAt": self.OLD,
                        "reviews": [{"state": "APPROVED", "submittedAt": now}]}]),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    @patch.object(claim_issue, "run_cmd")
    def test_an_old_review_does_not_protect_a_later_abandoned_claim(self, run_cmd):
        # A PR reviewed once, then claimed again by an agent that crashed: any
        # historical review used to make the claim permanently unreapable, so
        # the reviewer:* label excluded the PR from the queue forever.
        self.timeline.return_value = [{
            "event": "labeled",
            "label": {"name": "reviewer:crashed"},
            "created_at": "2021-01-01T00:00:00Z",
        }]
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [{"name": "reviewer:crashed"}],
                        "updatedAt": self.OLD,
                        "reviews": [{"state": "APPROVED", "submittedAt": self.OLD}]}]),
            (0, "", ""),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [5])

    @patch.object(claim_issue, "run_cmd")
    def test_recent_claim_is_left_alone(self, run_cmd):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.timeline.return_value = [{
            "event": "labeled",
            "label": {"name": "reviewer:busy"},
            "created_at": now,
        }]
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [{"name": "reviewer:busy"}],
                        "updatedAt": now, "reviews": []}]),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    @patch.object(claim_issue, "run_cmd")
    def test_unclaimed_pr_is_ignored(self, run_cmd):
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [], "updatedAt": self.OLD, "reviews": []}]),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    def test_reaping_is_off_by_default(self):
        self.assertEqual(claim_issue.reap_stale_reviews(0), [])


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
