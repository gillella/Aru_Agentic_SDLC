# line-ceiling: 4860
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr
import cleanup_worktrees


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
        normalized = []
        for node in [] if nodes is None else nodes:
            if not isinstance(node, dict):
                normalized.append(node)
                continue
            node = dict(node)
            node.setdefault("body", "")
            if isinstance(node.get("author"), dict):
                node["author"] = dict(node["author"])
                node["author"].setdefault("__typename", "User")
            normalized.append(node)
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "reviews": {
                "nodes": normalized,
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

    @staticmethod
    def attestation_page(head="head123", nodes=None, has_next=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "comments": {
                "nodes": [] if nodes is None else nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }}}}

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_review_evidence_paginates_reviews_beyond_first_page(
        self, gh_json, _slug
    ):
        stale_reviews = [
            {
                "id": f"stale-{index}",
                "state": "COMMENTED",
                "submittedAt": f"2026-08-13T00:{index % 60:02d}:00Z",
                "author": {"login": f"reviewer-{index}"},
                "commit": {"oid": "old-head"},
            }
            for index in range(100)
        ]
        stale_reviews[0] = {
            "id": "advisory-current",
            "state": "COMMENTED",
            "submittedAt": "2026-08-14T00:00:00Z",
            "author": {"login": "coderabbitai[bot]"},
            "commit": {"oid": "head123"},
        }
        stale_reviews[1] = {
            "id": "pending-current",
            "state": "PENDING",
            "submittedAt": None,
            "author": {"login": "draft-reviewer"},
            "commit": {"oid": "head123"},
        }
        current_review = {
            "id": "current-substantive",
            "state": "COMMENTED",
            "submittedAt": "2026-08-15T00:00:00Z",
            "body": "Verdict: approved. The current diff is correct.",
            "author": {"login": "independent-agent"},
            "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(
                nodes=stale_reviews, has_next=True, cursor="review-page-2"
            ),
            self.review_page(nodes=[current_review]),
            self.attestation_page(),
            self.thread_page(),
        ]

        evidence = merge_pr.review_evidence(162)

        self.assertTrue(evidence["reviewed_head"])
        self.assertEqual(evidence["head_oid"], "head123")
        self.assertEqual(len(evidence["reviews"]), 101)
        self.assertIn("cursor=review-page-2", gh_json.call_args_list[1].args[0])
        self.assertEqual(gh_json.call_count, 4)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_page_two_changes_requested_blocks_latest_verdict_gate(
        self, gh_json, _slug
    ):
        first_page = [
            {
                "id": f"review-{index}",
                "state": "COMMENTED",
                "submittedAt": f"2026-08-14T00:{index % 60:02d}:00Z",
                "author": {"login": f"reviewer-{index}"},
                "commit": {"oid": "old-head"},
            }
            for index in range(100)
        ]
        blocker = {
            "id": "review-101",
            "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-08-15T00:00:00Z",
            "author": {"login": "independent-agent"},
            "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(
                nodes=first_page, has_next=True, cursor="review-page-2"
            ),
            self.review_page(nodes=[blocker]),
            self.attestation_page(),
            self.thread_page(),
        ]

        evidence = merge_pr.review_evidence(162)
        pr = labelled(
            "author:agent-1", "reviewed-by:agent-2", reviews=first_page
        )

        ok, message = merge_pr.check_reviews(pr, evidence)

        self.assertFalse(ok)
        self.assertIn("independent-agent requested changes", message)

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
    def test_review_nodes_require_unique_ids_and_valid_submission_times(
        self, gh_json, _slug
    ):
        valid = {
            "id": "review-1",
            "state": "APPROVED",
            "submittedAt": "2026-08-15T00:00:00Z",
            "author": None,
            "commit": {"oid": "head123"},
        }
        malformed_pages = [
            [dict(valid, id=None)],
            [dict(valid, id="")],
            [dict(valid, id=7)],
            [dict(valid, submittedAt=None)],
            [dict(valid, submittedAt="not-a-time")],
            [dict(valid, submittedAt="2026-08-15T00:00:00")],
            [dict(valid, author={})],
            [dict(valid, author={"name": "missing-login"})],
            [dict(valid, commit={})],
            [dict(valid, commit={"abbreviatedOid": "head123"})],
            [valid, dict(valid)],
        ]
        for nodes in malformed_pages:
            with self.subTest(nodes=nodes):
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [self.review_page(nodes=nodes)]
                self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_malformed_later_approval_cannot_clear_change_request(
        self, gh_json, _slug
    ):
        nodes = [
            {
                "id": "current-comment", "state": "COMMENTED",
                "submittedAt": "2026-08-15T00:00:00Z",
                "author": {"login": "peer"}, "commit": {"oid": "head123"},
            },
            {
                "id": "blocker", "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-15T01:00:00Z",
                "author": {"login": "bob"}, "commit": {"oid": "head123"},
            },
            {
                "id": "malformed-approval", "state": "APPROVED",
                "submittedAt": "2026-08-15T02:00:00Z",
                "author": {"login": "bob"}, "commit": {},
            },
        ]
        gh_json.return_value = self.review_page(nodes=nodes)

        self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_dismissed_review_does_not_attest_to_current_head(
        self, gh_json, _slug
    ):
        dismissed = {
            "id": "dismissed", "state": "DISMISSED",
            "submittedAt": "2026-08-15T00:00:00Z",
            "author": {"login": "peer"}, "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(nodes=[dismissed]), self.attestation_page(),
            self.thread_page()
        ]

        evidence = merge_pr.review_evidence(162)

        self.assertFalse(evidence["reviewed_head"])

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_author_thread_reply_and_graphql_bot_do_not_attest_to_head(
        self, gh_json, _slug
    ):
        reviews = [
            {
                "id": "author-thread-reply",
                "state": "COMMENTED",
                "submittedAt": "2026-08-16T15:18:51Z",
                "body": "",
                "author": {"login": "gillella", "__typename": "User"},
                "commit": {"oid": "head123"},
            },
            {
                "id": "coderabbit-current",
                "state": "COMMENTED",
                "submittedAt": "2026-08-16T15:19:08Z",
                "body": "Automated review summary",
                "author": {"login": "coderabbitai", "__typename": "Bot"},
                "commit": {"oid": "head123"},
            },
        ]
        gh_json.side_effect = [
            self.review_page(nodes=reviews), self.attestation_page(),
            self.thread_page()
        ]

        evidence = merge_pr.review_evidence(215)

        self.assertFalse(evidence["reviewed_head"])
        query = " ".join(gh_json.call_args_list[0].args[0])
        self.assertIn("__typename", query)
        self.assertIn("body", query)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_app_actor_never_attests_to_current_head(self, gh_json, _slug):
        app_review = {
            "id": "app-current",
            "state": "APPROVED",
            "submittedAt": "2026-08-16T15:19:08Z",
            "body": "Looks good",
            "author": {"login": "review-app", "__typename": "App"},
            "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(nodes=[app_review]), self.attestation_page(),
            self.thread_page()
        ]

        self.assertFalse(merge_pr.review_evidence(215)["reviewed_head"])

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_substantive_same_account_review_attests_to_current_head(
        self, gh_json, _slug
    ):
        head = "a" * 40
        peer_review = {
            "id": "peer-current",
            "state": "COMMENTED",
            "submittedAt": "2026-08-16T15:20:00Z",
            "body": "Verdict: approved. I verified the current diff and tests.",
            "author": {"login": "gillella", "__typename": "User"},
            "commit": {"oid": head},
        }
        gh_json.side_effect = [
            self.review_page(head=head, nodes=[peer_review]),
            self.attestation_page(nodes=[{
                "body": '<!-- aru-review-head:v1 {"agent":"cursor-1","head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->',
                "author": {"login": "gillella", "__typename": "User"},
            }], head=head),
            self.thread_page(head=head)
        ]

        evidence = merge_pr.review_evidence(215)

        self.assertTrue(evidence["reviewed_head"])
        pr = labelled("author:codex-1", "reviewed-by:cursor-1")
        ok, message = merge_pr.check_reviews(pr, evidence)
        self.assertTrue(ok)
        self.assertIn("Peer attribution: cursor-1", message)
        self.assertIn(f"current head {head[:12]}", message)
        self.assertIn("substantive independent review from gillella", message)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_malformed_attestation_comment_is_ignored(self, gh_json, _slug):
        head = "a" * 40
        peer_review = {
            "id": "peer-current",
            "state": "COMMENTED",
            "submittedAt": "2026-08-16T15:20:00Z",
            "body": "Verdict: approved. I verified the current diff and tests.",
            "author": {"login": "gillella", "__typename": "User"},
            "commit": {"oid": head},
        }
        valid_stamp = (
            '<!-- aru-review-head:v1 {"agent":"cursor-1",'
            '"head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->'
        )
        bot_stamp = (
            '<!-- aru-review-head:v1 {"agent":"bot",'
            '"head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->'
        )
        gh_json.side_effect = [
            self.review_page(head=head, nodes=[peer_review]),
            self.attestation_page(
                nodes=[
                    {
                        "body": '<!-- aru-review-head:v1 {"agent": -->',
                        "author": {"login": "malicious", "__typename": "User"},
                    },
                    {
                        "body": bot_stamp,
                        "author": {"login": "review-app", "__typename": "Bot"},
                    },
                    {
                        "body": valid_stamp,
                        "author": {"login": "gillella", "__typename": "User"},
                    },
                ],
                head=head,
            ),
            self.thread_page(head=head),
        ]

        evidence = merge_pr.review_evidence(215)

        self.assertEqual(
            evidence["review_attestations"],
            [{"agent": "cursor-1", "head": head, "github_login": "gillella"}],
        )

    def test_head_attestation_without_substantive_review_names_actual_gap(self):
        head = "a" * 40
        ok, message = merge_pr.check_reviews(
            labelled("author:codex-1", "reviewed-by:cursor-1"),
            {
                "unresolved": 0,
                "unfixed": 0,
                "withdrawn": 0,
                "reviewed_head": False,
                "head_oid": head,
                "review_attestations": [{"agent": "cursor-1", "head": head}],
            },
        )

        self.assertFalse(ok)
        self.assertIn("no substantive review targets that commit", message)
        self.assertNotIn("none is bound", message)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_unknown_actor_type_fails_closed(self, gh_json, _slug):
        review = {
            "id": "unknown-actor",
            "state": "COMMENTED",
            "submittedAt": "2026-08-16T15:20:00Z",
            "body": "Verdict: approved.",
            "author": {"login": "mystery", "__typename": None},
            "commit": {"oid": "head123"},
        }
        gh_json.return_value = self.review_page(nodes=[review])

        self.assertIsNone(merge_pr.review_evidence(215))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_stale_attribution_does_not_replace_current_head_review(
        self, gh_json, _slug
    ):
        stale_peer = {
            "id": "peer-stale",
            "state": "COMMENTED",
            "submittedAt": "2026-08-15T10:00:00Z",
            "body": "Verdict: approved on the old head.",
            "author": {"login": "gillella", "__typename": "User"},
            "commit": {"oid": "old-head"},
        }
        author_reply = {
            "id": "author-current",
            "state": "COMMENTED",
            "submittedAt": "2026-08-16T15:18:51Z",
            "body": "Verdict: approved. Author-authored substantive review.",
            "author": {"login": "gillella", "__typename": "User"},
            "commit": {"oid": "head123"},
        }
        gh_json.side_effect = [
            self.review_page(nodes=[stale_peer, author_reply]),
            self.attestation_page(), self.thread_page()
        ]

        evidence = merge_pr.review_evidence(215)
        self.assertTrue(evidence["reviewed_head"])
        ok, message = merge_pr.check_reviews(
            labelled("author:codex-1", "reviewed-by:cursor-1"), evidence
        )

        self.assertFalse(ok)
        self.assertIn("peer attribution exists for cursor-1", message.lower())
        self.assertIn("current head head123", message)
        self.assertIn("may be stale", message)

    def test_latest_verdict_uses_timestamp_not_page_order(self):
        reviews = [
            {
                "id": "newer",
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-15T02:00:00Z",
                "author": {"login": "independent-agent"},
            },
            {
                "id": "older",
                "state": "APPROVED",
                "submittedAt": "2026-08-15T01:00:00Z",
                "author": {"login": "independent-agent"},
            },
        ]
        evidence = {
            "reviews": reviews, "unresolved": 0, "unfixed": 0,
            "withdrawn": 0, "reviewed_head": True,
        }

        ok, message = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), evidence
        )

        self.assertFalse(ok)
        self.assertIn("independent-agent requested changes", message)

    def test_equal_verdict_timestamps_fail_closed(self):
        reviews = [
            {
                "id": "one", "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-08-15T02:00:00Z",
                "author": {"login": "independent-agent"},
            },
            {
                "id": "two", "state": "APPROVED",
                "submittedAt": "2026-08-15T02:00:00Z",
                "author": {"login": "independent-agent"},
            },
        ]
        evidence = {
            "reviews": reviews, "unresolved": 0, "unfixed": 0,
            "withdrawn": 0, "reviewed_head": True,
        }

        ok, message = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), evidence
        )

        self.assertFalse(ok)
        self.assertIn("unambiguous latest review verdict", message)

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

    def test_advisory_bot_status_does_not_gate_ci(self):
        pr = {"statusCheckRollup": [
            {"name": "verify", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "CodeRabbit", "state": "PENDING"},
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertTrue(ok, msg)

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

    # --- Superseded runs (#306) ---------------------------------------------
    # The rollup holds every run recorded against the head commit, so a check
    # that failed and was re-run green appears twice. Judging both kept the PR
    # red forever, with re-running powerless to clear it.

    @staticmethod
    def _run(name, conclusion, completed_at):
        return {"name": name, "status": "COMPLETED",
                "conclusion": conclusion, "completedAt": completed_at}

    def test_stale_failure_superseded_by_newer_success_is_green(self):
        pr = {"statusCheckRollup": [
            self._run("test", "FAILURE", "2026-08-20T01:00:00Z"),
            self._run("test", "SUCCESS", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertTrue(ok, msg)
        # Deduplicated: one check name, not two runs.
        self.assertIn("1 checks", msg)

    def test_array_order_does_not_decide_recency(self):
        # GitHub gives no ordering guarantee, so the newest-last arrangement
        # above must not be what makes the previous test pass.
        pr = {"statusCheckRollup": [
            self._run("test", "SUCCESS", "2026-08-20T02:00:00Z"),
            self._run("test", "FAILURE", "2026-08-20T01:00:00Z"),
        ]}
        self.assertTrue(merge_pr.check_ci(pr)[0])

    def test_stale_success_superseded_by_newer_failure_is_red(self):
        # Recency has to cut both ways, or the fix becomes a way to merge red.
        pr = {"statusCheckRollup": [
            self._run("test", "SUCCESS", "2026-08-20T01:00:00Z"),
            self._run("test", "FAILURE", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("test=failure", msg)

    def test_distinct_check_names_are_not_collapsed(self):
        pr = {"statusCheckRollup": [
            self._run("test", "SUCCESS", "2026-08-20T02:00:00Z"),
            self._run("lint", "FAILURE", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("lint=failure", msg)

    def test_started_at_is_used_when_a_run_has_not_completed(self):
        pr = {"statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE",
             "completedAt": "2026-08-20T01:00:00Z"},
            {"name": "test", "status": "IN_PROGRESS", "conclusion": None,
             "startedAt": "2026-08-20T02:00:00Z"},
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("not finished", msg)

    def test_contested_name_without_timestamps_fails_closed(self):
        # Two runs of one name and no way to order them: refuse rather than
        # assume either is current.
        pr = {"statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("undecidable", msg)

    def test_unparsable_timestamp_on_a_contested_name_fails_closed(self):
        pr = {"statusCheckRollup": [
            self._run("test", "FAILURE", "not-a-time"),
            self._run("test", "SUCCESS", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("undecidable", msg)

    def test_tied_runs_agreeing_through_different_fields_still_resolve(self):
        # A check run and a legacy commit status express one green outcome
        # through different fields. Comparing raw fields called that a
        # disagreement and blocked a PR check_ci itself treats as green.
        pr = {"statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS",
             "completedAt": "2026-08-20T02:00:00Z"},
            {"context": "test", "state": "SUCCESS",
             "completedAt": "2026-08-20T02:00:00Z"},
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertTrue(ok, msg)

    def test_tied_runs_disagreeing_fail_closed(self):
        pr = {"statusCheckRollup": [
            self._run("test", "SUCCESS", "2026-08-20T02:00:00Z"),
            self._run("test", "FAILURE", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("undecidable", msg)
        self.assertIn("tied", msg)

    def test_a_tie_never_resolves_by_array_order(self):
        both_orders = []
        for order in ((("SUCCESS",), ("FAILURE",)), (("FAILURE",), ("SUCCESS",))):
            pr = {"statusCheckRollup": [
                self._run("test", order[0][0], "2026-08-20T02:00:00Z"),
                self._run("test", order[1][0], "2026-08-20T02:00:00Z"),
            ]}
            both_orders.append(merge_pr.check_ci(pr)[0])
        self.assertEqual(both_orders, [False, False])

    def test_uncontested_name_without_a_timestamp_still_judged(self):
        # A single run needs no ordering; it is the run. Requiring a timestamp
        # here would break every ordinary pending check.
        pr = {"statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        self.assertTrue(merge_pr.check_ci(pr)[0])

    def test_allowlist_survives_deduplication(self):
        # The newest run being an unknown conclusion must still fail closed.
        pr = {"statusCheckRollup": [
            self._run("test", "SUCCESS", "2026-08-20T01:00:00Z"),
            self._run("test", "STARTUP_FAILURE", "2026-08-20T02:00:00Z"),
        ]}
        ok, msg = merge_pr.check_ci(pr)
        self.assertFalse(ok)
        self.assertIn("startup_failure", msg)

    def test_legacy_status_context_entries_group_by_context(self):
        pr = {"statusCheckRollup": [
            {"context": "ci/legacy", "state": "FAILURE",
             "completedAt": "2026-08-20T01:00:00Z"},
            {"context": "ci/legacy", "state": "SUCCESS",
             "completedAt": "2026-08-20T02:00:00Z"},
        ]}
        self.assertTrue(merge_pr.check_ci(pr)[0])

    def test_no_checks_at_all_blocks(self):
        # A PR with zero checks is unverified, not verified-by-default. This is
        # the exact hole that let a green-looking PR certify nothing.
        ok, msg = merge_pr.check_ci({"statusCheckRollup": []})
        self.assertFalse(ok)
        self.assertIn("No CI checks", msg)


class NoFastTrackEscapeHatchTests(unittest.TestCase):
    """The owner fast-track is gone, wiring and all (#321).

    Defaulting the flag off was not enough: a waiver that only needs one
    environment variable to re-enable is one export away from asserting a peer
    reviewed work that nobody reviewed, and nothing on the board would record it.
    """

    def test_no_relaxation_switch_survives_on_the_module(self):
        for name in ("RELAXED", "enable_relaxed"):
            self.assertFalse(hasattr(merge_pr, name),
                             f"merge_pr.{name} still exists")

    def test_the_environment_cannot_waive_the_review_gate(self):
        with patch.dict("os.environ", {"ARU_FAST_TRACK": "1"}):
            ok, msg = _gate(labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok, "a self-review merged under ARU_FAST_TRACK")
        self.assertIn("self-review", msg.lower())

    def test_the_environment_cannot_waive_test_coverage(self):
        pr = {"files": [{"path": "scripts/thing.py", "additions": 10, "deletions": 0}]}
        with patch.dict("os.environ", {"ARU_FAST_TRACK": "1"}):
            ok, msg = merge_pr.check_test_coverage(pr)
        self.assertFalse(ok)
        self.assertIn("test file", msg)

    def test_the_environment_cannot_waive_acceptance_criteria(self):
        body = "## Acceptance Criteria\n- [ ] not done yet\n"
        with patch.dict("os.environ", {"ARU_FAST_TRACK": "1"}):
            ok, _msg = merge_pr.check_acceptance(7, body)
        self.assertFalse(ok)

    def test_the_cli_no_longer_offers_a_relaxed_flag(self):
        source = Path(merge_pr.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--relaxed", source)
        self.assertNotIn("ARU_FAST_TRACK", source)


class ReviewGateTests(unittest.TestCase):
    def test_no_reviews_blocks(self):
        ok, msg = _gate({"reviews": []}, 0)
        self.assertFalse(ok)
        self.assertIn("No review", msg)

    def test_changes_requested_blocks(self):
        review = {
            "id": "blocking-review", "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "peer"},
        }
        ok, msg = _gate({"reviews": [review]}, 0)
        self.assertFalse(ok)
        self.assertIn("requested changes", msg)

    def test_advisory_bot_changes_requested_does_not_block_after_threads_resolve(self):
        reviews = [{
            "id": "advisory-change-request",
            "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-01-01T00:00:00Z",
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
        review = {
            "id": "approval", "state": "APPROVED",
            "submittedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "peer"},
        }
        ok, msg = _gate({"reviews": [review]}, None)
        self.assertFalse(ok)
        self.assertIn("review-thread state", msg)

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
    default = [{
        "id": "default-review",
        "state": "APPROVED",
        "submittedAt": "2026-01-01T00:00:00Z",
        "author": {"login": review_login},
    }]
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

    @patch.dict(os.environ, {"ARU_REVIEW_APP_LOGIN": "aru-reviewer[bot]"})
    def test_configured_review_app_approval_satisfies_the_gate(self):
        ok, msg = _gate(
            labelled("author:agent-1", review_login="aru-reviewer[bot]"), 0)
        self.assertTrue(ok)
        self.assertIn("aru-reviewer[bot]", msg)

    @patch.dict(os.environ, {"ARU_REVIEW_APP_LOGIN": "aru-reviewer[bot]"})
    def test_configured_review_app_bot_actor_at_current_head_counts(self):
        head = "a" * 40
        reviews = [{
            "id": "app-approve",
            "state": "APPROVED",
            "submittedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "aru-reviewer[bot]", "__typename": "Bot"},
            "commit": {"oid": head},
            "body": "",
        }]
        pr = labelled("author:agent-1", reviews=reviews)
        ok, msg = _gate(
            pr, 0,
            head_oid=head,
            reviews=reviews,
            review_attestations=[],
            reviewed_head=True,
        )
        self.assertTrue(ok)
        self.assertIn("aru-reviewer[bot]", msg)

    @patch.dict(os.environ, {"ARU_REVIEW_APP_LOGIN": "aru-reviewer[bot]"})
    def test_unconfigured_bot_stays_advisory_when_app_is_named(self):
        ok, msg = _gate(
            labelled("author:agent-1", review_login="coderabbitai[bot]"), 0)
        self.assertFalse(ok)
        self.assertIn("advisory", msg)

    @patch.dict(os.environ, {"ARU_REVIEW_APP_LOGIN": "aru-reviewer[bot]"})
    def test_same_account_self_review_still_fails_when_app_is_named(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg)

    @patch.dict(os.environ, {"ARU_REVIEW_APP_LOGIN": "aru-reviewer[bot]"})
    def test_reviewer_claim_still_blocks_configured_app_approval(self):
        ok, msg = _gate(
            labelled(
                "author:agent-1",
                "reviewer:agent-2",
                review_login="aru-reviewer[bot]",
            ),
            0,
        )
        self.assertFalse(ok)
        self.assertIn("reviewer:", msg)

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

    # --- Identity as the pair (id, family) (#307) ---------------------------
    # Agents authenticate as one GitHub user, so the labels are all that
    # distinguish them. Comparing the id alone cannot tell a genuine
    # cross-family reviewer apart from the author reviewing its own work.

    def test_same_id_same_family_is_still_a_self_review(self):
        ok, msg = _gate(labelled(
            "author:agent-1", "family:anthropic",
            "reviewed-by:agent-1", "reviewer-family:agent-1:anthropic"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())
        # Families were present, so no missing-label caveat is warranted.
        self.assertNotIn("cannot be ruled out", msg)

    def test_same_id_different_family_is_reported_as_an_id_collision(self):
        # Two agents answering to one id (#304). Not a peer review, and not
        # honestly a self-review either -- the namespace broke.
        ok, msg = _gate(labelled(
            "author:agent-1", "family:anthropic",
            "reviewed-by:agent-1", "reviewer-family:agent-1:google"), 0)
        self.assertFalse(ok)
        self.assertIn("sharing one id", msg)
        self.assertIn("anthropic", msg)
        self.assertIn("google", msg)
        self.assertNotIn("A self-review does not satisfy", msg)

    def test_id_collision_blocks_even_with_a_genuine_peer(self):
        # A broken id namespace is reportable regardless of who else reviewed:
        # no attribution carrying that id can be trusted.
        ok, msg = _gate(labelled(
            "author:agent-1", "family:anthropic",
            "reviewed-by:agent-1", "reviewer-family:agent-1:google",
            "reviewed-by:agent-9"), 0)
        self.assertFalse(ok)
        self.assertIn("sharing one id", msg)

    def test_distinct_id_review_passes_without_any_family_labels(self):
        # Unchanged from today: family is consulted only where the ids collide,
        # so PRs predating family stamping keep merging.
        ok, msg = _gate(labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_missing_family_on_a_same_id_review_names_what_is_missing(self):
        ok, msg = _gate(labelled("author:agent-1", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())
        self.assertIn("family:<family> on the PR", msg)
        self.assertIn("reviewer-family:agent-1:<family>", msg)

    def test_missing_reviewer_family_alone_is_named(self):
        ok, msg = _gate(labelled(
            "author:agent-1", "family:anthropic", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("reviewer-family:agent-1:<family>", msg)
        self.assertNotIn("family:<family> on the PR", msg)

    def test_missing_family_does_not_block_a_genuine_peer(self):
        ok, _ = _gate(labelled(
            "author:agent-1", "reviewed-by:agent-1", "reviewed-by:agent-3"), 0)
        self.assertTrue(ok)

    def test_classifier_partitions_reviewers(self):
        pr = labelled("author:a1", "family:anthropic",
                      "reviewed-by:a1", "reviewer-family:a1:google",
                      "reviewed-by:a2")
        peers, collisions, unresolved = merge_pr.classify_reviewers(
            pr, ["a1", "a2"], "a1")
        self.assertEqual(peers, ["a2"])
        self.assertEqual(collisions, [("a1", "anthropic", "google")])
        self.assertEqual(unresolved, [])

    def test_reviewer_families_ignores_malformed_labels(self):
        pr = labelled("reviewer-family:a1:google", "reviewer-family:nofamily",
                      "reviewer-family:")
        self.assertEqual(merge_pr.reviewer_families(pr), {"a1": ["google"]})

    def test_conflicting_family_stamps_are_reported_as_ambiguity(self):
        # Two families for one id is evidence of the reissue defect; letting the
        # last label win would describe the wrong situation entirely.
        pr = labelled("author:agent-1", "family:anthropic",
                      "reviewed-by:agent-1",
                      "reviewer-family:agent-1:anthropic",
                      "reviewer-family:agent-1:google")
        _peers, collisions, _unresolved = merge_pr.classify_reviewers(
            pr, ["agent-1"], "agent-1")
        self.assertEqual(len(collisions), 1)
        self.assertIn("ambiguous", collisions[0][1])

    def test_two_author_family_labels_are_reported_as_ambiguity(self):
        pr = labelled("author:agent-1", "family:anthropic", "family:google",
                      "reviewed-by:agent-1",
                      "reviewer-family:agent-1:anthropic")
        _peers, collisions, _unresolved = merge_pr.classify_reviewers(
            pr, ["agent-1"], "agent-1")
        self.assertEqual(len(collisions), 1)
        self.assertIn("anthropic, google", collisions[0][1])

    def test_ambiguous_families_still_block_the_merge(self):
        ok, msg = _gate(labelled(
            "author:agent-1", "family:anthropic",
            "reviewed-by:agent-1",
            "reviewer-family:agent-1:anthropic",
            "reviewer-family:agent-1:google"), 0)
        self.assertFalse(ok)
        self.assertIn("ambiguous", msg)

    def test_reviewer_family_label_is_not_read_as_the_author_family(self):
        # family: and reviewer-family: must not be confused by prefix matching.
        pr = labelled("reviewer-family:a1:google")
        self.assertEqual(merge_pr.label_values(pr, merge_pr.FAMILY_LABEL), [])

    def test_an_empty_author_label_does_not_make_every_reviewer_a_peer(self):
        # A bare `author:` label parses to "", which no reviewer id equals, so
        # a self-review read as an independent peer and satisfied the gate.
        ok, msg = _gate(labelled("author:", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("author:", msg)

    def test_a_whitespace_only_author_label_is_also_rejected(self):
        ok, _msg = _gate(labelled("author:   ", "reviewed-by:agent-1"), 0)
        self.assertFalse(ok)

    def test_an_empty_reviewed_by_label_is_not_a_peer(self):
        ok, _msg = _gate(labelled("author:agent-1", "reviewed-by:"), 0)
        self.assertFalse(ok)

    def test_author_label_whitespace_is_trimmed_not_treated_as_distinct(self):
        # " agent-1" and "agent-1" are one identity, not an ambiguity.
        ok, _msg = _gate(labelled("author: agent-1", "author:agent-1",
                                  "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)

    def test_two_different_author_labels_fail_closed(self):
        # Concurrent adoption can leave two author: stamps. Picking one by
        # position would decide the peer comparison arbitrarily.
        ok, msg = _gate(labelled("author:agent-1", "author:agent-2",
                                 "reviewed-by:agent-3"), 0)
        self.assertFalse(ok)
        self.assertIn("cannot be established", msg)
        self.assertIn("agent-1, agent-2", msg)

    def test_a_duplicated_identical_author_label_is_not_ambiguous(self):
        ok, _msg = _gate(labelled("author:agent-1", "author:agent-1",
                                  "reviewed-by:agent-3"), 0)
        self.assertTrue(ok)

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
        approval = {
            "id": "self-approval", "state": "APPROVED",
            "submittedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "gillella"},
        }
        ok, msg = _gate(
            labelled("author:solo", "reviewed-by:solo",
                     reviews=[approval, {"state": "COMMENTED"}]), 0)
        self.assertFalse(ok)
        self.assertIn("self-review", msg.lower())


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


def _pr(state="CLEAN", mergeable="MERGEABLE", base="main", head="deadbeef"):
    return {"mergeStateStatus": state, "mergeable": mergeable,
            "baseRefName": base, "headRefOid": head}


def _behind(n):
    """Resolver stub returning a fixed behind_by, so no test touches the network."""
    return lambda base, head: n


def _paths(ours, theirs, base="main", head="deadbeef"):
    """Changed-path resolver stub for both sides of the fork.

    Asserts the three-dot direction as a side effect: `ours` is only served for
    ``compare(base, head)`` and `theirs` only for the swapped ``compare(head,
    base)``, so a transposed call fails loudly instead of silently comparing a
    side against itself. ``None`` models "could not determine".
    """
    def resolve(first, second):
        if (first, second) == (base, head):
            return None if ours is None else set(ours)
        if (first, second) == (head, base):
            return None if theirs is None else set(theirs)
        raise AssertionError(f"unexpected compare {first}...{second}")
    return resolve


# The moment the base advance landed, for the freshness gate. Every check below
# is placed either side of it, so "stale" and "fresh" are never ambiguous.
_ADVANCE_AT = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)
_BEFORE_ADVANCE = "2026-08-22T11:59:59Z"
_AFTER_ADVANCE = "2026-08-22T12:00:01Z"


def _advance(when=_ADVANCE_AT, base="main", head="deadbeef"):
    """Base-advance-time resolver stub, asserting the direction it is asked for.

    ``when`` is the resolved timestamp, or None for "could not determine".
    """
    def resolve(first, second):
        if (first, second) != (base, head):
            raise AssertionError(f"unexpected advance lookup {first}...{second}")
        return when
    return resolve


def _check_run(name, started, conclusion="SUCCESS"):
    """One completed check run in the shape `gh pr view` emits.

    The completion stamp mirrors the start stamp so recency ordering follows
    the timeline a reader expects; tests that need the two to disagree set
    ``completedAt`` themselves.
    """
    run = {"name": name, "status": "COMPLETED", "conclusion": conclusion}
    if started is not None:
        run["startedAt"] = started
        run["completedAt"] = started
    return run


def _ci_pr(runs, **kwargs):
    pr = dict(_pr(**kwargs))
    pr["statusCheckRollup"] = list(runs)
    return pr


def _boom(*_args):
    raise AssertionError("resolver must not be consulted on this path")


class RebaseGateTests(unittest.TestCase):
    def test_conflicts_block(self):
        self.assertFalse(merge_pr.check_rebased({"mergeStateStatus": "DIRTY"}, _behind(0))[0])
        self.assertFalse(merge_pr.check_rebased({"mergeable": "CONFLICTING"}, _behind(0))[0])

    def test_clean_and_current_passes(self):
        self.assertTrue(merge_pr.check_rebased(_pr(), _behind(0))[0])

    def test_behind_with_overlapping_changes_blocks(self):
        """The case the gate exists for: the base moved a file this branch moved."""
        ok, msg = merge_pr.check_rebased(
            _pr(state="CLEAN"), _behind(3),
            _paths(["scripts/merge_pr.py"], ["scripts/merge_pr.py", "docs/x.md"]))
        self.assertFalse(ok)
        self.assertIn("3 commits behind", msg)
        self.assertIn("scripts/merge_pr.py", msg)
        self.assertIn("Rebase", msg)

    def test_behind_with_disjoint_changes_passes(self):
        """Issue #369: a rebase here would only destroy the review attestation.

        Disjointness alone is no longer enough (#371), so the CI this branch
        carries has to postdate the advance as well.
        """
        ok, msg = merge_pr.check_rebased(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], state="CLEAN"), _behind(3),
            _paths(["scripts/merge_pr.py"], ["docs/releases.md"]), _advance())
        self.assertTrue(ok)
        self.assertIn("3 commits behind", msg)
        self.assertIn("disjoint", msg)

    def test_unstable_and_behind_with_overlap_blocks(self):
        """Regression: hermes-trading-automation PR #17, 18 behind, reported current."""
        ok, msg = merge_pr.check_rebased(
            _pr(state="UNSTABLE"), _behind(18), _paths(["a.py"], ["a.py"]))
        self.assertFalse(ok)
        self.assertIn("18 commits behind", msg)

    def test_behind_status_is_no_longer_a_fast_path_rejection(self):
        """A BEHIND branch still merges when it does not overlap the base."""
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], state="BEHIND")
        ok, _ = merge_pr.check_rebased(pr, _behind(2), _paths(["a.py"], ["b.py"]),
                                       _advance())
        self.assertTrue(ok)

    def test_one_commit_behind_is_singular(self):
        msg = merge_pr.check_rebased(_pr(), _behind(1), _paths(["a.py"], ["a.py"]))[1]
        self.assertIn("1 commit behind", msg)

    def test_rename_on_our_side_cannot_hide_an_overlap(self):
        """We renamed a.py -> b.py; the base edited a.py. Not disjoint."""
        ok, msg = merge_pr.check_rebased(
            _pr(), _behind(2), _paths(["a.py", "b.py"], ["a.py"]))
        self.assertFalse(ok)
        self.assertIn("a.py", msg)

    def test_rename_on_the_base_side_cannot_hide_an_overlap(self):
        ok, msg = merge_pr.check_rebased(
            _pr(), _behind(2), _paths(["a.py"], ["a.py", "renamed.py"]))
        self.assertFalse(ok)
        self.assertIn("a.py", msg)

    def test_overlap_message_truncates_long_lists(self):
        shared = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        msg = merge_pr.check_rebased(_pr(), _behind(2), _paths(shared, shared))[1]
        self.assertIn("+2 more", msg)

    def test_unknown_our_side_fails_closed(self):
        ok, msg = merge_pr.check_rebased(_pr(), _behind(3), _paths(None, ["a.py"]))
        self.assertFalse(ok)
        self.assertIn("unverified", msg)
        self.assertIn("Rebase", msg)

    def test_unknown_base_side_fails_closed(self):
        ok, msg = merge_pr.check_rebased(_pr(), _behind(3), _paths(["a.py"], None))
        self.assertFalse(ok)
        self.assertIn("unverified", msg)

    def test_paths_resolver_exception_fails_closed(self):
        def boom(first, second):
            raise RuntimeError("compare exploded")
        ok, msg = merge_pr.check_rebased(_pr(), _behind(3), boom)
        self.assertFalse(ok)
        self.assertIn("compare exploded", msg)
        self.assertIn("Rebase", msg)

    def test_unknown_ancestry_fails_closed(self):
        ok, msg = merge_pr.check_rebased(_pr(), lambda base, head: None)
        self.assertFalse(ok)
        self.assertIn("unverified ancestry", msg)

    def test_resolver_exception_fails_closed(self):
        def boom(base, head):
            raise RuntimeError("api exploded")
        ok, msg = merge_pr.check_rebased(_pr(), boom)
        self.assertFalse(ok)
        self.assertIn("unverified ancestry", msg)
        self.assertIn("api exploded", msg)

    def test_conflict_rejections_do_not_consult_any_resolver(self):
        """Conflicts are decided before ancestry, and cost no API call."""
        calls = []

        def spy(base, head):
            calls.append((base, head))
            return 0

        def paths_spy(first, second):
            calls.append((first, second))
            return set()

        for pr in ({"mergeStateStatus": "DIRTY"}, {"mergeable": "CONFLICTING"}):
            self.assertFalse(merge_pr.check_rebased(pr, spy, paths_spy)[0])
        self.assertEqual(calls, [])

    def test_current_branch_does_not_consult_the_paths_resolver(self):
        """Zero-behind short-circuits: no reason to pay for two compare calls."""
        def paths_spy(first, second):
            raise AssertionError("must not be called when the branch is current")

        self.assertTrue(merge_pr.check_rebased(_pr(), _behind(0), paths_spy)[0])

    def test_resolver_receives_base_and_head(self):
        seen = []
        merge_pr.check_rebased(_pr(base="release/v2", head="abc123"),
                               lambda base, head: seen.append((base, head)) or 0)
        self.assertEqual(seen, [("release/v2", "abc123")])


class StaleCIAgainstBaseAdvanceTests(unittest.TestCase):
    """Issue #371 / PR #370 review: disjoint paths do not make old CI fresh.

    The false-pass being closed, exactly as reported: PR A goes green; an
    unrelated PR B merges to main; A is now behind; GitHub triggers no new
    pull_request run, so A still advertises the same green rollup; the path
    sets are disjoint; the final base-OID lock sees a base that did not move
    *during* the merge command. Before this gate, A merged on checks that were
    computed against the superseded base.
    """

    def _check(self, pr, behind=2, ours=("a.py",), theirs=("b.py",), when=_ADVANCE_AT):
        return merge_pr.check_rebased(
            pr, _behind(behind), _paths(list(ours), list(theirs)), _advance(when))

    def test_stale_pre_advance_ci_does_not_pass(self):
        ok, msg = self._check(_ci_pr([_check_run("Lint", _BEFORE_ADVANCE)]))
        self.assertFalse(ok)
        self.assertIn("Lint", msg)
        self.assertIn("superseded base", msg)

    def test_fresh_post_advance_ci_passes(self):
        ok, msg = self._check(_ci_pr([
            _check_run("Lint", _AFTER_ADVANCE), _check_run("Secret Scan", _AFTER_ADVANCE)]))
        self.assertTrue(ok)
        self.assertIn("disjoint", msg)
        self.assertIn("after the base advance", msg)

    def test_one_stale_check_among_fresh_ones_blocks(self):
        """Freshness is a property of the whole rollup, not of its best member."""
        ok, msg = self._check(_ci_pr([
            _check_run("Lint", _AFTER_ADVANCE), _check_run("Secret Scan", _BEFORE_ADVANCE)]))
        self.assertFalse(ok)
        self.assertIn("Secret Scan", msg)

    def test_check_started_in_the_same_second_as_the_advance_fails_closed(self):
        """One-second API granularity cannot order these, so it must not try."""
        ok, msg = self._check(_ci_pr([_check_run("Lint", "2026-08-22T12:00:00Z")]))
        self.assertFalse(ok)
        self.assertIn("superseded base", msg)

    def test_stale_message_asks_for_a_re_run_and_not_a_rebase(self):
        """#371: rebasing would destroy the head-bound review attestation."""
        msg = self._check(_ci_pr([_check_run("Lint", _BEFORE_ADVANCE)]))[1]
        self.assertIn("Re-run this PR's CI", msg)
        self.assertIn("Do not rebase", msg)
        self.assertNotIn("Rebase on main", msg)

    def test_missing_start_time_fails_closed(self):
        """completedAt is not a substitute: a run can finish after it read."""
        ok, msg = self._check(_ci_pr([_check_run("Lint", None)]))
        self.assertFalse(ok)
        self.assertIn("no usable start time", msg)

    def test_malformed_start_time_fails_closed(self):
        ok, msg = self._check(_ci_pr([_check_run("Lint", "yesterday-ish")]))
        self.assertFalse(ok)
        self.assertIn("no usable start time", msg)

    def test_unresolvable_base_advance_fails_closed(self):
        ok, msg = self._check(_ci_pr([_check_run("Lint", _AFTER_ADVANCE)]), when=None)
        self.assertFalse(ok)
        self.assertIn("could not be determined", msg)

    def test_advance_resolver_exception_fails_closed(self):
        def explode(_first, _second):
            raise RuntimeError("compare exploded")
        ok, msg = merge_pr.check_rebased(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]), _behind(2),
            _paths(["a.py"], ["b.py"]), explode)
        self.assertFalse(ok)
        self.assertIn("compare exploded", msg)
        self.assertIn("Do not rebase", msg)

    def test_empty_rollup_fails_closed(self):
        """A behind branch with no checks has proved nothing about any base."""
        ok, msg = self._check(_ci_pr([]))
        self.assertFalse(ok)
        self.assertIn("no required check", msg)

    def test_a_rollup_of_only_advisory_bots_fails_closed(self):
        ok, msg = self._check(_ci_pr([
            {"context": "CodeRabbit", "state": "SUCCESS", "startedAt": _AFTER_ADVANCE}]))
        self.assertFalse(ok)
        self.assertIn("no required check", msg)

    def test_a_stale_advisory_bot_does_not_block_fresh_required_checks(self):
        """Advisory bots are not build checks here, so they prove nothing either."""
        ok, _ = self._check(_ci_pr([
            _check_run("Lint", _AFTER_ADVANCE),
            {"context": "CodeRabbit", "state": "SUCCESS", "startedAt": _BEFORE_ADVANCE}]))
        self.assertTrue(ok)

    def test_a_legacy_status_context_is_a_required_check(self):
        """Freshness must not be dodged by reporting through the older API."""
        ok, msg = self._check(_ci_pr([
            {"context": "buildkite", "state": "SUCCESS", "startedAt": _BEFORE_ADVANCE}]))
        self.assertFalse(ok)
        self.assertIn("buildkite", msg)

    def test_undecidable_recency_fails_closed(self):
        """Two runs of one name that cannot be ordered have no knowable age."""
        ok, msg = self._check(_ci_pr([
            _check_run("Lint", None), _check_run("Lint", None)]))
        self.assertFalse(ok)
        self.assertIn("undecidable", msg)

    def test_a_superseded_stale_run_does_not_block_a_fresh_current_one(self):
        """#306 semantics hold: the newest run of a name is the one judged."""
        ok, _ = self._check(_ci_pr([
            _check_run("Lint", _BEFORE_ADVANCE), _check_run("Lint", _AFTER_ADVANCE)]))
        self.assertTrue(ok)

    def test_a_fresh_run_superseded_by_a_stale_one_blocks(self):
        """The mirror of the case above, so neither is passing by array order."""
        stale = _check_run("Lint", _BEFORE_ADVANCE)
        stale["completedAt"] = "2026-08-22T23:00:00Z"
        fresh = _check_run("Lint", _AFTER_ADVANCE)
        fresh["completedAt"] = "2026-08-22T12:00:02Z"
        ok, msg = self._check(_ci_pr([fresh, stale]))
        self.assertFalse(ok)
        self.assertIn("superseded base", msg)

    def test_zero_behind_never_consults_the_freshness_resolver(self):
        """Compatibility: a current branch has no advance to be stale against."""
        ok, msg = merge_pr.check_rebased(
            _ci_pr([_check_run("Lint", _BEFORE_ADVANCE)]), _behind(0), _boom, _boom)
        self.assertTrue(ok)
        self.assertIn("current with the base", msg)

    def test_overlap_is_decided_before_freshness_and_costs_no_lookup(self):
        ok, msg = merge_pr.check_rebased(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]), _behind(2),
            _paths(["a.py"], ["a.py"]), _boom)
        self.assertFalse(ok)
        self.assertIn("a.py", msg)

    def test_conflicts_are_decided_before_freshness_and_cost_no_lookup(self):
        for pr in ({"mergeStateStatus": "DIRTY"}, {"mergeable": "CONFLICTING"}):
            self.assertFalse(merge_pr.check_rebased(pr, _boom, _boom, _boom)[0])

    def test_unverifiable_ancestry_is_decided_before_freshness(self):
        ok, msg = merge_pr.check_rebased(
            _ci_pr([]), lambda _b, _h: None, _boom, _boom)
        self.assertFalse(ok)
        self.assertIn("unverified ancestry", msg)

    def test_evaluate_dod_threads_the_freshness_resolver(self):
        """The gate the picker and --dry-run read is the same one, not a copy."""
        seen = []

        def record(first, second):
            seen.append((first, second))
            return _ADVANCE_AT

        pr = _ci_pr([_check_run("Lint", _BEFORE_ADVANCE)], state="CLEAN")
        pr["body"] = "Closes #369"
        with patch.object(merge_pr, "_compare_paths", _paths(["a.py"], ["b.py"])):
            _, gates = merge_pr.evaluate_dod(
                pr, {369: "- [x] done\n"}, evidence={},
                behind_resolver=_behind(2), advance_resolver=record)
        rebased = [g for g in gates if g[0] == "rebased"][0]
        self.assertFalse(rebased[1])
        self.assertIn("superseded base", rebased[2])
        self.assertEqual(seen, [("main", "deadbeef")])


class BaseAdvanceTimeTests(unittest.TestCase):
    """`_base_advance_time` must answer None for anything it cannot read."""

    @staticmethod
    def _commit(committer, author=None):
        return {"commit": {"committer": {"date": committer},
                           "author": {"date": author or committer}}}

    @staticmethod
    def _compare(commits):
        """A compare payload whose `total_commits` matches what it carries.

        Every honest fixture is built here, so a test that gets None back gets
        it for the reason that test names rather than for a missing count.
        """
        return {"commits": commits, "total_commits": len(commits)}

    def _resolve(self, payload):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json", return_value=payload):
            return merge_pr._base_advance_time("main", "abc")

    def test_missing_refs_return_none(self):
        self.assertIsNone(merge_pr._base_advance_time("", "abc"))
        self.assertIsNone(merge_pr._base_advance_time("main", ""))

    def test_missing_slug_returns_none(self):
        with patch.object(merge_pr, "get_repo_slug", return_value=None):
            self.assertIsNone(merge_pr._base_advance_time("main", "abc"))

    def test_the_compare_direction_is_head_to_base(self):
        """Swapped arguments would time this branch instead of the advance."""
        seen = []

        def spy(args):
            seen.append(args[-1])
            return self._compare([self._commit("2026-08-22T12:00:00Z")])

        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json", side_effect=spy):
            merge_pr._base_advance_time("main", "abc")
        self.assertEqual(seen, ["repos/o/r/compare/abc...main"])

    def test_newest_commit_wins(self):
        got = self._resolve(self._compare([
            self._commit("2026-08-22T01:00:00Z"),
            self._commit("2026-08-22T09:00:00Z"),
            self._commit("2026-08-22T04:00:00Z"),
        ]))
        self.assertEqual(got, datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc))

    def test_a_later_author_date_raises_the_bar_rather_than_lowering_it(self):
        """A fabricated stamp can only demand fresher CI, never accept staler."""
        got = self._resolve(self._compare([
            self._commit("2026-08-22T01:00:00Z", author="2026-08-22T20:00:00Z")]))
        self.assertEqual(got, datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc))

    def test_naive_timestamps_are_read_as_utc(self):
        got = self._resolve(self._compare([self._commit("2026-08-22T09:00:00")]))
        self.assertEqual(got, datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc))

    def test_malformed_payloads_return_none(self):
        """Each fixture carries an honest count, so only the named flaw refuses."""
        for payload in (
            None, [], "nope", {}, {"commits": None}, {"commits": {}},
            # A branch reported behind must have commits on the other side; an
            # empty list is contradictory data, not a clean bill of health.
            self._compare([]),
            self._compare(["not-a-dict"]),
            self._compare([{}]),
            self._compare([{"commit": "not-a-dict"}]),
            self._compare([{"commit": {"author": {"date": "2026-08-22T09:00:00Z"}}}]),
            self._compare([{"commit": {"committer": "x",
                                       "author": {"date": "2026-08-22T09:00:00Z"}}}]),
            self._compare([{"commit": {"committer": {"date": None},
                                       "author": {"date": "2026-08-22T09:00:00Z"}}}]),
            self._compare([{"commit": {"committer": {"date": "not-a-date"},
                                       "author": {"date": "2026-08-22T09:00:00Z"}}}]),
            # One unreadable commit poisons the whole answer: the advance's age
            # is the maximum, so a skipped member could hide the newest stamp.
            self._compare([{"commit": {"committer": {"date": "2026-08-22T09:00:00Z"},
                                       "author": {"date": "2026-08-22T09:00:00Z"}}},
                           {"commit": {"committer": {"date": "not-a-date"},
                                       "author": {"date": "2026-08-22T09:00:00Z"}}}]),
        ):
            self.assertIsNone(self._resolve(payload), f"{payload!r}")

    def test_a_truncated_commit_array_is_refused(self):
        """The compare is unpaginated, so GitHub caps `commits` at 250.

        `total_commits` keeps counting past the cap. Commits come back in
        chronological order, so truncation drops the *newest* ones -- exactly
        the stamp this helper exists to find. Reading a short array would move
        the freshness bar backwards and admit CI that predates the advance.
        """
        self.assertIsNone(self._resolve({
            "commits": [self._commit("2026-08-22T01:00:00Z")] * 250,
            "total_commits": 251}))

    def test_an_unreadable_commit_count_returns_none(self):
        """Absent, mistyped, or disagreeing counts all fail closed.

        `1.0` and `True` both compare equal to a one-commit array, so the
        isinstance guards -- not the equality -- are what refuse them.
        """
        commits = [self._commit("2026-08-22T09:00:00Z")]
        for total in (None, "1", 1.0, True, False, -1, 0, 2):
            self.assertIsNone(
                self._resolve({"commits": list(commits), "total_commits": total}),
                f"total_commits={total!r}")
        # Absent entirely, not merely unreadable.
        self.assertIsNone(self._resolve({"commits": list(commits)}))

    def test_a_matching_commit_count_is_accepted(self):
        got = self._resolve({
            "commits": [self._commit("2026-08-22T01:00:00Z"),
                        self._commit("2026-08-22T09:00:00Z")],
            "total_commits": 2})
        self.assertEqual(got, datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc))


class CheckStartTimeTests(unittest.TestCase):
    def test_only_the_start_stamp_is_read(self):
        """A completion stamp cannot bound what a run checked out."""
        self.assertIsNone(merge_pr._check_start_time({"completedAt": _AFTER_ADVANCE}))

    def test_start_stamp_is_parsed(self):
        self.assertEqual(
            merge_pr._check_start_time({"startedAt": "2026-08-22T12:00:01Z",
                                        "completedAt": "2026-08-22T13:00:00Z"}),
            datetime(2026, 8, 22, 12, 0, 1, tzinfo=timezone.utc))

    def test_unparseable_start_stamp_is_none(self):
        self.assertIsNone(merge_pr._check_start_time({"startedAt": "soon"}))

    def test_ordering_still_prefers_the_completion_stamp(self):
        """`_check_time` is unchanged; the two helpers answer different questions."""
        self.assertEqual(
            merge_pr._check_time({"startedAt": "2026-08-22T12:00:01Z",
                                  "completedAt": "2026-08-22T13:00:00Z"}),
            datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc))


class BehindByTests(unittest.TestCase):
    def test_missing_refs_return_none(self):
        self.assertIsNone(merge_pr._behind_by("", "abc"))
        self.assertIsNone(merge_pr._behind_by("main", ""))

    def test_valid_payload_returns_count(self):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json", return_value={"behind_by": 7}):
            self.assertEqual(merge_pr._behind_by("main", "abc"), 7)

    def test_malformed_payloads_return_none(self):
        for payload in (None, [], {}, {"behind_by": "3"}, {"behind_by": True},
                        {"behind_by": -1}, {"behind_by": None}):
            with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
                 patch.object(merge_pr, "_gh_json", return_value=payload):
                self.assertIsNone(merge_pr._behind_by("main", "abc"), f"{payload!r}")

    def test_missing_slug_returns_none(self):
        with patch.object(merge_pr, "get_repo_slug", return_value=None):
            self.assertIsNone(merge_pr._behind_by("main", "abc"))


class ComparePathsTests(unittest.TestCase):
    @staticmethod
    def _payload(files):
        return {"files": files}

    def _resolve(self, payload):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json", return_value=payload):
            return merge_pr._compare_paths("main", "abc")

    def test_missing_refs_return_none(self):
        self.assertIsNone(merge_pr._compare_paths("", "abc"))
        self.assertIsNone(merge_pr._compare_paths("main", ""))

    def test_missing_slug_returns_none(self):
        with patch.object(merge_pr, "get_repo_slug", return_value=None):
            self.assertIsNone(merge_pr._compare_paths("main", "abc"))

    def test_filenames_are_collected(self):
        got = self._resolve(self._payload([{"filename": "a.py"}, {"filename": "b.py"}]))
        self.assertEqual(got, {"a.py", "b.py"})

    def test_rename_contributes_both_paths(self):
        got = self._resolve(self._payload(
            [{"filename": "new.py", "previous_filename": "old.py"}]))
        self.assertEqual(got, {"new.py", "old.py"})

    def test_response_at_the_file_cap_is_unverifiable(self):
        """Truncation can only ever make two change sets look more disjoint."""
        files = [{"filename": f"f{i}.py"} for i in range(merge_pr.COMPARE_FILE_LIMIT)]
        self.assertIsNone(self._resolve(self._payload(files)))

    def test_one_under_the_cap_still_resolves(self):
        files = [{"filename": f"f{i}.py"} for i in range(merge_pr.COMPARE_FILE_LIMIT - 1)]
        self.assertEqual(len(self._resolve(self._payload(files))),
                         merge_pr.COMPARE_FILE_LIMIT - 1)

    def test_empty_file_list_is_unverifiable_not_a_clean_pass(self):
        self.assertIsNone(self._resolve(self._payload([])))

    def test_malformed_payloads_return_none(self):
        for payload in (None, [], {}, {"files": None}, {"files": "a.py"},
                        {"files": [{"filename": ""}]}, {"files": [{"filename": 3}]},
                        {"files": [{"no_filename": "a.py"}]}, {"files": ["a.py"]}):
            self.assertIsNone(self._resolve(payload), f"{payload!r}")


class OverlapWithBaseAdvanceTests(unittest.TestCase):
    def test_queries_both_directions_of_the_fork(self):
        seen = []

        def resolve(first, second):
            seen.append((first, second))
            return {"a.py"}

        merge_pr._overlap_with_base_advance(
            {"baseRefName": "main", "headRefOid": "abc"}, resolve)
        self.assertEqual(seen, [("main", "abc"), ("abc", "main")])

    def test_disjoint_sets_report_no_overlap(self):
        got = merge_pr._overlap_with_base_advance(
            {"baseRefName": "main", "headRefOid": "abc"},
            _paths(["a.py"], ["b.py"], base="main", head="abc"))
        self.assertEqual(got, [])

    def test_overlap_is_sorted(self):
        got = merge_pr._overlap_with_base_advance(
            {"baseRefName": "main", "headRefOid": "abc"},
            _paths(["z.py", "a.py"], ["a.py", "z.py"], base="main", head="abc"))
        self.assertEqual(got, ["a.py", "z.py"])

    def test_either_side_unknown_is_unknown(self):
        for ours, theirs in ((None, ["a.py"]), (["a.py"], None)):
            got = merge_pr._overlap_with_base_advance(
                {"baseRefName": "main", "headRefOid": "abc"},
                _paths(ours, theirs, base="main", head="abc"))
            self.assertIsNone(got)

    def test_base_side_is_not_queried_when_our_side_is_unknown(self):
        calls = []

        def resolve(first, second):
            calls.append((first, second))
            return None

        merge_pr._overlap_with_base_advance(
            {"baseRefName": "main", "headRefOid": "abc"}, resolve)
        self.assertEqual(calls, [("main", "abc")])


class SizeGateTests(unittest.TestCase):
    def test_oversized_diff_blocks_without_waiver(self):
        ok, msg = merge_pr.check_size({"additions": 800, "deletions": 100, "body": ""})
        self.assertFalse(ok)
        self.assertIn("over the 400-line limit", msg)
        self.assertIn("size-waiver", msg)

    def test_oversized_diff_passes_with_explicit_waiver(self):
        ok, msg = merge_pr.check_size({
            "additions": 800,
            "deletions": 100,
            "body": "## Notes\nsize-waiver: generated compatibility fixtures\n",
        })
        self.assertTrue(ok)
        self.assertIn("generated compatibility fixtures", msg)

    def test_oversized_diff_blocks_on_empty_waiver(self):
        ok, _ = merge_pr.check_size({
            "additions": 401,
            "deletions": 0,
            "body": "size-waiver:   \n",
        })
        self.assertFalse(ok)

    def test_small_diff_passes(self):
        self.assertTrue(merge_pr.check_size({"additions": 10, "deletions": 2})[0])


class ReviewRoundGateTests(unittest.TestCase):
    def _pr(self, *states, body="Closes #98"):
        reviews = []
        for idx, state in enumerate(states):
            entry = {"state": state, "author": {"login": f"r{idx}"}}
            if state == "COMMENTED":
                entry["body"] = "**Blocking:** fix this"
            reviews.append(entry)
        return {"number": 42, "body": body, "reviews": reviews}

    def test_rounds_are_visible_and_never_block(self):
        ok, msg = merge_pr.check_review_rounds(self._pr("CHANGES_REQUESTED", "CHANGES_REQUESTED"))
        self.assertTrue(ok)
        self.assertIn("2 review round(s)", msg)
        self.assertNotIn("escalate", msg.lower())

    def test_threshold_crossing_still_passes_with_split_guidance(self):
        ok, msg = merge_pr.check_review_rounds(
            self._pr("CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED")
        )
        self.assertTrue(ok)
        self.assertIn("3 review round(s)", msg)
        self.assertIn("--emit-review-split", msg)
        self.assertIn("never creates a human gate", msg)

    def test_commented_blocking_body_counts_as_a_round(self):
        self.assertEqual(
            merge_pr.count_review_rounds(self._pr("COMMENTED", "APPROVED")),
            1,
        )

    def test_split_plan_follow_ups_carry_depends_on(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        plan = merge_pr.build_review_round_split_plan(
            pr, findings=["scripts/merge_pr.py: too broad"],
        )
        self.assertTrue(plan["crossed"])
        self.assertEqual(plan["threshold"], 3)
        self.assertIn(merge_pr.REVIEW_ROUND_SPLIT_MARKER, plan["comment"])
        self.assertIn("does **not** create a human approval gate", plan["comment"])
        self.assertTrue(plan["follow_ups"])
        for item in plan["follow_ups"]:
            # depends-on targets the linked Closes issue (picker semantics), not the PR.
            self.assertIn("depends-on: #98", item["body"])
            self.assertIn(
                merge_pr._split_item_marker(42, item["idx"]),
                item["body"],
            )
            self.assertNotIn("depends-on: #42", item["body"])

    def test_flatten_comment_pages_handles_slurp_and_flat(self):
        flat = merge_pr._flatten_comment_pages([
            {"body": "a"}, {"body": "b"},
        ])
        self.assertEqual(flat, ["a", "b"])
        slurped = merge_pr._flatten_comment_pages([
            [{"body": "p1a"}, {"body": "p1b"}],
            [{"body": "p2"}],
        ])
        self.assertEqual(slurped, ["p1a", "p1b", "p2"])

    def test_pr_comments_bodies_uses_paginate_slurp(self):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json") as gh_json:
            gh_json.return_value = [[{"body": "one"}], [{"body": "two"}]]
            bodies = merge_pr._pr_comments_bodies(42)
        self.assertEqual(bodies, ["one", "two"])
        args = gh_json.call_args[0][0]
        self.assertIn("--paginate", args)
        self.assertIn("--slurp", args)

    def test_emit_is_idempotent_when_marker_already_present(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        with patch.object(
            merge_pr, "_pr_comments_bodies",
            return_value=[f"{merge_pr.REVIEW_ROUND_SPLIT_MARKER}\nalready done"],
        ), patch.object(merge_pr, "run_cmd") as run_cmd, \
             patch.object(merge_pr, "_find_existing_split_follow_ups") as find_existing:
            result = merge_pr.emit_review_round_split(
                pr, findings=["scripts/x.py: leftover"], apply=True,
            )
        self.assertFalse(result["emitted"])
        self.assertEqual(result["reason"], "already emitted")
        run_cmd.assert_not_called()
        find_existing.assert_not_called()

    def test_emit_attaches_new_issues_to_board(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        with patch.object(merge_pr, "_pr_comments_bodies", return_value=["prior"]), \
             patch.object(merge_pr, "_find_existing_split_follow_ups", return_value={}), \
             patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "run_cmd") as run_cmd, \
             patch.object(merge_pr, "update_status", return_value=True) as update_status:
            run_cmd.side_effect = [
                (0, "https://github.com/o/r/issues/501\n", ""),
                (0, "", ""),
            ]
            result = merge_pr.emit_review_round_split(
                pr, findings=["scripts/x.py: leftover"], apply=True,
            )
        self.assertTrue(result["emitted"])
        self.assertEqual(result["reason"], "posted")
        update_status.assert_called_once_with(501, "Backlog", require_board=True)
        create_args = run_cmd.call_args_list[0][0][0]
        self.assertEqual(create_args[:3], ["gh", "issue", "create"])
        self.assertIn(
            merge_pr._split_item_marker(42, 1),
            create_args[create_args.index("--body") + 1],
        )

    def test_emit_fails_closed_when_board_attach_fails(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        with patch.object(merge_pr, "_pr_comments_bodies", return_value=[]), \
             patch.object(merge_pr, "_find_existing_split_follow_ups", return_value={}), \
             patch.object(
                 merge_pr, "run_cmd",
                 return_value=(0, "https://github.com/o/r/issues/502\n", ""),
             ), \
             patch.object(merge_pr, "update_status", return_value=False):
            result = merge_pr.emit_review_round_split(
                pr, findings=["scripts/x.py: leftover"], apply=True,
            )
        self.assertFalse(result["emitted"])
        self.assertIn("board attach failed", result["reason"])

    def test_emit_reuses_partial_creates_on_retry(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        existing = {
            1: {"number": 510, "url": "https://github.com/o/r/issues/510"},
        }
        with patch.object(merge_pr, "_pr_comments_bodies", return_value=[]), \
             patch.object(
                 merge_pr, "_find_existing_split_follow_ups", return_value=existing,
             ), \
             patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "run_cmd") as run_cmd, \
             patch.object(merge_pr, "update_status", return_value=True) as update_status:
            run_cmd.side_effect = [
                (0, "https://github.com/o/r/issues/511\n", ""),  # create idx 2
                (0, "", ""),  # PR comment
            ]
            result = merge_pr.emit_review_round_split(
                pr,
                findings=["finding one", "finding two"],
                apply=True,
            )
        self.assertTrue(result["emitted"])
        self.assertEqual(
            result["created_issues"],
            [
                "https://github.com/o/r/issues/510",
                "https://github.com/o/r/issues/511",
            ],
        )
        # Reused #510 + newly created #511 both get board attach.
        self.assertEqual(
            [c.args for c in update_status.call_args_list],
            [(510, "Backlog",), (511, "Backlog",)],
        )
        for call in update_status.call_args_list:
            self.assertTrue(call.kwargs.get("require_board"))
        # Only one issue create (idx 2); idx 1 was reused.
        create_calls = [
            c for c in run_cmd.call_args_list
            if c[0][0][:3] == ["gh", "issue", "create"]
        ]
        self.assertEqual(len(create_calls), 1)

    def test_emit_retries_comment_after_issues_already_filed(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        existing = {
            1: {"number": 520, "url": "https://github.com/o/r/issues/520"},
        }
        with patch.object(merge_pr, "_pr_comments_bodies", return_value=[]), \
             patch.object(
                 merge_pr, "_find_existing_split_follow_ups", return_value=existing,
             ), \
             patch.object(merge_pr, "run_cmd") as run_cmd, \
             patch.object(merge_pr, "update_status", return_value=True):
            run_cmd.side_effect = [
                (0, "", ""),  # PR comment succeeds on retry
            ]
            result = merge_pr.emit_review_round_split(
                pr, findings=["only one"], apply=True,
            )
        self.assertTrue(result["emitted"])
        self.assertEqual(
            result["created_issues"],
            ["https://github.com/o/r/issues/520"],
        )
        create_calls = [
            c for c in run_cmd.call_args_list
            if c[0][0][:3] == ["gh", "issue", "create"]
        ]
        self.assertEqual(create_calls, [])
        comment_args = run_cmd.call_args_list[0][0][0]
        self.assertEqual(comment_args[:3], ["gh", "pr", "comment"])
        body_idx = comment_args.index("--body") + 1
        self.assertIn(merge_pr.REVIEW_ROUND_SPLIT_MARKER, comment_args[body_idx])

    def test_evaluate_dod_includes_review_rounds_soft_gate(self):
        pr = self._pr(
            "CHANGES_REQUESTED", "CHANGES_REQUESTED", "CHANGES_REQUESTED",
            body="Closes #98\n",
        )
        with patch.object(merge_pr, "check_open", return_value=(True, "open")), \
             patch.object(merge_pr, "check_issue_link", return_value=(True, "linked")), \
             patch.object(merge_pr, "check_verification", return_value=(True, "ok")), \
             patch.object(merge_pr, "check_ci", return_value=(True, "green")), \
             patch.object(merge_pr, "check_reviews", return_value=(True, "reviewed")), \
             patch.object(merge_pr, "check_rebased", return_value=(True, "current")), \
             patch.object(merge_pr, "check_size", return_value=(True, "small")), \
             patch.object(merge_pr, "check_test_coverage", return_value=(True, "tests")), \
             patch.object(merge_pr, "check_acceptance", return_value=(True, "accept")), \
             patch.object(merge_pr, "linked_issues", return_value=[98]):
            ok, gates = merge_pr.evaluate_dod(pr, {98: "- [x] done\n"}, evidence={})
        names = [name for name, _, _ in gates]
        self.assertIn("review rounds", names)
        rounds_gate = next(g for g in gates if g[0] == "review rounds")
        self.assertTrue(rounds_gate[1])
        self.assertIn("--emit-review-split", rounds_gate[2])
        self.assertTrue(ok)

    def test_evaluate_dod_includes_spec_sync_gate(self):
        pr = {"body": "Closes #242\n"}
        with patch.object(merge_pr, "check_open", return_value=(True, "open")), \
             patch.object(merge_pr, "check_issue_link", return_value=(True, "linked")), \
             patch.object(merge_pr, "check_verification", return_value=(True, "ok")), \
             patch.object(merge_pr, "check_ci", return_value=(True, "green")), \
             patch.object(merge_pr, "check_reviews", return_value=(True, "reviewed")), \
             patch.object(merge_pr, "check_rebased", return_value=(True, "current")), \
             patch.object(merge_pr, "check_size", return_value=(True, "small")), \
             patch.object(merge_pr, "check_test_coverage", return_value=(True, "tests")), \
             patch.object(merge_pr, "check_spec_sync", return_value=(True, "spec sync ok")), \
             patch.object(merge_pr, "check_review_rounds", return_value=(True, "ok")), \
             patch.object(merge_pr, "check_acceptance", return_value=(True, "accept")), \
             patch.object(merge_pr, "linked_issues", return_value=[242]):
            ok, gates = merge_pr.evaluate_dod(pr, {242: "- [x] done\n"}, evidence={})
        names = [name for name, _, _ in gates]
        self.assertIn("spec-sync", names)
        sync_gate = next(g for g in gates if g[0] == "spec-sync")
        self.assertTrue(sync_gate[1])
        self.assertEqual(sync_gate[2], "spec sync ok")
        self.assertTrue(ok)


class TestCoverageGateTests(unittest.TestCase):
    def test_truncated_changed_file_list_fails_closed(self):
        ok, msg = merge_pr.check_test_coverage({
            "changedFiles": 101,
            "files": [
                {"path": f"docs/note-{index}.md", "additions": 1, "deletions": 0}
                for index in range(100)
            ],
        })
        self.assertFalse(ok)
        self.assertIn("truncated (100 of 101)", msg)

    def test_script_change_requires_changed_test(self):
        ok, msg = merge_pr.check_test_coverage({
            "files": [{"path": "scripts/merge_pr.py", "additions": 10, "deletions": 1}],
        })
        self.assertFalse(ok)
        self.assertIn("require a changed", msg)

    def test_source_change_passes_with_changed_test(self):
        ok, msg = merge_pr.check_test_coverage({
            "files": [
                {"path": "src/service.py", "additions": 8, "deletions": 2},
                {"path": "tests/test_service.py", "additions": 12, "deletions": 0},
            ],
        })
        self.assertTrue(ok)
        self.assertIn("1 test file", msg)

    def test_deleted_test_does_not_satisfy_gate(self):
        ok, _ = merge_pr.check_test_coverage({
            "files": [
                {"path": "scripts/tool.py", "additions": 2, "deletions": 0},
                {"path": "tests/test_tool.py", "additions": 0, "deletions": 20},
            ],
        })
        self.assertFalse(ok)

    def test_non_production_change_does_not_require_test(self):
        ok, msg = merge_pr.check_test_coverage({
            "files": [{"path": "docs/guide.md", "additions": 4, "deletions": 0}],
        })
        self.assertTrue(ok)
        self.assertIn("No src/ or scripts/ changes", msg)


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
        "baseRefOid": "base-sha",
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

    @patch.object(merge_pr, "post_human_intervention", return_value=False)
    @patch.object(merge_pr.time, "sleep")
    @patch.object(merge_pr, "clear_merger_claims")
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "review clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(False, "delete failed"))
    @patch.object(merge_pr, "cleanup_local_branch", return_value=(True, "local retained"))
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
    @patch.object(merge_pr, "check_spec_sync", return_value=(True, "ok"))
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
    def test_successful_merge_with_branch_delete_failure_is_resumable(
        self, fetch, _json, _sync, _threads, execute, _root, _chdir, _prune, _local,
        _remote, _close, _done, _issue_claim, _review_claim, merger_claim,
        sleep, intervention,
    ):
        fetch.return_value = {
            "number": 9,
            "title": "open",
            "body": "Closes #7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "fix/issue-7-example",
            "headRefOid": "gated-sha",
            "baseRefOid": "base-sha",
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [{
                "id": "peer-approval", "state": "APPROVED",
                "submittedAt": "2026-01-01T00:00:00Z",
                "author": {"login": "peer"},
            }],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch("builtins.print") as printer:
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_ERROR)

        execute.assert_called_once_with(9, fetch.return_value, "merge")
        self.assertEqual(_close.call_count, 4)
        self.assertEqual(_done.call_count, 4)
        self.assertEqual(_issue_claim.call_count, 4)
        self.assertEqual(_review_claim.call_count, 4)
        merger_claim.assert_not_called()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15, 45])
        intervention.assert_called_once()
        self.assertEqual(len(intervention.call_args.args[5]), 4)
        output = " ".join(str(call.args[0]) for call in printer.call_args_list if call.args)
        self.assertIn("could not be fully recorded", output)
        self.assertNotIn("evidence was recorded", output)

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
    @patch.object(merge_pr, "check_spec_sync", return_value=(True, "ok"))
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
    def test_default_merge_method_is_merge(
        self, fetch, _json, _sync, _threads, execute, _root, closeout
    ):
        fetch.return_value = {
            "number": 9,
            "title": "open",
            "body": "Closes #7",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "fix/issue-7-example",
            "headRefOid": "gated-sha",
            "baseRefOid": "base-sha",
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [{
                "id": "peer-approval", "state": "APPROVED",
                "submittedAt": "2026-01-01T00:00:00Z",
                "author": {"login": "peer"},
            }],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_OK)

        execute.assert_called_once_with(9, fetch.return_value, "merge")


class SerializedMergeExecutionTests(unittest.TestCase):
    def test_repository_lock_is_shared_across_callers_for_the_same_slug(self):
        with tempfile.TemporaryDirectory() as state_home, \
             patch.dict(os.environ, {"XDG_STATE_HOME": state_home}), \
             patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"):
            with merge_pr.repository_merge_lock() as first:
                with merge_pr.repository_merge_lock() as second:
                    self.assertTrue(first[0])
                    self.assertFalse(second[0])
                    self.assertIn("another merge", second[1])

    def _open_pr(self, base="base-a"):
        return {
            "number": 9,
            "title": "open",
            "body": "Closes #7",
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": "gated-sha",
            "baseRefOid": base,
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        }

    def test_base_move_inside_serialized_window_blocks_server_merge(self):
        initial = self._open_pr("base-a")
        fresh = self._open_pr("base-b")
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", side_effect=[initial, fresh]), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "execute_merge") as execute:
            code = merge_pr.main()

        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute.assert_not_called()

    def _behind_pr(self, started):
        pr = self._open_pr()
        pr.update({
            "baseRefName": "main",
            "headRefOid": "gated-sha",
            "mergeStateStatus": "BEHIND",
            "statusCheckRollup": [_check_run("Lint", started)],
        })
        return pr

    def _final_window(self, started):
        """Drive main() to the serialized re-read with a behind, disjoint PR."""
        pr = self._behind_pr(started)
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", side_effect=[pr, dict(pr)]), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "_behind_by", new=lambda _base, _head: 2), \
             patch.object(merge_pr, "_compare_paths", _paths(["a.py"], ["b.py"],
                                                             head="gated-sha")), \
             patch.object(merge_pr, "_base_advance_time",
                          _advance(head="gated-sha")), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "execute_merge",
                          return_value=(merged_pr(), "merged")) as execute:
            return merge_pr.main(), execute

    def test_stale_ci_blocks_at_the_final_reread_even_when_the_base_held_still(self):
        """The reported false-pass: the base moved *before* the merge command.

        The base-OID lock only proves nothing moved during this command, so it
        cannot see an advance that already happened. The final rebased check is
        what has to catch it, and it runs on the freshly re-read PR.
        """
        code, execute = self._final_window(_BEFORE_ADVANCE)
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute.assert_not_called()

    def test_fresh_ci_still_merges_through_the_final_reread(self):
        code, execute = self._final_window(_AFTER_ADVANCE)
        self.assertEqual(code, merge_pr.EXIT_OK)
        execute.assert_called_once()

    def test_unavailable_repository_lock_fails_closed(self):
        initial = self._open_pr()
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", return_value=initial), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "repository_merge_lock", return_value=nullcontext(
                 (False, "another merge is executing for this repository")
             )), \
             patch.object(merge_pr, "execute_merge") as execute:
            code = merge_pr.main()

        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute.assert_not_called()


class CloseOutRecoveryTests(unittest.TestCase):
    def _run(self, failing):
        outcomes = {
            "prune_worktree": (True, "worktree ok"),
            "cleanup_local_branch": (True, "local ok"),
            "delete_remote_branch": (True, "remote ok"),
            "ensure_issue_closed": (True, "closed"),
            "reconcile_issue_done": (True, "done"),
            "clear_issue_claims": (True, "issue claim clear"),
            "clear_review_claims": (True, "review claim clear"),
            "clear_merger_claims": (True, "merger claim clear"),
            "sweep_leftovers": (True, "janitor ok"),
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
        mocks["sweep_leftovers"].assert_called_once_with("/repo", retain_merger_pr=9)

    def test_worktree_failure_does_not_skip_branch_or_board_cleanup(self):
        ok, mocks = self._run("prune_worktree")
        self.assertFalse(ok)
        mocks["cleanup_local_branch"].assert_called_once()
        mocks["delete_remote_branch"].assert_called_once()
        mocks["reconcile_issue_done"].assert_called_once_with(7)

    def test_board_failure_does_not_skip_claim_cleanup(self):
        ok, mocks = self._run("reconcile_issue_done")
        self.assertFalse(ok)
        mocks["clear_issue_claims"].assert_called_once_with(7)
        mocks["clear_review_claims"].assert_called_once_with(9)
        mocks["clear_merger_claims"].assert_not_called()

    def test_closeout_invokes_janitor_even_when_a_prior_step_fails(self):
        ok, mocks = self._run("prune_worktree")
        self.assertFalse(ok)
        mocks["sweep_leftovers"].assert_called_once_with("/repo", retain_merger_pr=9)

    def test_local_branch_deletion_failure_does_not_skip_remaining_closeout(self):
        ok, mocks = self._run("cleanup_local_branch")
        self.assertFalse(ok)
        mocks["delete_remote_branch"].assert_called_once()
        mocks["reconcile_issue_done"].assert_called_once_with(7)
        mocks["clear_merger_claims"].assert_not_called()

    @patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor ok"))
    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear"))
    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "review clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(True, "remote"))
    @patch.object(merge_pr, "cleanup_local_branch", return_value=(True, "local"))
    @patch.object(merge_pr, "prune_worktree", return_value=(True, "worktree"))
    @patch.object(merge_pr.os, "chdir")
    def test_changes_to_surviving_root_before_pruning_caller_worktree(
        self, chdir, prune, _local, _remote, _close, _done, _issue, _review, _merger, _janitor
    ):
        def after_chdir(*_args):
            chdir.assert_called_once_with("/repo")
            return True, "worktree"

        prune.side_effect = after_chdir
        self.assertTrue(merge_pr.run_closeout(merged_pr(), [7], "/repo"))
        chdir.assert_called_once_with("/repo")


class HumanInterventionTests(unittest.TestCase):
    def test_retry_policy_exhausts_before_returning_failure_evidence(self):
        def fail(_pr, _issues, _root, failures=None):
            failures.append("remote branch: delete failed")
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail) as closeout, \
             patch.object(merge_pr.time, "sleep") as sleep:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertFalse(ok)
        self.assertEqual(closeout.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15, 45])
        self.assertEqual(
            attempts,
            [["remote branch: delete failed"]] * 4,
        )

    def test_harmless_orphan_local_branch_releases_claim_after_bounded_retries(self):
        """#343: a local-branch-only leftover must never deadlock the claim.

        Remote branch, issues, board, and review/janitor scaffolding all
        succeed on every attempt; only the local branch step keeps failing.
        Once the bounded retry budget is exhausted, the merger claim is
        released directly with a non-blocking warning instead of routing to
        human-intervention evidence.
        """
        def fail(_pr, _issues, _root, failures=None):
            failures.append(
                "local branch: Orphan local branch fix/x deletion failed after unattached validation: locked"
            )
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail) as closeout, \
             patch.object(merge_pr.time, "sleep") as sleep, \
             patch.object(
                 merge_pr, "clear_merger_claims", return_value=(True, "cleared"),
             ) as merger:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertTrue(ok)
        self.assertEqual(closeout.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15, 45])
        merger.assert_called_once_with(9)
        self.assertEqual(
            attempts,
            [[
                "local branch: Orphan local branch fix/x deletion failed after unattached validation: locked"
            ]] * 4,
        )

    def test_local_branch_fail_safe_does_not_mask_other_unresolved_failures(self):
        """A mixed failure (e.g. worktree still dirty) must keep retrying/HITL.

        The fail-safe only fires when the local branch step is the *sole*
        recorded failure -- never when a real, unresolved lifecycle failure
        (like a still-attached worktree) is also present (#343 decision
        boundary: never silently wave through a genuinely unmerged/live
        worktree situation).
        """
        def fail(_pr, _issues, _root, failures=None):
            failures.append("worktree: still attached, dirty")
            failures.append(
                "local branch: Orphan local branch fix/x deletion failed after unattached validation: locked"
            )
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail) as closeout, \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "clear_merger_claims") as merger:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertFalse(ok)
        self.assertEqual(closeout.call_count, 4)
        merger.assert_not_called()
        self.assertEqual(len(attempts), 4)

    def test_local_branch_fail_safe_reports_failure_when_claim_release_fails(self):
        """If clearing the claim itself fails, the fail-safe must not claim success."""
        def fail(_pr, _issues, _root, failures=None):
            failures.append(
                "local branch: Orphan local branch fix/x deletion failed after unattached validation: locked"
            )
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail), \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(
                 merge_pr, "clear_merger_claims",
                 return_value=(False, "gh api rate limited"),
             ) as merger:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertFalse(ok)
        merger.assert_called_once_with(9)
        self.assertEqual(len(attempts), 4)

    def test_bounded_fallback_does_not_clear_claim_when_local_branch_is_attached(self):
        """Bounded fallback must NOT release merger claim if the failure is worktree attachment."""
        def fail(_pr, _issues, _root, failures=None):
            failures.append("local branch: Retained local branch fix/x; branch is attached to a worktree.")
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail) as closeout, \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "clear_merger_claims") as merger:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertFalse(ok)
        self.assertEqual(closeout.call_count, 4)
        merger.assert_not_called()
        self.assertEqual(len(attempts), 4)

    def test_bounded_fallback_does_not_clear_claim_when_local_branch_has_lease_mismatch(self):
        """Bounded fallback must NOT release merger claim if the failure is a lease mismatch."""
        def fail(_pr, _issues, _root, failures=None):
            failures.append(
                "local branch: Local branch fix/x lease failed on gated-sha (now at new-sha): lock failed; ref retained."
            )
            return False

        with patch.object(merge_pr, "run_closeout", side_effect=fail) as closeout, \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "clear_merger_claims") as merger:
            ok, attempts = merge_pr.run_closeout_with_retries(
                merged_pr(), [7], "/repo"
            )

        self.assertFalse(ok)
        self.assertEqual(closeout.call_count, 4)
        merger.assert_not_called()
        self.assertEqual(len(attempts), 4)

    def test_comment_contains_command_artifacts_attempts_and_one_action(self):
        pr = merged_pr()
        pr["labels"] = [{"name": "merger:codex-root"}]
        failures = [
            ["remote branch: delete failed"],
            ["remote branch: delete still failed"],
        ]
        with patch.object(merge_pr, "fetch_pr", return_value=pr), \
             patch.object(
                 merge_pr, "surviving_worktree",
                 return_value="/repo/.worktrees/fix-7 at gated-sha",
             ):
            body = merge_pr.human_intervention_body(
                pr, "/repo", "gated-sha", "merge-sha", failures,
                "python3 merge_pr.py --pr 9",
            )

        self.assertTrue(body.startswith("## Human intervention required"))
        self.assertIn("command: `python3 merge_pr.py --pr 9`", body)
        self.assertIn("exit code: `1`", body)
        self.assertIn("gated head SHA: `gated-sha`", body)
        self.assertIn("merged SHA: `merge-sha`", body)
        self.assertIn("worktree: /repo/.worktrees/fix-7 at gated-sha", body)
        self.assertIn("remaining merger claims: `merger:codex-root`", body)
        self.assertIn("attempt 2: remote branch: delete still failed", body)
        self.assertEqual(body.count("### Operator action"), 1)

    def test_pre_closeout_failure_reports_zero_attempts_and_no_retry(self):
        pr = merged_pr()
        with patch.object(merge_pr, "fetch_pr", return_value=pr), \
             patch.object(merge_pr, "surviving_worktree", return_value="unavailable"):
            body = merge_pr.human_intervention_body(
                pr, None, "gated-sha", "unknown", [], "command",
                blocked_before_closeout="merge audit: missing SHA",
            )

        self.assertIn("attempted remediation: 0 close-out attempts", body)
        self.assertIn("close-out not attempted: merge audit: missing SHA", body)
        self.assertNotIn("attempt 1:", body)

    def test_evidence_is_posted_to_pr_and_every_linked_issue(self):
        with patch.object(
            merge_pr, "human_intervention_body", return_value="evidence"
        ), patch.object(merge_pr, "run_cmd", return_value=(0, "", "")) as run:
            ok = merge_pr.post_human_intervention(
                merged_pr(), [7, 8], "/repo", "gated", "merged", [[]], "command"
            )

        self.assertTrue(ok)
        targets = [(call.args[0][1], call.args[0][3]) for call in run.call_args_list]
        self.assertEqual(targets, [("pr", "9"), ("issue", "7"), ("issue", "8")])


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
        ok, message = merge_pr.cleanup_local_branch(
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
        ok, message = merge_pr.cleanup_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertFalse(ok)
        self.assertIn("unrelated ref retained", message)
        self.assertIn("lease mismatch", message)
        self.assertEqual(run.call_count, 1)

    @patch.object(merge_pr, "run_cmd")
    def test_unattached_local_branch_at_gated_sha_is_deleted(self, run):
        run.side_effect = [
            (0, "gated-sha\n", ""),  # rev-parse --verify
            (0, "worktree /repo\nbranch refs/heads/main\n", ""),  # worktree list
            (0, "", ""),  # git update-ref -d
        ]
        ok, message = merge_pr.cleanup_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertTrue(ok)
        self.assertIn("Deleted local branch", message)
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["git", "update-ref", "-d", "refs/heads/fix/issue-7-x", "gated-sha"],
        )

    @patch.object(merge_pr, "run_cmd")
    def test_unattached_local_branch_delete_failure_is_reported(self, run):
        run.side_effect = [
            (0, "gated-sha\n", ""),  # rev-parse --verify
            (0, "worktree /repo\nbranch refs/heads/main\n", ""),  # worktree list
            (1, "", "unable to lock ref"),  # git update-ref -d
            (0, "gated-sha\n", ""),  # post-failure rev-parse check
        ]
        ok, message = merge_pr.cleanup_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertFalse(ok)
        self.assertIn("Orphan local branch", message)
        self.assertIn("unable to lock ref", message)

    @patch.object(merge_pr, "run_cmd")
    def test_local_branch_cleanup_fails_closed_when_worktree_enumeration_fails(self, run):
        run.side_effect = [
            (0, "gated-sha\n", ""),  # rev-parse --verify
            (1, "", "worktree listing failed"),  # worktree list
        ]
        ok, message = merge_pr.cleanup_local_branch(
            "/repo", "fix/issue-7-x", "gated-sha"
        )
        self.assertFalse(ok)
        self.assertIn("Could not enumerate worktrees", message)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["git", "worktree", "list", "--porcelain"],
        )

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
                ok, message = merge_pr.cleanup_local_branch(
                    repo, branch, expected_sha
                )

            self.assertTrue(raced)
            self.assertFalse(ok)
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
            retained, message = merge_pr.cleanup_local_branch(
                repo, branch, expected_sha
            )

            self.assertFalse(pruned)
            self.assertFalse(retained)
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
            origin = root / "origin.git"
            self._git(root, "clone", "--bare", str(repo), str(origin))
            self._git(repo, "remote", "add", "origin", str(origin))
            self._git(repo, "push", "origin", "HEAD:main")
            secret = worktree / "secret.txt"
            retained = (
                repo / ".worktrees" / ".retained" /
                f"{expected_sha[:12]}-{worktree.name}"
            )
            real_run_cmd = merge_pr.run_cmd
            raced = False
            status_hits = 0

            def inject_secret_after_preflight(command, **kwargs):
                nonlocal raced, status_hits
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "status", "--porcelain"]:
                    status_hits += 1
                    if status_hits == 1:
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
            self.assertFalse(pruned)
            self.assertIn("tracked or untracked files", message)
            self.assertTrue(worktree.exists())
            self.assertFalse((worktree / ".aru-retained-clean").exists())
            self.assertEqual(secret.read_text(), "late secret\n")
            self.assertFalse(retained.exists())
            sweep_ok, sweep_msg = cleanup_worktrees.sweep(
                str(repo), include_labels=False
            )
            self.assertTrue(sweep_ok)
            self.assertTrue(secret.exists())
            self.assertEqual(secret.read_text(), "late secret\n")
            self.assertIn("dirty", sweep_msg)

    def test_ignored_file_created_after_snapshot_survives_retained_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "late-secret"
            branch = "fix/late-secret"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            (repo / ".gitignore").write_text("secret.txt\n")
            self._git(repo, "add", ".gitignore")
            self._git(repo, "commit", "-m", "ignore local secret")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            origin = root / "origin.git"
            self._git(root, "clone", "--bare", str(repo), str(origin))
            self._git(repo, "remote", "add", "origin", str(origin))
            self._git(repo, "push", "origin", "HEAD:main")
            secret = worktree / "secret.txt"
            retained = (
                repo / ".worktrees" / ".retained" /
                f"{expected_sha[:12]}-{worktree.name}"
            )
            real_run_cmd = merge_pr.run_cmd
            raced = False
            status_hits = 0

            def inject_secret_after_snapshot(command, **kwargs):
                nonlocal raced, status_hits
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "status", "--porcelain"]:
                    status_hits += 1
                    if status_hits >= 2:
                        secret.write_text("late secret\n")
                        raced = True
                return result

            with patch.object(
                merge_pr, "run_cmd", side_effect=inject_secret_after_snapshot
            ):
                pruned, message = merge_pr.prune_worktree(
                    repo, branch, expected_sha
                )

            self.assertTrue(raced)
            self.assertTrue(pruned)
            self.assertIn("Retained worktree", message)
            self.assertFalse(worktree.exists())
            self.assertEqual((retained / "secret.txt").read_text(), "late secret\n")
            sweep_ok, sweep_msg = cleanup_worktrees.sweep(
                str(repo), include_labels=False
            )
            self.assertFalse(sweep_ok)
            self.assertTrue((retained / "secret.txt").exists())
            self.assertEqual((retained / "secret.txt").read_text(), "late secret\n")
            self.assertIn("dirty after retention", sweep_msg)

    def test_existing_retention_destination_does_not_write_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            worktree = repo / ".worktrees" / "collision"
            branch = "fix/collision"
            self._git(root, "init", "--initial-branch=main", str(repo))
            self._git(repo, "config", "user.name", "Aru Test")
            self._git(repo, "config", "user.email", "aru@example.invalid")
            self._git(repo, "commit", "--allow-empty", "-m", "seed")
            worktree.parent.mkdir()
            self._git(repo, "worktree", "add", "-b", branch, str(worktree))
            expected_sha = self._git(worktree, "rev-parse", "HEAD")
            retained = (
                repo / ".worktrees" / ".retained" /
                f"{expected_sha[:12]}-{worktree.name}"
            )
            retained.mkdir(parents=True)
            (retained / "already.txt").write_text("keep\n")

            pruned, message = merge_pr.prune_worktree(
                repo, branch, expected_sha
            )

            self.assertFalse(pruned)
            self.assertIn("already exists", message)
            self.assertTrue(worktree.exists())
            self.assertFalse((worktree / ".aru-retained-clean").exists())
            self.assertEqual((retained / "already.txt").read_text(), "keep\n")

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


class OrphanLocalBranchRegressionTests(unittest.TestCase):
    """Models the PR #79/#85 incident (#343): a merged PR whose worktree is
    pruned but the local branch lags one close-out pass behind must still
    reach Done without a stuck ``merger:`` claim, and a genuinely attached or
    reused branch must still never be force-deleted.
    """

    @staticmethod
    def _git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, text=True, capture_output=True,
        ).stdout.strip()

    def _seed_merged_branch(self, directory):
        repo = Path(directory) / "repo"
        worktree = repo / ".worktrees" / "fix-79"
        branch = "fix/issue-79-example"
        self._git(directory, "init", "--initial-branch=main", str(repo))
        self._git(repo, "config", "user.name", "Aru Test")
        self._git(repo, "config", "user.email", "aru@example.invalid")
        self._git(repo, "commit", "--allow-empty", "-m", "seed")
        worktree.parent.mkdir()
        self._git(repo, "worktree", "add", "-b", branch, str(worktree))
        expected_sha = self._git(worktree, "rev-parse", "HEAD")
        return repo, worktree, branch, expected_sha

    def _remote_lifecycle_patches(self):
        return [
            patch.object(merge_pr, "delete_remote_branch", return_value=(True, "remote gone")),
            patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed")),
            patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done")),
            patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue claim clear")),
            patch.object(merge_pr, "clear_review_claims", return_value=(True, "review claim clear")),
            patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor ok")),
        ]

    def test_worktree_pruned_first_pass_branch_deleted_on_retry_after_transient_failure(self):
        """Pass 1 prunes the worktree registration but the delete attempt hits
        a transient failure (real PR #79/#85 symptom); pass 2 -- with no
        worktree and the local branch still present -- succeeds without any
        regression to the already-complete remote/board state.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            pr = dict(merged_pr(), headRefName=branch, headRefOid=expected_sha, number=79)

            remote_patches = self._remote_lifecycle_patches()
            for item in remote_patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in remote_patches])

            real_run_cmd = merge_pr.run_cmd
            first_delete_seen = False

            def fail_first_branch_delete(command, **kwargs):
                nonlocal first_delete_seen
                if command[:3] == ["git", "update-ref", "-d"] and not first_delete_seen:
                    first_delete_seen = True
                    return 1, "", "simulated transient lock"
                return real_run_cmd(command, **kwargs)

            with patch.object(merge_pr, "run_cmd", side_effect=fail_first_branch_delete), \
                 patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear")) as merger:
                failures_pass1 = []
                ok1 = merge_pr.run_closeout(pr, [7], str(repo), failures=failures_pass1)

            # Pass 1: worktree registration is gone, but the branch delete failed.
            self.assertFalse(ok1)
            self.assertFalse(worktree.exists())
            self.assertNotIn(
                f"refs/heads/{branch}",
                self._git(repo, "worktree", "list", "--porcelain"),
            )
            self.assertTrue(any(f.startswith("local branch:") for f in failures_pass1))
            merger.assert_not_called()
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"), expected_sha,
            )

            with patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear")) as merger2:
                failures_pass2 = []
                ok2 = merge_pr.run_closeout(pr, [7], str(repo), failures=failures_pass2)

            # Pass 2 (the bounded follow-up): no worktree, branch now deletes cleanly.
            self.assertTrue(ok2)
            self.assertEqual(failures_pass2, [])
            merger2.assert_called_once_with(79)
            branch_code, _, _ = merge_pr.run_cmd(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False, cwd=str(repo),
            )
            self.assertNotEqual(branch_code, 0)

    def test_idempotent_rerun_after_worktree_already_absent_does_not_retain_claim(self):
        """Re-running close-out once everything -- including the local branch
        -- is already gone must not re-retain the merger claim (#343 AC3).
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            pr = dict(merged_pr(), headRefName=branch, headRefOid=expected_sha, number=85)

            remote_patches = self._remote_lifecycle_patches()
            for item in remote_patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in remote_patches])

            with patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear")) as merger:
                ok = merge_pr.run_closeout(pr, [7], str(repo))

            self.assertTrue(ok)
            self.assertFalse(worktree.exists())
            merger.assert_called_once_with(85)

            # A second, fully independent close-out call (e.g. operator reruns
            # merge_pr.py) must find everything already clean and idempotent.
            with patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear")) as merger2:
                failures = []
                ok2 = merge_pr.run_closeout(pr, [7], str(repo), failures=failures)

            self.assertTrue(ok2)
            self.assertEqual(failures, [])
            merger2.assert_called_once_with(85)

    def test_live_dirty_worktree_branch_is_never_force_deleted_across_retries(self):
        """A branch still attached to a genuinely dirty worktree must survive
        every close-out attempt -- the fail-safe must never paper over a real,
        unresolved live-worktree condition (#343 decision boundary).
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            (worktree / "dirty.txt").write_text("uncommitted\n")
            pr = dict(merged_pr(), headRefName=branch, headRefOid=expected_sha, number=79)

            remote_patches = self._remote_lifecycle_patches()
            for item in remote_patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in remote_patches])

            with patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "clear_merger_claims") as merger:
                failures = []
                ok = merge_pr.run_closeout(pr, [7], str(repo), failures=failures)

            self.assertFalse(ok)
            merger.assert_not_called()
            self.assertTrue(worktree.exists())
            self.assertTrue((worktree / "dirty.txt").exists())
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"), expected_sha,
            )
            self.assertTrue(any(f.startswith("worktree:") for f in failures))

    def test_real_atomic_lease_delete_success(self):
        """Verifies that an unattached branch matching expected_sha is atomically deleted."""
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            self._git(repo, "worktree", "remove", "--force", str(worktree))
            ok, message = merge_pr.cleanup_local_branch(repo, branch, expected_sha)
            self.assertTrue(ok)
            self.assertIn("Deleted local branch", message)
            code, _, _ = merge_pr.run_cmd(
                ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False, cwd=str(repo),
            )
            self.assertNotEqual(code, 0)

    def test_real_ref_moved_between_observation_and_delete_fails_lease_and_preserves_new_ref(self):
        """If the branch moves/advances between observation and update-ref,
        the lease fails, the new ref is preserved, and close-out returns failure.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            self._git(repo, "worktree", "remove", "--force", str(worktree))

            real_run_cmd = merge_pr.run_cmd
            raced = False
            new_sha = None

            def inject_ref_advance(command, **kwargs):
                nonlocal raced, new_sha
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "rev-parse", "--verify"] and not raced:
                    self._git(repo, "commit", "--allow-empty", "-m", "concurrent commit")
                    new_sha = self._git(repo, "rev-parse", "HEAD")
                    self._git(repo, "update-ref", f"refs/heads/{branch}", new_sha)
                    raced = True
                return result

            with patch.object(merge_pr, "run_cmd", side_effect=inject_ref_advance):
                ok, message = merge_pr.cleanup_local_branch(repo, branch, expected_sha)

            self.assertTrue(raced)
            self.assertFalse(ok)
            self.assertIn("lease failed", message)
            current = self._git(repo, "rev-parse", f"refs/heads/{branch}")
            self.assertEqual(current, new_sha)

    def test_real_ref_recreated_at_different_sha_fails_lease_and_preserves_ref(self):
        """If a branch was deleted and recreated at a new commit, cleanup_local_branch
        detects the lease mismatch, preserves the new ref, and returns failure.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            self._git(repo, "worktree", "remove", "--force", str(worktree))
            self._git(repo, "commit", "--allow-empty", "-m", "unrelated commit")
            unrelated_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "update-ref", f"refs/heads/{branch}", unrelated_sha)

            ok, message = merge_pr.cleanup_local_branch(repo, branch, expected_sha)
            self.assertFalse(ok)
            self.assertIn("lease mismatch", message)
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"),
                unrelated_sha,
            )

    def test_real_reattached_worktree_between_prune_and_cleanup_blocks_and_retains_merger_claim(self):
        """If a new worktree attaches to the branch after prune_worktree finishes
        but before cleanup_local_branch runs, cleanup_local_branch returns failure
        so the merger claim is retained.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            new_worktree = repo / ".worktrees" / "new-attacher"

            pr = dict(merged_pr(), headRefName=branch, headRefOid=expected_sha, number=79)
            remote_patches = self._remote_lifecycle_patches()
            for item in remote_patches:
                item.start()
            self.addCleanup(lambda: [item.stop() for item in remote_patches])

            real_prune = merge_pr.prune_worktree

            def prune_and_reattach(r, b, sha):
                res = real_prune(r, b, sha)
                # Race occurs: another process creates a worktree attached to this branch
                self._git(repo, "worktree", "add", str(new_worktree), branch)
                return res

            with patch.object(merge_pr.os, "chdir"), \
                 patch.object(merge_pr, "prune_worktree", side_effect=prune_and_reattach), \
                 patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear")) as merger:
                failures = []
                ok = merge_pr.run_closeout(pr, [7], str(repo), failures=failures)

            self.assertFalse(ok)
            merger.assert_not_called()
            self.assertTrue(new_worktree.exists())
            self.assertEqual(
                self._git(repo, "rev-parse", f"refs/heads/{branch}"),
                expected_sha,
            )
            self.assertTrue(any("attached to a worktree" in f for f in failures))

    def test_invalid_branch_name_rejected_safely(self):
        """Invalid branch names fail ref validation without invoking git update-ref."""
        for invalid in ["-leading-dash", "has..dotdot", "has:colon", "has?glob", "has space", "ends.lock", "ends/"]:
            ok, message = merge_pr.cleanup_local_branch("/repo", invalid, "gated-sha")
            self.assertFalse(ok)
            self.assertIn("Invalid local branch ref name", message)

    def test_concurrent_deletion_during_update_ref_reports_already_absent(self):
        """If another process deletes the branch concurrently during the race window
        before update-ref -d, cleanup_local_branch detects it is absent and reports success.
        """
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, branch, expected_sha = self._seed_merged_branch(directory)
            self._git(repo, "worktree", "remove", "--force", str(worktree))

            real_run_cmd = merge_pr.run_cmd
            raced = False

            def inject_concurrent_deletion(command, **kwargs):
                nonlocal raced
                result = real_run_cmd(command, **kwargs)
                if command[:3] == ["git", "rev-parse", "--verify"] and not raced:
                    self._git(repo, "branch", "-D", branch)
                    raced = True
                return result

            with patch.object(merge_pr, "run_cmd", side_effect=inject_concurrent_deletion):
                ok, message = merge_pr.cleanup_local_branch(repo, branch, expected_sha)

            self.assertTrue(raced)
            self.assertTrue(ok)
            self.assertIn("already absent", message)


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


class DodStatusHeadBindingTests(unittest.TestCase):
    @patch.object(merge_pr, "evaluate_dod")
    @patch.object(merge_pr, "review_evidence", return_value={"head_oid": "H2"})
    @patch.object(merge_pr, "_gh_json", return_value={"body": ""})
    @patch.object(merge_pr, "fetch_pr")
    def test_status_refuses_evidence_from_a_different_head(
        self, fetch_pr, _issue, _evidence, evaluate
    ):
        fetch_pr.return_value = ExpectedHeadGateTests.open_pr("H1")

        ok, reason = merge_pr.dod_status(9)

        self.assertFalse(ok)
        self.assertIn("evidence covers H2", reason)
        self.assertIn("snapshot is H1", reason)
        evaluate.assert_not_called()


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


class BodyEditEvidenceTests(unittest.TestCase):
    HEAD = "a" * 40

    @classmethod
    def verification_body(cls, head):
        payload = json.dumps({
            "schema": merge_pr.VERIFICATION_EVIDENCE_SCHEMA,
            "head_sha": head,
        }, sort_keys=True)
        return (
            f"{merge_pr.VERIFICATION_EVIDENCE_START}\n"
            f"```json\n{payload}\n```\n"
            f"{merge_pr.VERIFICATION_EVIDENCE_END}"
        )

    @staticmethod
    def edit(edit_id, edited_at, snapshot, editor="author"):
        return {
            "id": edit_id,
            "editedAt": edited_at,
            "diff": snapshot,
            "editor": {"login": editor, "__typename": "User"},
        }

    @classmethod
    def page(cls, nodes, body, *, has_next=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": cls.HEAD,
            "body": body,
            "author": {"login": "author", "__typename": "User"},
            "userContentEdits": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }}}}

    @patch.object(merge_pr, "_gh_json")
    def test_size_waiver_transition_is_author_body_evidence(self, gh_json):
        before = "Closes #115\n"
        after = before + "size-waiver: cohesive checker and tests\n"
        gh_json.return_value = self.page([
            self.edit("old", "2026-08-17T00:30:00Z", before),
            self.edit("new", "2026-08-17T00:34:00Z", after),
        ], after)

        events = merge_pr._body_edit_events(
            "owner", "repo", 248, self.HEAD,
        )

        self.assertEqual(len(events["size-waiver"]), 1)
        self.assertEqual(events["verification"], [])

    @patch.object(merge_pr, "_gh_json")
    def test_refresh_pr_transition_requires_current_head_evidence(self, gh_json):
        old = self.verification_body("b" * 40)
        current = self.verification_body(self.HEAD)
        gh_json.return_value = self.page([
            self.edit("old", "2026-08-17T00:30:00Z", old),
            self.edit("new", "2026-08-17T00:34:00Z", current),
        ], current)

        events = merge_pr._body_edit_events(
            "owner", "repo", 248, self.HEAD,
        )

        self.assertEqual(len(events["verification"]), 1)
        self.assertEqual(events["size-waiver"], [])

    @patch.object(merge_pr, "_gh_json")
    def test_removed_remedies_do_not_leave_historical_evidence(self, gh_json):
        before = "Closes #115\n"
        compliant = (
            before + "size-waiver: cohesive change\n"
            + self.verification_body(self.HEAD)
        )
        gh_json.return_value = self.page([
            self.edit("old", "2026-08-17T00:30:00Z", before),
            self.edit("added", "2026-08-17T00:34:00Z", compliant),
            self.edit("removed", "2026-08-17T00:35:00Z", before),
        ], before)

        events = merge_pr._body_edit_events(
            "owner", "repo", 248, self.HEAD,
        )

        self.assertEqual(events, {"size-waiver": [], "verification": []})

    def test_inverted_verification_markers_fail_closed(self):
        inverted = (
            f"{merge_pr.VERIFICATION_EVIDENCE_END}\n"
            f"{merge_pr.VERIFICATION_EVIDENCE_START}\n"
        )
        self.assertIsNone(merge_pr._verification_region(inverted))

    def test_body_remedy_classifier_rejects_parser_findings(self):
        self.assertIsNone(merge_pr._finding_body_region(
            "The `size-waiver:` parser accepts blank input; fix the code.",
        ))
        self.assertIsNone(merge_pr._finding_body_region(
            "The `--refresh-pr` parser overwrites reviewer text; fix it.",
        ))
        self.assertEqual(
            merge_pr._finding_body_region(
                "Add `size-waiver:` to the PR body.",
            ),
            "size-waiver",
        )
        self.assertEqual(
            merge_pr._finding_body_region(
                "Verification is stale. Run `--refresh-pr`.",
            ),
            "verification",
        )
        self.assertEqual(
            merge_pr._finding_body_region(
                "Verification is stale. Refresh with:\n```\n"
                "python3 \"$ARU_SDLC_HOME/scripts/create_pr.py\" "
                "--refresh-pr 248 --issue 115\n```",
            ),
            "verification",
        )

    @patch.object(merge_pr, "_gh_json")
    def test_unrelated_or_non_author_edit_is_not_evidence(self, gh_json):
        before = "Notes: first\n"
        after = "Notes: second\nsize-waiver: added by bot\n"
        gh_json.return_value = self.page([
            self.edit("old", "2026-08-17T00:30:00Z", before),
            self.edit("new", "2026-08-17T00:34:00Z", after, editor="bot"),
        ], after)

        events = merge_pr._body_edit_events(
            "owner", "repo", 248, self.HEAD,
        )

        self.assertEqual(events, {"size-waiver": [], "verification": []})

    @patch.object(merge_pr, "_gh_json")
    def test_edit_history_paginates_and_fails_closed_on_head_change(self, gh_json):
        before = "Closes #115\n"
        after = before + "size-waiver: justified\n"
        gh_json.side_effect = [
            self.page(
                [self.edit("new", "2026-08-17T00:34:00Z", after)],
                after,
                has_next=True,
                cursor="older",
            ),
            self.page(
                [self.edit("old", "2026-08-17T00:30:00Z", before)],
                after,
            ),
        ]
        events = merge_pr._body_edit_events(
            "owner", "repo", 248, self.HEAD,
        )
        self.assertEqual(len(events["size-waiver"]), 1)
        self.assertIn("cursor=older", gh_json.call_args_list[1].args[0])

        changed = self.page([], after)
        changed["data"]["repository"]["pullRequest"]["headRefOid"] = "moved"
        gh_json.side_effect = None
        gh_json.return_value = changed
        self.assertIsNone(
            merge_pr._body_edit_events("owner", "repo", 248, self.HEAD)
        )


class ReviewBodyEditIntegrationTests(unittest.TestCase):
    HEAD = "a" * 40
    REVIEW = {
        "id": "peer-review",
        "state": "COMMENTED",
        "submittedAt": "2026-08-17T00:20:00Z",
        "body": "Verdict: approved after fixes.",
        "author": {"login": "gillella", "__typename": "User"},
        "commit": {"oid": HEAD},
    }

    @staticmethod
    def thread_page(comments):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": ReviewBodyEditIntegrationTests.HEAD,
            "commits": {"nodes": []},
            "reviewThreads": {
                "nodes": [
                    {
                        "isResolved": True,
                        "isOutdated": False,
                        "comments": {"nodes": [{
                            "createdAt": "2026-08-17T00:23:58Z",
                            "body": body,
                        }]},
                    }
                    for body in comments
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}}

    def evidence(self, comments, events):
        after = merge_pr._parse_ts("2026-08-17T00:34:00Z")
        def event_times(name):
            value = events.get(name)
            if isinstance(value, list):
                return value
            return [after] if value else []

        normalized = {
            "size-waiver": event_times("size-waiver"),
            "verification": event_times("verification"),
        }
        with (
            patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"),
            patch.object(
                merge_pr, "_reviewed_current_head",
                return_value=(self.HEAD, True, [self.REVIEW]),
            ),
            patch.object(
                merge_pr, "_review_head_attestations",
                return_value=[{"agent": "agent-2", "head": self.HEAD}],
            ),
            patch.object(merge_pr, "_gh_json", return_value=self.thread_page(comments)),
            patch.object(merge_pr, "_body_edit_events", return_value=normalized),
        ):
            return merge_pr.review_evidence(248)

    def test_pr_248_two_body_only_remedies_pass_review_gate(self):
        evidence = self.evidence([
            "P1 — size waiver or split required. Add `size-waiver:` to the body.",
            "P1 — verification evidence is stale. Run `--refresh-pr`.",
        ], {"size-waiver": True, "verification": True})

        self.assertEqual(evidence["unfixed"], 0)
        self.assertEqual(evidence["body_addressed"], 2)
        ok, message = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), evidence,
        )
        self.assertTrue(ok)
        self.assertIn("2 finding(s) addressed by relevant PR body edit", message)

    def test_relevant_edit_before_finding_does_not_clear_thread(self):
        before = merge_pr._parse_ts("2026-08-17T00:10:00Z")
        evidence = self.evidence(
            ["size-waiver required"],
            {"size-waiver": [before], "verification": []},
        )
        self.assertEqual(evidence["unfixed"], 1)

    def test_body_edit_never_clears_unrelated_code_finding(self):
        evidence = self.evidence(
            ["The `size-waiver:` parser accepts blank input; fix the code."],
            {"size-waiver": True, "verification": True},
        )
        self.assertEqual(evidence["unfixed"], 1)
        self.assertEqual(evidence["body_addressed"], 0)


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

    def test_the_refusal_names_all_three_evidence_forms(self):
        """A refusal an agent cannot act on becomes a workaround."""
        _, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 2, "withdrawn": 0, "reviewed_head": True},
        )
        self.assertIn("Push the fix", msg)
        self.assertIn("size-waiver", msg)
        self.assertIn("verification-evidence", msg)
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
                            "nodes": [{
                                "id": "review-1", "state": "COMMENTED",
                                "submittedAt": "2026-08-10T09:00:00Z",
                                "body": "Verdict: approved.",
                                "author": {"login": "agent-2", "__typename": "User"},
                                "commit": {"oid": "head123"},
                            }],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "comments": {
                            "nodes": [],
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
                            "nodes": [{
                                "id": "review-1", "state": "COMMENTED",
                                "submittedAt": "2026-08-10T09:00:00Z",
                                "body": "Verdict: approved.",
                                "author": {"login": "agent-2", "__typename": "User"},
                                "commit": {"oid": "head123"},
                            }],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "comments": {
                            "nodes": [],
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
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "post_human_intervention", return_value=True), \
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
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "execute_merge",
                          return_value=(merged_pr(), "merged")), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "save_gate_verdicts", return_value=True), \
             patch.object(merge_pr, "load_gate_verdicts", return_value=None), \
             patch.object(merge_pr, "discard_gate_verdicts"), \
             patch.object(merge_pr, "run_closeout", return_value=closeout_ok), \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "post_human_intervention", return_value=True), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "written")) as tag:
            code = merge_pr.main()
        return code, tag

    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
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

    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
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

    def test_evidence_head_mismatch_emits_blocking_json(self):
        pr = {
            "number": 9, "title": "feat", "body": "Closes #1",
            "state": "OPEN", "mergedAt": None, "headRefOid": "H1",
            "labels": [],
        }
        with patch.object(
            sys, "argv", ["merge_pr.py", "--pr", "9", "--dry-run", "--json"]
        ), patch.object(
            merge_pr, "fetch_pr", return_value=pr
        ), patch.object(
            merge_pr, "_gh_json", return_value={"body": ""}
        ), patch.object(
            merge_pr, "review_evidence", return_value={"head_oid": "H2"}
        ), patch("builtins.print") as printer:
            code = merge_pr.main()

        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        printed = [str(call.args[0]) for call in printer.call_args_list if call.args]
        self.assertEqual(len(printed), 1)
        payload = json.loads(printed[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["first_blocking"], "review head")
        self.assertIn("different commits", payload["gates"][0]["message"])

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
             patch.object(merge_pr, "post_human_intervention") as intervention, \
             patch("builtins.print") as printer:
            code = merge_pr.main()
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute_merge.assert_not_called()
        intervention.assert_not_called()
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


if __name__ == "__main__":
    unittest.main()
