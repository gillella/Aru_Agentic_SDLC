# line-ceiling: 7120
from contextlib import nullcontext
from datetime import datetime, timezone
import inspect
import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common
import merge_pr
import cleanup_worktrees


def _pull_version(head="head123", reviews=0, comments=0, threads=0):
    """The evidence-version fields merge_pr requires on every review page."""
    return {"headRefOid": head, "reviewTotal": {"totalCount": reviews},
            "commentTotal": {"totalCount": comments},
            "threadTotal": {"totalCount": threads}}


# The tuple merge_pr derives from those fields, for callers that pass a
# version straight into a paginated reader.
_VERSION = merge_pr._evidence_version(_pull_version("a" * 40))


def _complete_comments(nodes):
    return {
        "totalCount": len(nodes),
        "nodes": nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }


def _gate(pr, threads=0, **overrides):
    """Run the review gate with clean default evidence."""
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
            **_pull_version(head),
            "reviews": {
                "nodes": normalized,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }}}}

    @staticmethod
    def thread_page(head="head123", nodes=None):
        normalized = []
        for node in [] if nodes is None else nodes:
            node = dict(node)
            comments = dict(node.get("comments") or {})
            comment_nodes = comments.get("nodes") or []
            comments.setdefault("totalCount", len(comment_nodes))
            comments.setdefault(
                "pageInfo", {"hasNextPage": False, "endCursor": None},
            )
            node["comments"] = comments
            normalized.append(node)
        return {"data": {"repository": {"pullRequest": {
            **_pull_version(head),
            "commits": {"nodes": [{"commit": {
                "committedDate": "2026-08-24T00:00:00Z",
            }}]},
            "reviewThreads": {
                "nodes": normalized,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}}

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_unattributed_active_thread_fails_closed(self, gh_json, _slug):
        malformed_comments = (
            [],
            [{"createdAt": "2026-08-24T01:00:00Z", "body": "finding", "author": None}],
            [{
                "createdAt": "2026-08-24T01:00:00Z",
                "body": "finding",
                "author": "malformed",
            }],
            [{
                "createdAt": "2026-08-24T01:00:00Z",
                "body": "finding",
                "author": {"login": "", "__typename": "Bot"},
            }],
        )
        for comments in malformed_comments:
            with self.subTest(comments=comments):
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [
                    self.review_page(),
                    self.attestation_page(),
                    self.thread_page(nodes=[{
                        "isResolved": False,
                        "isOutdated": False,
                        "comments": {"nodes": comments},
                    }]),
                ]
                self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_nested_thread_comments_require_complete_truncation_proof(
        self, gh_json, _slug,
    ):
        comment = {
            "createdAt": "2026-08-24T01:00:00Z",
            "body": "finding",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }
        cases = ("missing-total", "missing-page", "truncated", "count-mismatch")
        for case in cases:
            with self.subTest(case=case):
                thread_page = self.thread_page(nodes=[{
                    "isResolved": False,
                    "isOutdated": False,
                    "comments": {"nodes": [comment]},
                }])
                connection = thread_page["data"]["repository"]["pullRequest"][
                    "reviewThreads"
                ]["nodes"][0]["comments"]
                if case == "missing-total":
                    connection.pop("totalCount")
                elif case == "missing-page":
                    connection.pop("pageInfo")
                elif case == "truncated":
                    connection["pageInfo"]["hasNextPage"] = True
                    connection["pageInfo"]["endCursor"] = "more"
                else:
                    connection["totalCount"] = 2
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [
                    self.review_page(), self.attestation_page(), thread_page,
                ]
                self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_commit_history_requires_complete_aware_entries(
        self, gh_json, _slug,
    ):
        malformed = (
            "missing", None, {}, {"nodes": None}, {"nodes": []},
            {"nodes": [None]}, {"nodes": [{}]},
            {"nodes": [{"commit": None}]},
            {"nodes": [{"commit": {}}]},
            {"nodes": [{"commit": {"committedDate": "not-a-time"}}]},
            {"nodes": [{"commit": {
                "committedDate": "2026-08-24T00:00:00",
            }}]},
        )
        for commits in malformed:
            with self.subTest(commits=commits):
                page = self.thread_page()
                pull = page["data"]["repository"]["pullRequest"]
                if commits == "missing":
                    pull.pop("commits")
                else:
                    pull["commits"] = commits
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [
                    self.review_page(), self.attestation_page(), page,
                ]
                self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_commit_history_must_agree_across_thread_pages(
        self, gh_json, _slug,
    ):
        # Every thread page repeats the full commit history. Two well-formed
        # but differing histories mean the branch was pushed to between page
        # fetches, so the pages describe different trees and the assembled
        # evidence cannot be trusted for any one head.
        first = self.thread_page()
        first["data"]["repository"]["pullRequest"]["reviewThreads"][
            "pageInfo"
        ] = {"hasNextPage": True, "endCursor": "next"}
        second = self.thread_page()
        second["data"]["repository"]["pullRequest"]["commits"] = {
            "nodes": [{"commit": {"committedDate": "2026-08-24T02:00:00Z"}}],
        }

        gh_json.side_effect = [
            self.review_page(), self.attestation_page(), first, second,
        ]

        self.assertIsNone(merge_pr.review_evidence(162))
        self.assertEqual(gh_json.call_count, 4)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_thread_timestamp_must_be_timezone_aware(self, gh_json, _slug):
        page = self.thread_page(nodes=[{
            "isResolved": False,
            "isOutdated": False,
            "comments": _complete_comments([{
                "createdAt": "2026-08-24T01:00:00",
                "body": "finding",
                "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            }]),
        }])
        gh_json.side_effect = [
            self.review_page(), self.attestation_page(), page,
        ]
        self.assertIsNone(merge_pr.review_evidence(162))

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_only_coderabbit_can_withdraw_its_finding(self, gh_json, _slug):
        actors = (
            ({"login": "gillella", "__typename": "User"}, 0, 1),
            ({"login": "other-bot", "__typename": "Bot"}, 0, 1),
            ({"login": "coderabbitai[bot]", "__typename": "Bot"}, 1, 0),
        )
        root = {
            "createdAt": "2026-08-24T01:00:00Z",
            "body": "finding",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }
        for actor, withdrawn, unfixed in actors:
            with self.subTest(actor=actor):
                reply = {
                    "createdAt": "2026-08-24T02:00:00Z",
                    "body": "Withdrawn: rationale",
                    "author": actor,
                }
                page = self.thread_page(nodes=[{
                    "isResolved": True,
                    "isOutdated": False,
                    "comments": _complete_comments([root, reply]),
                }])
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [
                    self.review_page(), self.attestation_page(), page,
                ]
                evidence = merge_pr.review_evidence(162)
                self.assertEqual(evidence["withdrawn"], withdrawn)
                self.assertEqual(evidence["unfixed"], unfixed)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_evidence_read_from_two_states_fails_closed(self, gh_json, _slug):
        """A review or thread created mid-read is never merged around.

        Reviews, comments and threads are three separate paginated calls, so
        an unchanged head proves only that nobody pushed. Every page reports
        the size of all three connections, and any disagreement between two
        pages fails the whole read closed instead of merging on a torn view.
        """
        empty_page = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        for moved in ("reviews", "comments", "threads"):
            for connection in ("comments", "reviewThreads"):
                with self.subTest(moved=moved, read=connection):
                    gh_json.reset_mock(side_effect=True, return_value=True)
                    later = {"data": {"repository": {"pullRequest": {
                        **_pull_version(**{moved: 1}),
                        "commits": {"nodes": [{"commit": {
                            "committedDate": "2026-08-24T00:00:00Z"}}]},
                        connection: empty_page,
                    }}}}
                    pages = [self.review_page(), later]
                    if connection == "reviewThreads":
                        pages.insert(1, self.attestation_page())
                    gh_json.side_effect = pages
                    self.assertIsNone(merge_pr.review_evidence(162))

    @staticmethod
    def attestation_page(head="head123", nodes=None, has_next=False, cursor=None):
        return {"data": {"repository": {"pullRequest": {
            **_pull_version(head),
            "comments": {
                "nodes": [] if nodes is None else nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }}}}

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_retired_agent_review_markers_are_not_parsed(self, gh_json, _slug):
        """#414: an agent-written completion marker is inert comment text.

        Parsing it would keep a merge path alive that nothing can legitimately
        produce any more, so a forged copy would be the only way to reach it.
        """
        head = "a" * 40
        payload = json.dumps({
            "agent": "agent-2", "completed_at": "2026-08-25T10:03:00Z",
            "disposition": "no-findings", "family": "openai", "head": head,
            "status": "completed",
        }, sort_keys=True, separators=(",", ":"))
        nodes = [
            {"body": f"<!-- aru-agent-review:v1 {payload} -->",
             "createdAt": "2026-08-25T10:03:00Z",
             "author": {"login": "gillella", "__typename": "User"}},
            {"body": '<!-- aru-review-head:v1 {"agent":"agent-2","head":"'
                     + head + '"} -->',
             "createdAt": "2026-08-25T10:03:00Z",
             "author": {"login": "gillella", "__typename": "User"}},
        ]
        gh_json.side_effect = [
            self.review_page(head=head),
            self.attestation_page(head=head, nodes=nodes),
            self.thread_page(head=head),
        ]
        evidence = merge_pr.review_evidence(162)
        for key in ("agent_review_attestations", "agent_review_marker_errors",
                    "agent_review_assignments", "agent_review_assignment_errors",
                    "review_attestations"):
            self.assertNotIn(key, evidence)

    @patch.object(merge_pr, "get_repo_slug", return_value="owner/repo")
    @patch.object(merge_pr, "_gh_json")
    def test_full_review_comment_timestamp_requires_timezone(
        self, gh_json, _slug,
    ):
        gh_json.side_effect = [
            self.review_page(),
            self.attestation_page(nodes=[{
                "body": "Full review finished.",
                "createdAt": "2026-08-24T01:00:00",
                "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            }]),
        ]
        self.assertIsNone(merge_pr.review_evidence(162))

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
            self.attestation_page(head=head),
            self.thread_page(head=head)
        ]

        evidence = merge_pr.review_evidence(215)

        self.assertTrue(evidence["reviewed_head"])
        # A same-account human review attests the head, but it is not the
        # assigned service, so it never satisfies the gate on its own.
        pr = labelled("author:codex-1")
        ok, message = merge_pr.check_reviews(pr, evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", message)

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
        self.assertIn("coderabbit", message.lower())

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
    def test_thread_pagination_rejects_non_string_cursors(
        self, gh_json, _slug,
    ):
        for cursor in (7, ["next"], {"cursor": "next"}):
            with self.subTest(cursor=cursor):
                thread_page = self.thread_page()
                thread_page["data"]["repository"]["pullRequest"][
                    "reviewThreads"
                ]["pageInfo"] = {
                    "hasNextPage": True,
                    "endCursor": cursor,
                }
                gh_json.reset_mock(side_effect=True, return_value=True)
                gh_json.side_effect = [
                    self.review_page(),
                    self.attestation_page(),
                    thread_page,
                ]

                self.assertIsNone(merge_pr.review_evidence(162))
                self.assertEqual(gh_json.call_count, 3)

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


class AcceptanceEnvironmentTests(unittest.TestCase):
    """#429: a refused merge must not corrupt the PR it refused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Resolution consults VIRTUAL_ENV, so a suite run from an activated
        # virtualenv would otherwise resolve a runner these cases declare
        # unresolvable and pass or fail on the operator's shell, not the code.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("VIRTUAL_ENV", None)

    def _make_venv_runner(self, name, root=".venv", exit_code=0):
        binaries = os.path.join(self.tmp, root, "bin")
        os.makedirs(binaries, exist_ok=True)
        path = os.path.join(binaries, name)
        with open(path, "w") as handle:
            handle.write(f"#!/bin/sh\nexit {exit_code}\n")
        os.chmod(path, 0o755)
        return path

    def test_project_virtualenv_wins_over_ambient_path(self):
        expected = self._make_venv_runner("python3")
        resolved, error = merge_pr.resolve_project_runner("python3", self.tmp)
        self.assertIsNone(error)
        self.assertEqual(resolved, expected)

    def test_legacy_venv_directory_is_also_honoured(self):
        expected = self._make_venv_runner("python3", root="venv")
        resolved, error = merge_pr.resolve_project_runner("python3", self.tmp)
        self.assertIsNone(error)
        self.assertEqual(resolved, expected)

    def test_falls_back_to_path_when_no_project_virtualenv(self):
        with patch.object(merge_pr.shutil, "which", return_value="/usr/bin/python3"):
            resolved, error = merge_pr.resolve_project_runner("python3", self.tmp)
        self.assertIsNone(error)
        self.assertEqual(resolved, "/usr/bin/python3")

    def test_unresolvable_runner_is_an_environment_fault(self):
        with patch.object(merge_pr.shutil, "which", return_value=None):
            resolved, error = merge_pr.resolve_project_runner("python3", self.tmp)
        self.assertIsNone(resolved)
        self.assertIn("environment fault", error)
        self.assertIn("python3", error)

    def test_malformed_runner_name_fails_closed(self):
        for name in (None, "", 7, []):
            with self.subTest(name=name):
                resolved, error = merge_pr.resolve_project_runner(name, self.tmp)
                self.assertIsNone(resolved)
                self.assertTrue(error)

    def test_resolution_reports_the_first_fault_without_running_anything(self):
        criteria = merge_pr.acceptance_runner.parse_criteria(
            "## Acceptance Criteria\n\n"
            "- [x] one (verify: `python3 -m unittest tests.test_a`)\n"
        )
        with patch.object(merge_pr.shutil, "which", return_value=None):
            resolved, error = merge_pr.resolve_acceptance_runners(criteria, self.tmp)
        self.assertIsNone(resolved)
        self.assertIn("environment fault", error)

    def test_runner_rewrites_argv0_to_the_resolved_interpreter(self):
        seen = {}

        def fake_run(argv, cwd=None, evidence=None, check=False):
            seen["argv"] = list(argv)
            return 0, "", ""

        runner = merge_pr.acceptance_run_cmd({"python3": "/proj/.venv/bin/python3"})
        with patch.object(merge_pr.acceptance_runner, "_run_verify", fake_run):
            runner(["python3", "-m", "unittest", "tests.test_a"], cwd="/tmp")
        self.assertEqual(
            seen["argv"], ["/proj/.venv/bin/python3", "-m", "unittest", "tests.test_a"],
        )

    def test_unmapped_runner_is_passed_through_unchanged(self):
        seen = {}

        def fake_run(argv, cwd=None, evidence=None, check=False):
            seen["argv"] = list(argv)
            return 0, "", ""

        runner = merge_pr.acceptance_run_cmd({})
        with patch.object(merge_pr.acceptance_runner, "_run_verify", fake_run):
            runner(["ruff", "check", "scripts/merge_pr.py"])
        self.assertEqual(seen["argv"], ["ruff", "check", "scripts/merge_pr.py"])

class RefusedMergeIsNonDestructiveTests(unittest.TestCase):
    """#429: the refusal path end to end, driven through ``main()``.

    The resolver unit tests above prove which executable is chosen. These prove
    the consequence the issue is actually about: what a refused merge leaves
    behind on the pull request, and that a second attempt finds it unchanged.
    """

    BODY = (
        "## Acceptance Criteria\n\n"
        "- [x] the gate runs this (verify: `python3 -m unittest tests.test_example`)\n"
    )
    # Verbs that reach GitHub. A refusal must issue none of them.
    MUTATIONS = frozenset({
        "edit", "comment", "merge", "create", "close", "delete",
        "--add-label", "--remove-label", "POST", "PATCH", "PUT", "DELETE",
    })

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("VIRTUAL_ENV", None)

    def _runner(self, exit_code, root=".venv"):
        binaries = os.path.join(self.tmp, root, "bin")
        os.makedirs(binaries, exist_ok=True)
        path = os.path.join(binaries, "python3")
        with open(path, "w") as handle:
            handle.write(f"#!/bin/sh\nexit {exit_code}\n")
        os.chmod(path, 0o755)
        return path

    def _pr(self):
        return {
            "number": 9, "title": "example", "body": "Closes #7", "state": "OPEN",
            "isDraft": False, "headRefOid": "gated-sha", "baseRefOid": "base-sha",
            "baseRefName": "main", "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE", "labels": [],
        }

    def _drive(
        self,
        pr,
        *,
        ambient=None,
        check_rebased_return=(True, "current"),
        execute_merge_return=None,
        persist_error=False,
    ):
        """Run main() from the DoD pass down through merge execution.

        Every GitHub touch is recorded rather than issued, so the assertions
        below can speak about mutations that were *attempted*, not merely about
        ones that happened to succeed against a live API.
        """
        commands = []
        captured_state = {"body": pr.get("body", "")}

        def record_json(argv, *_args, **_kwargs):
            commands.append(list(argv))
            if len(argv) >= 3 and argv[0:2] == ["gh", "issue"] and argv[2] == "view":
                return {"body": self.BODY}
            if len(argv) >= 3 and argv[0:2] == ["gh", "pr"] and argv[2] == "view":
                return {"body": captured_state["body"], "headRefOid": pr.get("headRefOid")}
            return {"body": self.BODY}

        def record_run(argv, *_args, **_kwargs):
            commands.append(list(argv))
            if len(argv) >= 4 and argv[0:3] == ["gh", "pr", "edit"]:
                if persist_error:
                    return 1, "", "simulated persistence failure"
                if "--body" in argv:
                    captured_state["body"] = argv[argv.index("--body") + 1]
            return 0, "", ""

        def fetch_snapshot(pr_id):
            snap = dict(pr)
            snap["body"] = captured_state["body"]
            return snap

        exec_return = (
            execute_merge_return
            if execute_merge_return is not None
            else (merged_pr(), "merged")
        )

        which = patch.object(merge_pr.shutil, "which", return_value=ambient) \
            if ambient else nullcontext()
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", side_effect=fetch_snapshot), \
             patch.object(merge_pr, "_gh_json", side_effect=record_json), \
             patch.object(merge_pr, "run_cmd", side_effect=record_run), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "with_service_evidence",
                          side_effect=lambda _pr, _id, evidence: evidence), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "repository_root", return_value=self.tmp), \
             patch.object(merge_pr, "ensure_pr_head_checkout",
                          return_value=(self.tmp, None)), \
             patch.object(merge_pr, "release_pr_head_checkout"), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "_current_base_tip", return_value="base-sha"), \
             patch.object(merge_pr, "check_rebased", return_value=check_rebased_return), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "execute_merge", return_value=exec_return) as execute, \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "checkpoint written")), \
             which:
            code = merge_pr.main()
        return SimpleNamespace(
            code=code,
            commands=commands,
            captured_body=captured_state["body"],
            execute=execute,
        )

    def _assert_no_mutation(self, commands):
        for argv in commands:
            self.assertFalse(
                self.MUTATIONS.intersection(argv),
                f"a refused merge issued a mutating command: {argv}",
            )

    def test_failed_acceptance_twice_refuses_without_touching_pr(self):
        """Failed acceptance twice must assert no PR edit and byte-identical PR body."""
        self._runner(1)
        pr = self._pr()
        initial_body = pr["body"]

        first = self._drive(pr)
        self.assertEqual(first.code, merge_pr.EXIT_BLOCKED)
        first.execute.assert_not_called()
        self._assert_no_mutation(first.commands)
        self.assertEqual(first.captured_body, initial_body)

        second = self._drive(pr)
        self.assertEqual(second.code, merge_pr.EXIT_BLOCKED)
        second.execute.assert_not_called()
        self._assert_no_mutation(second.commands)
        self.assertEqual(second.captured_body, initial_body)
        self.assertEqual(
            first.commands, second.commands,
            "the second attempt did not repeat the first exactly",
        )

    def test_final_gate_refusal_after_successful_acceptance_twice_preserves_pr(self):
        """Final-gate refusal after successful acceptance twice must not edit PR and keep PR body identical."""
        self._runner(0)
        pr = self._pr()
        initial_body = pr["body"]

        first = self._drive(pr, check_rebased_return=(False, "Branch is behind base"))
        self.assertEqual(first.code, merge_pr.EXIT_BLOCKED)
        first.execute.assert_not_called()
        self._assert_no_mutation(first.commands)
        self.assertEqual(first.captured_body, initial_body)

        second = self._drive(pr, check_rebased_return=(False, "Branch is behind base"))
        self.assertEqual(second.code, merge_pr.EXIT_BLOCKED)
        second.execute.assert_not_called()
        self._assert_no_mutation(second.commands)
        self.assertEqual(second.captured_body, initial_body)
        self.assertEqual(
            first.commands, second.commands,
            "the second attempt did not repeat the first exactly",
        )

    def test_successful_acceptance_persists_evidence_post_merge(self):
        """Acceptance evidence is persisted only after execute_merge succeeds."""
        self._runner(0)
        pr = self._pr()
        initial_body = pr["body"]

        result = self._drive(pr)
        self.assertEqual(result.code, merge_pr.EXIT_OK)
        result.execute.assert_called_once()
        self.assertNotEqual(result.captured_body, initial_body)
        self.assertIn("aru.verification.v1", result.captured_body)
        self.assertIn("tests.test_example", result.captured_body)
        edit_cmds = [
            cmd for cmd in result.commands
            if len(cmd) >= 3 and cmd[0:3] == ["gh", "pr", "edit"]
        ]
        self.assertEqual(len(edit_cmds), 1)

    def test_post_merge_persistence_failure_triggers_intervention_recovery(self):
        """A persistence failure post-merge enters human intervention recovery, not pre-merge EXIT_BLOCKED."""
        self._runner(0)
        pr = self._pr()

        result = self._drive(pr, persist_error=True)
        self.assertEqual(result.code, merge_pr.EXIT_ERROR)
        result.execute.assert_called_once()
        intervention_comments = [
            cmd for cmd in result.commands
            if len(cmd) >= 3 and cmd[0:2] in (["gh", "issue"], ["gh", "pr"]) and cmd[2] == "comment"
        ]
        self.assertTrue(
            intervention_comments,
            "human intervention comment must be recorded on failure",
        )

    def test_an_unresolvable_interpreter_refuses_before_running_anything(self):
        """No project runner exists, so nothing runs and nothing is recorded."""
        with patch.object(merge_pr.shutil, "which", return_value=None):
            result = self._drive(self._pr())

        self.assertEqual(result.code, merge_pr.EXIT_BLOCKED)
        result.execute.assert_not_called()
        self._assert_no_mutation(result.commands)

    def test_a_command_passing_under_the_project_interpreter_is_not_a_failure(self):
        """#429's first defect: an ambient shim failed code the project passes."""
        ambient = os.path.join(self.tmp, "ambient-python3")
        with open(ambient, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(ambient, 0o755)
        project = self._runner(0)

        result = self._drive(self._pr(), ambient=ambient)

        self.assertEqual(result.code, merge_pr.EXIT_OK)
        result.execute.assert_called_once()
        evidence, error = merge_pr.parse_verification_evidence(result.captured_body)
        self.assertIsNone(error)
        self.assertEqual(
            [record["command"][0] for record in evidence.get("commands", [])],
            [common.sanitize_command([project])[0]],
        )
        self.assertTrue(
            all(record["status"] == "passed" for record in evidence.get("commands", [])),
        )


class SourceryEvidenceTests(unittest.TestCase):
    """#435: Sourcery satisfies the gate only on producer-validated exact-head proof."""

    HEAD = "b" * 40
    BASE = "c" * 40

    def pr(self):
        pr = labelled("author:agent-1", "review:sourcery")
        pr.update({"number": 433, "headRefOid": self.HEAD, "baseRefOid": self.BASE,
                   "body": "Closes #413"})
        return pr

    def run_entry(self, **over):
        entry = {
            "name": "Sourcery review", "app": {"slug": "sourcery-ai"},
            "head_sha": self.HEAD, "status": "completed", "conclusion": "success",
            "pull_requests": [{"number": 433, "head": {"sha": self.HEAD},
                               "base": {"sha": self.BASE}}],
        }
        entry.update(over)
        return entry

    def evidence(self, runs):
        return {"github_review_evidence": True, "head_oid": self.HEAD,
                "sourcery_check_runs": runs, "unresolved": 0, "unfixed": 0,
                "outdated_unfixed": 0}

    def test_valid_current_head_run_is_authoritative(self):
        self.assertTrue(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([self.run_entry()])))

    def test_wrong_producer_app_is_refused(self):
        """A check merely named "Sourcery review" proves nothing."""
        for slug in ("not-sourcery", "", None, "github-actions"):
            with self.subTest(slug=slug):
                run = self.run_entry(app={"slug": slug} if slug is not None else {})
                self.assertFalse(merge_pr.has_authoritative_sourcery_review(
                    self.pr(), self.evidence([run])))

    def test_prior_head_run_does_not_carry_forward(self):
        run = self.run_entry(head_sha="9" * 40)
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([run])))

    def test_two_sourcery_runs_are_ambiguous_and_block(self):
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([self.run_entry(), self.run_entry()])))

    def test_missing_run_blocks(self):
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([])))

    def test_unlinked_run_blocks(self):
        """Without a linked PR, a run from another PR sharing a head would pass."""
        for linked in ([], None, "nope"):
            with self.subTest(linked=linked):
                self.assertFalse(merge_pr.has_authoritative_sourcery_review(
                    self.pr(), self.evidence([self.run_entry(pull_requests=linked)])))

    def test_run_linked_to_a_different_pr_blocks(self):
        run = self.run_entry(pull_requests=[{"number": 999,
                                             "head": {"sha": self.HEAD},
                                             "base": {"sha": self.BASE}}])
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([run])))

    def test_run_linked_to_a_different_base_blocks(self):
        run = self.run_entry(pull_requests=[{"number": 433,
                                             "head": {"sha": self.HEAD},
                                             "base": {"sha": "d" * 40}}])
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(
            self.pr(), self.evidence([run])))

    def test_incomplete_or_failed_run_blocks(self):
        for over in ({"status": "in_progress"}, {"conclusion": "failure"},
                     {"conclusion": "neutral"}, {"conclusion": None}, {"status": None}):
            with self.subTest(over=over):
                self.assertFalse(merge_pr.has_authoritative_sourcery_review(
                    self.pr(), self.evidence([self.run_entry(**over)])))

    def test_malformed_payloads_fail_closed(self):
        for runs in ([None], ["str"], [7]):
            with self.subTest(runs=runs):
                self.assertFalse(merge_pr.has_authoritative_sourcery_review(
                    self.pr(), self.evidence(runs)))

    def test_unreadable_runs_block_rather_than_falling_back(self):
        ev = {"github_review_evidence": True, "head_oid": self.HEAD,
              "unresolved": 0, "unfixed": 0, "outdated_unfixed": 0}
        self.assertIsNone(merge_pr._sourcery_check(self.pr(), ev))
        self.assertFalse(merge_pr.has_authoritative_sourcery_review(self.pr(), ev))

    def test_unresolved_threads_still_block_a_clean_sourcery_run(self):
        ev = self.evidence([self.run_entry()])
        ev["unresolved"] = 1
        self.assertFalse(merge_pr.check_reviews(self.pr(), ev)[0])

    def test_gate_passes_end_to_end_on_clean_evidence(self):
        ok, message = merge_pr.check_reviews(self.pr(), self.evidence([self.run_entry()]))
        self.assertTrue(ok, message)
        self.assertIn("Sourcery review is complete", message)

    def test_enrichment_follows_the_assigned_service(self):
        with patch.object(merge_pr, "_with_sourcery_runs", return_value={"picked": "sourcery"}), \
             patch.object(merge_pr, "_with_coderabbit_status", return_value={"picked": "cr"}):
            self.assertEqual(merge_pr.with_service_evidence(self.pr(), 433, {}), {"picked": "sourcery"})
            cr = labelled("author:agent-1", "review:coderabbit")
            self.assertEqual(merge_pr.with_service_evidence(cr, 1, {}), {"picked": "cr"})


class SourceryCheckRunPaginationTests(unittest.TestCase):
    """#435: a partial check-run read must never look like complete evidence.

    ``total_count`` covers the whole reference while the endpoint caps its
    result set, so a short read can hide a second "Sourcery review" run outside
    the returned window - which would turn an ambiguous verdict into an
    apparently sole authoritative match and open the gate.
    """

    HEAD = "b" * 40

    def page(self, total, count, name="Sourcery review"):
        return {"total_count": total,
                "check_runs": [{"name": name} for _ in range(count)]}

    def read(self, payload):
        with patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"), \
             patch.object(merge_pr, "_gh_json", return_value=payload) as gh:
            return merge_pr._sourcery_check_runs("owner", "repo", self.HEAD), gh

    def test_reader_slurps_so_multi_page_json_stays_parseable(self):
        """Without --slurp, --paginate concatenates objects and page 2 is lost."""
        _, gh = self.read([self.page(1, 1)])
        self.assertIn("--slurp", gh.call_args[0][0])

    def test_all_pages_are_aggregated_when_the_total_is_proven(self):
        runs, _ = self.read([self.page(5, 2), self.page(5, 2), self.page(5, 1)])
        self.assertEqual(len(runs or []), 5)

    def test_single_page_and_empty_reference_still_read(self):
        """An empty reference reads as no evidence, not as an unreadable one."""
        self.assertEqual(len(self.read([self.page(1, 1)])[0]), 1)
        self.assertEqual(self.read([self.page(0, 0)])[0], [])

    def test_short_read_against_declared_total_fails_closed(self):
        """The capped/truncated case CodeRabbit flagged: 2 of 3 runs returned."""
        self.assertIsNone(self.read([self.page(3, 2)])[0])

    def test_over_long_read_against_declared_total_fails_closed(self):
        self.assertIsNone(self.read([self.page(1, 2)])[0])

    def test_totals_disagreeing_across_pages_fail_closed(self):
        self.assertIsNone(self.read([self.page(4, 2), self.page(9, 2)])[0])

    def test_missing_or_malformed_total_fails_closed(self):
        for total in (None, "5", 5.0, True, [5], float("nan")):
            with self.subTest(total=total):
                page = {"total_count": total, "check_runs": [{"name": "x"}]}
                self.assertIsNone(self.read([page])[0])

    def test_absent_total_key_fails_closed(self):
        self.assertIsNone(self.read([{"check_runs": []}])[0])

    def test_malformed_pages_and_run_lists_fail_closed(self):
        for payload in (None, {}, [], "pages", [None], ["page"], [[]],
                        [{"total_count": 0, "check_runs": None}],
                        [{"total_count": 1, "check_runs": {"name": "x"}}],
                        [self.page(2, 1), "page-2"]):
            with self.subTest(payload=payload):
                self.assertIsNone(self.read(payload)[0])

    def test_a_hidden_duplicate_run_can_no_longer_pass_as_the_sole_match(self):
        """The truncated page holds one run; the total says a second exists."""
        truncated = self.page(2, 1)
        self.assertIsNone(self.read([truncated])[0])
        # That payload is exactly what would have read as unique before the fix.
        self.assertIs(merge_pr._sourcery_unique_match(truncated["check_runs"]),
                      truncated["check_runs"][0])

    def test_unreadable_response_keeps_the_gate_closed(self):
        with patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"), \
             patch.object(merge_pr, "_gh_json", return_value=[self.page(3, 2)]):
            self.assertIsNone(merge_pr._with_sourcery_runs(
                433, {"github_review_evidence": True, "head_oid": self.HEAD}))

    def test_missing_head_is_never_queried(self):
        for head in (None, ""):
            with self.subTest(head=head):
                with patch.object(merge_pr, "_gh_json") as gh:
                    self.assertIsNone(merge_pr._sourcery_check_runs("o", "r", head))
                gh.assert_not_called()


class CodeAntEvidenceTests(unittest.TestCase):
    """#435: CodeAnt satisfies the gate only on producer-validated exact-head proof.

    CodeAnt is the one assigned service with two legitimate shapes of positive
    evidence: a Review object, and -- when a run finds nothing and so leaves no
    Review object behind -- its provider-owned rolling status comment. Both
    paths are exercised here, together with the ways each one is spoofable.
    """

    HEAD = "e" * 40
    PRIOR = "f" * 40

    def pr(self):
        pr = labelled("author:agent-1", "review:codeant")
        pr.update({"number": 434, "headRefOid": self.HEAD, "body": "Closes #413"})
        return pr

    def review(self, **over):
        entry = {
            "id": "codeant-review-1",
            "state": "COMMENTED",
            "body": "CodeAnt reviewed this head.",
            "submittedAt": "2026-08-20T10:00:00Z",
            "author": {"login": "codeant-ai", "__typename": "Bot"},
            "commit": {"oid": self.HEAD},
        }
        entry.update(over)
        return entry

    def record(self, **over):
        entry = {
            "label": "CodeAnt review",
            "commit": self.HEAD,
            "started": "2026-08-20T10:00:00Z",
            "finished": "2026-08-20T10:04:00Z",
            "done": True,
        }
        entry.update(over)
        return entry

    def status_comment(self, records, *, login="codeant-ai", typename="Bot",
                       body=None):
        if body is None:
            body = (f"### CodeAnt status\n{merge_pr.CODEANT_STATUS_MARKER_PREFIX}"
                    f"{json.dumps(records)}-->")
        return {"body": body, "author": {"login": login, "__typename": typename}}

    def evidence(self, *, reviews=(), comments=(), **over):
        ev = {"github_review_evidence": True, "head_oid": self.HEAD,
              "reviews": list(reviews), "codeant_status_comments": list(comments),
              "unresolved": 0, "unfixed": 0, "outdated_unfixed": 0}
        ev.update(over)
        return ev

    # -- Review-object path -------------------------------------------------

    def test_exact_head_review_object_is_authoritative(self):
        for state in ("COMMENTED", "APPROVED"):
            with self.subTest(state=state):
                ev = self.evidence(reviews=[self.review(state=state)])
                self.assertTrue(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_gate_passes_end_to_end_on_a_review_object(self):
        ok, message = merge_pr.check_reviews(
            self.pr(), self.evidence(reviews=[self.review()]))
        self.assertTrue(ok, message)
        self.assertIn("CodeAnt review is complete", message)

    def test_changes_requested_blocks_until_re_review(self):
        ev = self.evidence(reviews=[self.review(state="CHANGES_REQUESTED")])
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))
        ok, message = merge_pr.check_reviews(self.pr(), ev)
        self.assertFalse(ok)
        self.assertIn("requested changes", message)

    def test_another_account_naming_itself_codeant_is_not_codeant(self):
        """Only the ``codeant-ai`` Bot identity attests; a lookalike is history."""
        for author in ({"login": "codeant", "__typename": "Bot"},
                       {"login": "codeant-ai-reviews", "__typename": "Bot"},
                       {"login": "gillella", "__typename": "User"}):
            with self.subTest(author=author["login"]):
                ev = self.evidence(reviews=[self.review(author=author)])
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_codeant_login_on_a_non_bot_actor_blocks_both_paths(self):
        """A human who took the login is a spoof, not a fallback to status."""
        ev = self.evidence(
            reviews=[self.review(author={"login": "codeant-ai",
                                         "__typename": "User"})],
            comments=[self.status_comment([self.record()])])
        self.assertIs(merge_pr._codeant_latest_review(ev),
                      merge_pr.CODEANT_REVIEW_UNUSABLE)
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))
        ok, message = merge_pr.check_reviews(self.pr(), ev)
        self.assertFalse(ok)
        self.assertIn("untrustworthy Review object", message)

    def test_pending_review_at_head_blocks_trusted_status(self):
        """A run still in flight must not be overtaken by an older status row."""
        ev = self.evidence(reviews=[self.review(state="PENDING",
                                                submittedAt=None)],
                           comments=[self.status_comment([self.record()])])
        self.assertIs(merge_pr._codeant_latest_review(ev),
                      merge_pr.CODEANT_REVIEW_UNUSABLE)
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_malformed_current_head_review_blocks(self):
        for over in ({"id": ""}, {"id": 7}, {"submittedAt": None},
                     {"submittedAt": "not-a-time"}, {"body": None},
                     {"state": "COMMENTED", "body": "   "},
                     {"state": "APPROVED_MAYBE"}, {"commit": None},
                     {"commit": {"oid": "not-a-sha"}}):
            with self.subTest(over=over):
                ev = self.evidence(reviews=[self.review(**over)])
                self.assertIs(merge_pr._codeant_latest_review(ev),
                              merge_pr.CODEANT_REVIEW_UNUSABLE)
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_two_current_head_reviews_at_the_same_instant_are_ambiguous(self):
        ev = self.evidence(reviews=[self.review(),
                                    self.review(id="codeant-review-2",
                                                state="CHANGES_REQUESTED")])
        self.assertIs(merge_pr._codeant_latest_review(ev),
                      merge_pr.CODEANT_REVIEW_UNUSABLE)
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_newest_current_head_review_wins_over_an_earlier_one(self):
        ev = self.evidence(reviews=[
            self.review(state="CHANGES_REQUESTED",
                        submittedAt="2026-08-20T10:00:00Z"),
            self.review(id="codeant-review-2", state="APPROVED",
                        submittedAt="2026-08-20T11:00:00Z"),
        ])
        self.assertTrue(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_prior_head_review_is_history_not_current_evidence(self):
        review = self.review(commit={"oid": self.PRIOR})
        self.assertIsNone(
            merge_pr._codeant_latest_review(self.evidence(reviews=[review])))
        self.assertFalse(merge_pr.has_authoritative_codeant_review(
            self.pr(), self.evidence(reviews=[review])))
        # ... and it must not veto a trusted status record for the live head.
        self.assertTrue(merge_pr.has_authoritative_codeant_review(
            self.pr(), self.evidence(reviews=[review],
                                     comments=[self.status_comment(
                                         [self.record()])])))

    def test_dismissed_review_falls_through_to_status(self):
        ev = self.evidence(reviews=[self.review(state="DISMISSED")],
                           comments=[self.status_comment([self.record()])])
        self.assertTrue(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    # -- Status-record path -------------------------------------------------

    def test_clean_run_status_record_is_authoritative(self):
        ev = self.evidence(comments=[self.status_comment([self.record()])])
        self.assertTrue(merge_pr.has_authoritative_codeant_review(self.pr(), ev))
        ok, message = merge_pr.check_reviews(self.pr(), ev)
        self.assertTrue(ok, message)
        self.assertIn("CodeAnt clean-review status is complete", message)

    def test_status_history_may_carry_prior_heads_alongside_the_live_one(self):
        ev = self.evidence(comments=[self.status_comment([
            self.record(commit=self.PRIOR, done=False),
            self.record(),
        ])])
        self.assertTrue(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_spoofed_status_author_is_refused(self):
        for login, typename in (("gillella", "User"), ("codeant", "Bot"),
                                ("codeant-ai", "User"),
                                ("codeant-ai", "Organization")):
            with self.subTest(login=login, typename=typename):
                ev = self.evidence(comments=[self.status_comment(
                    [self.record()], login=login, typename=typename)])
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_unfinished_or_failed_record_at_head_blocks(self):
        for over in ({"done": False}, {"done": "true"}, {"finished": None},
                     {"started": "whenever"}, {"label": "   "}, {"label": 7}):
            with self.subTest(over=over):
                ev = self.evidence(comments=[self.status_comment(
                    [self.record(**over)])])
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_unexpected_record_shape_at_head_blocks(self):
        extra = self.record()
        extra["verdict"] = "clean"
        missing = self.record()
        del missing["finished"]
        for record in (extra, missing):
            with self.subTest(keys=sorted(record)):
                ev = self.evidence(comments=[self.status_comment([record])])
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_records_that_name_no_live_head_are_missing_evidence(self):
        for over in ({"commit": self.PRIOR}, {"commit": "abc123"},
                     {"commit": None}, {"commit": self.HEAD[:39]}):
            with self.subTest(over=over):
                ev = self.evidence(comments=[self.status_comment(
                    [self.record(**over)])])
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_head_comparison_is_case_insensitive(self):
        ev = self.evidence(comments=[self.status_comment(
            [self.record(commit=self.HEAD.upper())])])
        self.assertTrue(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_two_trusted_status_comments_are_ambiguous(self):
        ev = self.evidence(comments=[self.status_comment([self.record()]),
                                     self.status_comment([self.record()])])
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_duplicated_marker_in_one_comment_yields_nothing_usable(self):
        marker = (f"{merge_pr.CODEANT_STATUS_MARKER_PREFIX}"
                  f"{json.dumps([self.record()])}-->")
        ev = self.evidence(comments=[self.status_comment(None,
                                                         body=marker + marker)])
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_malformed_marker_payloads_yield_nothing_usable(self):
        for payload in ("not json", "{}", '"clean"', "[", "null"):
            with self.subTest(payload=payload):
                body = (f"{merge_pr.CODEANT_STATUS_MARKER_PREFIX}{payload}-->")
                ev = self.evidence(comments=[self.status_comment(None, body=body)])
                self.assertIsNone(merge_pr._codeant_status_records(body))
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_absent_or_malformed_status_collection_blocks(self):
        for comments in (None, "nope", 7):
            with self.subTest(comments=comments):
                ev = self.evidence()
                ev["codeant_status_comments"] = comments
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))
        ev = self.evidence()
        del ev["codeant_status_comments"]
        self.assertFalse(merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    def test_no_evidence_of_any_kind_blocks_with_recovery_wording(self):
        ok, message = merge_pr.check_reviews(self.pr(), self.evidence())
        self.assertFalse(ok)
        self.assertIn("CodeAnt has not supplied", message)

    def test_unknown_head_blocks_every_path(self):
        for head in (None, "", 7):
            with self.subTest(head=head):
                ev = self.evidence(reviews=[self.review()],
                                   comments=[self.status_comment(
                                       [self.record()])],
                                   head_oid=head)
                self.assertFalse(
                    merge_pr.has_authoritative_codeant_review(self.pr(), ev))

    # -- Interaction with the shared gates ----------------------------------

    def test_unresolved_codeant_threads_still_block_a_clean_status(self):
        ev = self.evidence(comments=[self.status_comment([self.record()])])
        ev["service_threads"] = {"codeant": {"unresolved": 1, "unfixed": 0,
                                             "outdated_unfixed": 0}}
        ok, message = merge_pr.check_reviews(self.pr(), ev)
        self.assertFalse(ok)
        self.assertIn("unresolved review thread", message)

    def test_enrichment_needs_no_second_round_trip_for_codeant(self):
        """Fetching CodeRabbit's status here would buy evidence nobody reads."""
        with patch.object(merge_pr, "_with_coderabbit_status",
                          return_value={"picked": "cr"}), \
             patch.object(merge_pr, "_with_sourcery_runs",
                          return_value={"picked": "sourcery"}):
            self.assertEqual(
                merge_pr.with_service_evidence(self.pr(), 434, {"picked": None}),
                {"picked": None})

    def test_codeant_is_recognised_as_a_fallback_authority(self):
        self.assertEqual(merge_pr.assigned_review_service(self.pr()), "codeant")
        self.assertIn("review:codeant", merge_pr.REVIEW_SERVICE_LABELS)
class TerminalMergeLeaseTests(unittest.TestCase):
    """#344: a merged branch name is spent; later pushes are stale, not new work."""

    def test_merged_branch_resolves_to_a_lease(self):
        rows = [{"number": 425, "headRefName": "feat/x",
                 "headRefOid": "a" * 40, "mergeCommit": {"oid": "b" * 40},
                 "author": {"login": "someone"}, "mergedAt": "2026-08-25T00:00:00Z"}]
        with patch.object(common, "run_gh_json", return_value=rows):
            lease = common.terminal_merge_lease("feat/x")
        self.assertEqual(lease["pr"], 425)
        self.assertEqual(lease["gated_sha"], "a" * 40)
        self.assertEqual(lease["merged_sha"], "b" * 40)
        self.assertEqual(lease["holder"], "someone")

    def test_never_merged_branch_has_no_lease(self):
        with patch.object(common, "run_gh_json", return_value=[]):
            self.assertIsNone(common.terminal_merge_lease("feat/live"))

    def test_unreadable_lookup_fails_closed(self):
        """Cannot-tell must block continuation, not permit it."""
        with patch.object(common, "run_gh_json", return_value=None):
            lease = common.terminal_merge_lease("feat/x")
        self.assertTrue(lease["unreadable"])
        self.assertIn("refusing", common.terminal_lease_refusal(lease, "reuse").lower())

    def test_several_merged_prs_for_one_branch_fail_closed(self):
        rows = [{"number": n, "headRefName": "feat/x", "headRefOid": "a" * 40,
                 "mergeCommit": {"oid": "b" * 40}, "author": {"login": "x"}} for n in (1, 2)]
        with patch.object(common, "run_gh_json", return_value=rows):
            lease = common.terminal_merge_lease("feat/x")
        self.assertEqual(lease["ambiguous"], [1, 2])

    def test_partial_name_match_is_not_a_lease(self):
        rows = [{"number": 1, "headRefName": "feat/x-other", "headRefOid": "a" * 40,
                 "mergeCommit": {"oid": "b" * 40}, "author": {"login": "x"}}]
        with patch.object(common, "run_gh_json", return_value=rows):
            self.assertIsNone(common.terminal_merge_lease("feat/x"))

    def test_malformed_payload_fails_closed(self):
        """A non-list payload is unknown, not empty."""
        for payload in ({"nope": 1}, "rows", 7):
            with self.subTest(payload=payload):
                with patch.object(common, "run_gh_json", return_value=payload):
                    self.assertTrue(common.terminal_merge_lease("feat/x")["unreadable"])

    def test_a_full_page_is_treated_as_unreadable(self):
        """A capped result may be hiding another merged PR."""
        rows = [{"number": n, "headRefName": "feat/x", "headRefOid": "a" * 40,
                 "mergeCommit": {"oid": "b" * 40}, "author": {"login": "x"}}
                for n in range(20)]
        with patch.object(common, "run_gh_json", return_value=rows):
            self.assertTrue(common.terminal_merge_lease("feat/x")["unreadable"])

    def test_blank_branch_has_no_lease(self):
        for value in ("", None, 7):
            with self.subTest(value=value):
                self.assertIsNone(common.terminal_merge_lease(value))


class StaleWriterEscalationTests(unittest.TestCase):
    """#344 regression, modelled on hermes PR #89."""

    PR = {"number": 89, "author": {"login": "codex-1"}}

    def _detect(self, ls_remote_out, code=0):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "run_cmd", return_value=(code, ls_remote_out, "")):
            return merge_pr.detect_stale_writer(
                "/repo", self.PR, "feat/issue-87", "0" * 40, "o/r",
            )

    def test_deleted_branch_is_clean(self):
        ok, message = self._detect("")
        self.assertTrue(ok)
        self.assertIn("No recreated branch", message)

    def test_branch_still_at_gated_head_is_clean(self):
        ok, _ = self._detect(f"{'0' * 40}\trefs/heads/feat/issue-87")
        self.assertTrue(ok)

    def test_recreated_branch_escalates_with_every_operator_fact(self):
        ok, message = self._detect(f"{'3' * 40}\trefs/heads/feat/issue-87")
        self.assertFalse(ok)
        self.assertIn("[P0] STALE WRITER", message)
        self.assertIn("89", message)          # PR
        self.assertIn("0" * 40, message)      # gated SHA
        self.assertIn("3" * 40, message)      # new SHA
        self.assertIn("feat/issue-87", message)   # branch
        self.assertIn("codex-1", message)     # holder

    def test_escalation_never_deletes_the_orphan(self):
        _, message = self._detect(f"{'3' * 40}\trefs/heads/feat/issue-87")
        self.assertIn("NOT deleted", message)
        self.assertIn("preserved for inspection", message)

    def test_unreadable_remote_keeps_close_out_incomplete(self):
        """Fail closed: an unreadable remote is when a stale write is likeliest."""
        ok, message = self._detect("", code=1)
        self.assertFalse(ok)
        self.assertIn("cannot rule out", message)

    def test_close_out_runs_the_stale_writer_step(self):
        source = inspect.getsource(merge_pr)
        self.assertIn('("stale writer", lambda: detect_stale_writer(', source)


class TerminalLeaseRecordingTests(unittest.TestCase):
    """#344 criterion 1/4: the merge records a lease that no reap path clears."""

    def test_label_is_derived_from_the_gated_sha(self):
        self.assertEqual(common.terminal_lease_label("A" * 40), "terminal-lease:" + "a" * 12)

    def test_label_round_trips_through_the_reader(self):
        label = common.terminal_lease_label("abcdef1234567890")
        self.assertEqual(common.terminal_lease_sha([label]), "abcdef123456")

    def test_reader_ignores_unrelated_labels(self):
        self.assertIsNone(common.terminal_lease_sha(["status:done", "agent:x", None, 7]))

    def test_recording_stamps_the_pr(self):
        with patch.object(merge_pr, "ensure_label", return_value=True), \
             patch.object(merge_pr, "run_cmd", return_value=(0, "", "")) as run:
            ok, message = merge_pr.record_terminal_lease(89, "a" * 40)
        self.assertTrue(ok)
        self.assertIn("terminal-lease:" + "a" * 12, message)
        self.assertIn("--add-label", run.call_args[0][0])

    def test_recording_failure_warns_without_failing_a_completed_merge(self):
        """The marker is convenience; the derived lease is the protection."""
        with patch.object(merge_pr, "ensure_label", return_value=True), \
             patch.object(merge_pr, "run_cmd", return_value=(1, "", "denied")):
            ok, message = merge_pr.record_terminal_lease(89, "a" * 40)
        self.assertTrue(ok, "a failed marker must not strand a completed merge")
        self.assertIn("[WARN]", message)
        self.assertIn("derived merged-PR lease still blocks", message)

    def test_close_out_records_the_lease(self):
        self.assertIn('("terminal lease", lambda: record_terminal_lease(',
                      inspect.getsource(merge_pr))


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
        self.assertIn("coderabbit", msg.lower())

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
    def coderabbit_evidence(self, *, head="a" * 40, state="COMMENTED",
                            login="coderabbitai[bot]", body="Review complete."):
        return coderabbit_evidence(head, state=state, login=login, body=body)

    @staticmethod
    def coderabbit_pr(*labels):
        return coderabbit_pr(*labels)

    @staticmethod
    def coderabbit_checkrun_status():
        return [{
            "__typename": "CheckRun",
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "checkSuite": {"app": {"slug": "coderabbitai"}},
        }]

    @staticmethod
    def coderabbit_status_context(**overrides):
        """One authentic, current-head CodeRabbit StatusContext.

        Defaults to the genuine completion shape so each case below overrides
        exactly the one field it is about.
        """
        context = {
            "type": "StatusContext",
            "context": "CodeRabbit",
            "state": "SUCCESS",
            "creator": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            "description": "Review completed",
        }
        context.update(overrides)
        return [context]

    @classmethod
    def coderabbit_status_payload(cls, head):
        return {"data": {"repository": {"pullRequest": {
            "headRefOid": head,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "contexts": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": cls.coderabbit_checkrun_status(),
                },
            }}}]},
        }}}}

    def no_findings_full_review_evidence(
        self,
        *,
        head="a" * 40,
        request_time="2026-08-23T22:58:00Z",
        review_time="2026-08-23T22:59:55Z",
        completion_time="2026-08-23T23:00:10Z",
        head_commit_time="2026-08-23T22:57:00Z",
        request_author="gillella",
        request_type="User",
        completion_author="coderabbitai[bot]",
        completion_type="Bot",
        include_request=True,
        include_completion=True,
        extra_comments=None,
    ):
        evidence = self.coderabbit_evidence(head=head, body="", state="COMMENTED")
        evidence["reviews"][0]["submittedAt"] = review_time
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        evidence["head_commit_committed_at"] = head_commit_time
        comments = []
        if include_request:
            comments.append({
                "body": "@coderabbitai full review",
                "createdAt": request_time,
                "author": {"login": request_author, "__typename": request_type},
            })
        if include_completion:
            comments.append({
                "body": "Full review finished.",
                "createdAt": completion_time,
                "author": {"login": completion_author, "__typename": completion_type},
            })
        comments.extend([] if extra_comments is None else extra_comments)
        evidence["coderabbit_full_review_comments"] = comments
        return evidence

    def test_coderabbit_current_head_substantive_review_is_sole_authority(self):
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1", "reviewed-by:agent-2"),
            self.coderabbit_evidence(),
        )
        self.assertTrue(ok, msg)
        self.assertIn("CodeRabbit", msg)

    def test_coderabbit_remains_the_default_authority(self):
        pr = self.coderabbit_pr("author:agent-1", "review:coderabbit")
        pr["body"] = "Closes #341"
        self.assertEqual(merge_pr.assigned_review_service(pr), "coderabbit")
        ok, msg = merge_pr.check_reviews(pr, self.coderabbit_evidence())
        self.assertTrue(ok, msg)
        self.assertFalse(merge_pr.check_reviews(pr, None)[0])

    def test_sourcery_is_recognised_as_a_fallback_authority(self):
        """#435: an explicitly reassigned PR routes to Sourcery, not to nothing."""
        pr = labelled("author:agent-1", "review:sourcery")
        pr["body"] = "Closes #341"
        self.assertEqual(merge_pr.assigned_review_service(pr), "sourcery")
        self.assertFalse(merge_pr.check_reviews(pr, None)[0])

    def test_remediation_head_status_cannot_reuse_prior_head_review(self):
        old_head = "a" * 40
        new_head = "b" * 40
        evidence = self.coderabbit_evidence(head=old_head, state="APPROVED", body="")
        evidence["head_oid"] = new_head
        evidence["coderabbit_status"] = [{
            "type": "StatusContext",
            "context": "CodeRabbit",
            "state": "SUCCESS",
            "creator": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }]
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_current_head_status_without_prior_substantive_coderabbit_review_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["reviews"] = [{
            "id": "peer-review",
            "state": "APPROVED",
            "submittedAt": "2026-08-23T19:00:00Z",
            "body": "Peer approved.",
            "author": {"login": "human-reviewer", "__typename": "User"},
            "commit": {"oid": evidence["head_oid"]},
        }]
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_latest_completed_coderabbit_changes_requested_blocks_even_with_current_head_status(self):
        head = "b" * 40
        evidence = self.coderabbit_evidence(head="a" * 40, state="APPROVED")
        evidence["head_oid"] = head
        evidence["reviews"].append({
            "id": "coderabbit-changes-requested",
            "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-08-23T21:00:00Z",
            "body": "Blocking issue remains.",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            "commit": {"oid": "a" * 40},
        })
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_ambiguous_newest_prior_coderabbit_reviews_fail_closed_even_with_current_head_status(self):
        evidence = self.coderabbit_evidence(head="a" * 40, state="APPROVED")
        evidence["reviews"].append({
            "id": "coderabbit-review-2",
            "state": "CHANGES_REQUESTED",
            "submittedAt": evidence["reviews"][0]["submittedAt"],
            "body": "Blocking issue remains.",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            "commit": {"oid": "a" * 40},
        })
        evidence["head_oid"] = "b" * 40
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("unambiguous latest review verdict", msg)

    def test_graphql_coderabbit_login_without_bot_suffix_is_accepted(self):
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.coderabbit_evidence(login="coderabbitai"),
        )
        self.assertTrue(ok, msg)

    def test_coding_agent_review_cannot_satisfy_gate(self):
        ok, msg = _gate(
            labelled("author:agent-1", "reviewed-by:agent-2",
                     reviews=[{
                         "id": "peer", "state": "APPROVED",
                         "submittedAt": "2026-08-23T20:00:00Z",
                         "body": "Approved.",
                         "author": {"login": "human", "__typename": "User"},
                         "commit": {"oid": "a" * 40},
                     }]),
            head_oid="a" * 40,
        )
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_spoofed_coderabbit_login_fails_closed(self):
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.coderabbit_evidence(login="coderabbit-reviewer"),
        )
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_pending_coderabbit_review_fails_closed(self):
        pending = self.coderabbit_evidence()
        pending["reviews"].append(dict(
            pending["reviews"][0], id="coderabbit-pending", state="PENDING",
        ))
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), pending,
        )
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_success_context_from_unknown_producer_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": {"login": "spoof", "__typename": "User"},
        }]
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_completed_description_status_passes(self):
        """The genuine verdict still merges: no currently mergeable PR regresses."""
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context()
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )
        self.assertTrue(ok, msg)
        self.assertIn("CodeRabbit", msg)

    def test_rate_limited_description_status_fails_closed(self):
        """The reported bug: state=SUCCESS meaning 'I did not review this head'."""
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context(
            description="Review rate limited",
        )
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )
        self.assertFalse(ok)
        # The refusal already promised rate-limited evidence blocks merge;
        # this binds that wording to behaviour the code now implements.
        self.assertIn("rate-limited", msg)

    def test_label_skipped_description_status_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context(
            description="Review skipped: excluded by label configuration",
        )
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_missing_description_status_fails_closed(self):
        evidence = self.coderabbit_evidence()
        status = self.coderabbit_status_context()
        del status[0]["description"]
        evidence["coderabbit_status"] = status
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_empty_description_status_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context(
            description="   ",
        )
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_non_string_description_status_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context(
            description={"text": "Review completed"},
        )
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_unknown_description_status_fails_closed(self):
        """A future wording is not silently admitted by a substring rule."""
        evidence = self.coderabbit_evidence()
        evidence["coderabbit_status"] = self.coderabbit_status_context(
            description="Review could not be completed",
        )
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_empty_body_commented_exact_head_review_fails_closed_even_with_successful_status(self):
        evidence = self.coderabbit_evidence(body="", state="COMMENTED")
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }]
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_empty_body_approved_exact_head_review_passes_with_successful_status(self):
        evidence = self.coderabbit_evidence(body="", state="APPROVED")
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )
        self.assertTrue(ok, msg)
        self.assertIn("CodeRabbit", msg)

    def test_valid_no_findings_full_review_passes(self):
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(),
        )
        self.assertTrue(ok, msg)

    def test_automatic_empty_review_without_full_review_request_fails_closed(self):
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(include_request=False),
        )[0])

    def test_non_substantive_empty_commented_review_does_not_block_later_approval(self):
        evidence = self.no_findings_full_review_evidence(
            include_request=False,
            review_time="2026-08-23T20:00:00Z",
        )
        evidence["reviews"].append({
            "id": "coderabbit-approved",
            "state": "APPROVED",
            "submittedAt": "2026-08-23T21:00:00Z",
            "body": "",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            "commit": {"oid": evidence["head_oid"]},
        })
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertTrue(ok, msg)
        self.assertIn("CodeRabbit", msg)

    def test_newer_empty_commented_review_without_proof_blocks_older_approval(self):
        evidence = self.coderabbit_evidence(body="", state="APPROVED")
        evidence["reviews"].append({
            "id": "coderabbit-empty-comment",
            "state": "COMMENTED",
            "submittedAt": "2026-08-23T21:00:00Z",
            "body": "",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
            "commit": {"oid": evidence["head_oid"]},
        })
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_only_non_substantive_empty_commented_history_still_fails_closed(self):
        evidence = self.no_findings_full_review_evidence(include_request=False)
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_empty_review_without_completion_comment_fails_closed(self):
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(include_completion=False),
        )[0])

    def test_spoofed_full_review_completion_author_fails_closed(self):
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(
                completion_author="octocat",
                completion_type="User",
            ),
        )[0])

    def test_completion_before_request_fails_closed(self):
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(
                request_time="2026-08-23T23:00:00Z",
                completion_time="2026-08-23T22:59:00Z",
            ),
        )[0])

    def test_request_before_current_head_commit_fails_closed(self):
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"),
            self.no_findings_full_review_evidence(
                head_commit_time="2026-08-23T22:58:30Z",
                request_time="2026-08-23T22:58:00Z",
            ),
        )[0])

    def test_no_findings_chronology_rejects_timezone_naive_values(self):
        cases = (
            {"request_time": "2026-08-23T22:58:00"},
            {"completion_time": "2026-08-23T23:00:10"},
            {"head_commit_time": "2026-08-23T22:57:00"},
        )
        for override in cases:
            with self.subTest(override=override):
                self.assertFalse(merge_pr.check_reviews(
                    self.coderabbit_pr("author:agent-1"),
                    self.no_findings_full_review_evidence(**override),
                )[0])

    def test_tied_full_review_request_events_are_ambiguous(self):
        evidence = self.no_findings_full_review_evidence(extra_comments=[{
            "body": "@coderabbitai full review",
            "createdAt": "2026-08-23T22:58:00Z",
            "author": {"login": "other-user", "__typename": "User"},
        }])
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_tied_full_review_completion_events_are_ambiguous(self):
        evidence = self.no_findings_full_review_evidence(extra_comments=[{
            "body": "Full review finished.",
            "createdAt": "2026-08-23T23:00:10Z",
            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }])
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_whitespace_body_commented_exact_head_review_fails_closed_even_with_successful_status(self):
        evidence = self.coderabbit_evidence(body="   \n\t", state="COMMENTED")
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        }]
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_whitespace_body_approved_exact_head_review_passes_with_successful_status(self):
        evidence = self.coderabbit_evidence(body="   \n\t", state="APPROVED")
        evidence["coderabbit_status"] = self.coderabbit_checkrun_status()
        ok, msg = merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )
        self.assertTrue(ok, msg)
        self.assertIn("CodeRabbit", msg)

    def test_null_status_creator_fails_even_with_recognized_review(self):
        evidence = self.coderabbit_evidence(body="Review complete.")
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": None,
        }]
        ok, msg = merge_pr.check_reviews(self.coderabbit_pr("author:agent-1"), evidence)
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_non_null_spoof_status_creator_fails_with_recognized_review(self):
        evidence = self.coderabbit_evidence(body="")
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": {"login": "coderabbit-status", "__typename": "Bot"},
        }]
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_null_status_creator_without_recognized_review_fails_closed(self):
        evidence = self.coderabbit_evidence(login="not-coderabbit", body="")
        evidence["coderabbit_status"] = [{
            "type": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS",
            "creator": None,
        }]
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_missing_failed_rate_limited_or_ambiguous_check_fails_closed(self):
        good = self.coderabbit_checkrun_status()[0]
        cases = (
            [],
            [dict(good, conclusion="FAILURE")],
            [dict(good, conclusion="NEUTRAL")],
            [dict(good, status="IN_PROGRESS", conclusion="")],
            [dict(good), dict(good)],
        )
        for checks in cases:
            with self.subTest(checks=checks):
                evidence = self.coderabbit_evidence()
                evidence["coderabbit_status"] = checks
                pr = self.coderabbit_pr("author:agent-1")
                self.assertFalse(merge_pr.check_reviews(pr, evidence)[0])

    def test_github_review_evidence_without_authoritative_status_never_uses_pr_rollup(self):
        evidence = self.coderabbit_evidence()
        evidence.pop("coderabbit_status")
        evidence["github_review_evidence"] = True
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_tied_newest_current_head_coderabbit_reviews_are_ambiguous(self):
        evidence = self.coderabbit_evidence()
        second = dict(evidence["reviews"][0], id="coderabbit-review-2")
        evidence["reviews"].append(second)
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_distinct_timestamp_newest_current_head_coderabbit_review_wins(self):
        evidence = self.coderabbit_evidence()
        evidence["reviews"].append(dict(
            evidence["reviews"][0], id="coderabbit-review-2",
            submittedAt="2026-08-23T21:00:00Z",
        ))
        self.assertTrue(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_invalid_newest_current_head_coderabbit_review_fails_closed(self):
        evidence = self.coderabbit_evidence()
        evidence["reviews"].append(dict(
            evidence["reviews"][0], id="coderabbit-review-2",
            submittedAt="not-a-time",
        ))
        self.assertFalse(merge_pr.check_reviews(
            self.coderabbit_pr("author:agent-1"), evidence,
        )[0])

    def test_github_review_evidence_without_authoritative_status_fails_closed(self):
        pr = self.coderabbit_pr("author:agent-1")
        evidence = self.coderabbit_evidence()
        evidence.pop("coderabbit_status")
        evidence["github_review_evidence"] = True
        self.assertFalse(merge_pr.check_reviews(pr, evidence)[0])

    def test_missing_assigned_review_service_label_fails_closed(self):
        pr = {
            "author": {"login": "gillella"},
            "reviews": [{
                "id": "default-review",
                "state": "APPROVED",
                "submittedAt": "2026-01-01T00:00:00Z",
                "author": {"login": "gillella"},
            }],
            "labels": [{"name": "author:agent-1"}],
            "statusCheckRollup": [{
                "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
            }],
        }
        ok, msg = merge_pr.check_reviews(pr, self.coderabbit_evidence())
        self.assertFalse(ok)
        self.assertIn("review:", msg)

    def test_mixed_review_labels_fail_closed(self):
        pr = self.coderabbit_pr("author:agent-1", "review:coderabbit", "review:sourcery")
        ok, msg = merge_pr.check_reviews(pr, self.coderabbit_evidence())
        self.assertFalse(ok)
        self.assertIn("exactly one", msg)

    def test_review_authority_does_not_duplicate_the_issue_link_gate(self):
        pr = self.coderabbit_pr("author:agent-1")
        pr["body"] = "No issue link here."
        self.assertEqual(merge_pr.assigned_review_service(pr), "coderabbit")
        ok, msg = merge_pr.check_reviews(pr, self.coderabbit_evidence())
        self.assertTrue(ok, msg)
        self.assertFalse(merge_pr.check_issue_link(pr)[0])

    def test_legacy_unknown_and_duplicate_review_labels_fail_closed(self):
        # review:sourcery and review:codeant are recognised fallbacks since
        # #435 and are covered separately; every other shape still fails closed.
        cases = (
            ("review:manual",),
            ("review:coderabbit", "review:coderabbit"),
            ("review:coderabbit", "review:sourcery"),
            ("review:sourcery", "review:codeant"),
        )
        for labels in cases:
            with self.subTest(labels=labels):
                pr = self.coderabbit_pr("author:agent-1", *labels)
                self.assertIsNone(merge_pr.assigned_review_service(pr))
                ok, msg = merge_pr.check_reviews(pr, self.coderabbit_evidence())
                self.assertFalse(ok)
                self.assertIn("review:coderabbit", msg)

    def test_authoritative_status_propagates_coderabbit_loader_failure(self):
        evidence = {"github_review_evidence": True, "head_oid": "a" * 40}
        with patch.object(merge_pr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(merge_pr, "_coderabbit_status_evidence", return_value=None):
            self.assertIsNone(merge_pr._with_coderabbit_status(383, evidence))


class RetiredAgentReviewAuthorityTests(unittest.TestCase):
    """#414: `review:agent` is no longer an authority a PR can be merged on.

    The emergency path needed an operator assignment comment, a reviewer
    identity stamped as (id, family), a substantive exact-head GitHub review,
    and a matching `aru-agent-review:v1` completion marker. Nothing writes any
    of that now, so the only way to reach the old verdict would be forged
    evidence. The gate therefore refuses the label outright.
    """

    HEAD = "a" * 40

    def evidence(self, **overrides):
        evidence = {
            "head_oid": self.HEAD,
            "head_commit_committed_at": "2026-08-25T10:00:00Z",
            "unresolved": 0, "unfixed": 0, "outdated_unfixed": 0,
            "reviews": [{
                "id": "agent-review", "state": "COMMENTED",
                "submittedAt": "2026-08-25T10:02:00Z",
                "body": "No findings after exact-head review.",
                "author": {"login": "gillella", "__typename": "User"},
                "commit": {"oid": self.HEAD},
            }],
            "agent_review_attestations": [{
                "agent": "agent-2", "completed_at": "2026-08-25T10:03:00Z",
                "disposition": "no-findings", "family": "openai",
                "head": self.HEAD, "status": "completed",
                "github_login": "gillella",
            }],
            "agent_review_marker_errors": 0,
        }
        evidence.update(overrides)
        return evidence

    def test_review_agent_is_recognised_only_to_be_refused(self):
        """The gate reads the label so it can name the way out, not accept it."""
        pr = labelled("author:agent-1", "family:anthropic", "review:agent",
                      "reviewed-by:agent-2", "reviewer-family:agent-2:openai")
        self.assertEqual(merge_pr.assigned_review_service(pr), "agent")
        ok, message = merge_pr.check_reviews(pr, self.evidence())
        self.assertFalse(ok)
        self.assertIn("retired", message)
        self.assertIn("review:coderabbit", message)
        self.assertFalse(
            merge_pr.has_authoritative_assigned_review(pr, self.evidence()))

    def test_a_retired_assignment_costs_no_evidence_round_trip(self):
        pr = labelled("author:agent-1", "review:agent")
        evidence = self.evidence()
        with patch.object(merge_pr, "_with_coderabbit_status") as coderabbit, \
             patch.object(merge_pr, "_with_sourcery_runs") as sourcery:
            self.assertIs(
                merge_pr.with_service_evidence(pr, 9, evidence), evidence)
        coderabbit.assert_not_called()
        sourcery.assert_not_called()

    def test_forged_completion_evidence_cannot_merge_a_coderabbit_pr(self):
        """The retired marker must not become a second, weaker oracle."""
        pr = labelled("author:agent-1", "family:anthropic",
                      "reviewed-by:agent-2", "reviewer-family:agent-2:openai")
        ok, message = merge_pr.check_reviews(pr, self.evidence())
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", message)

    def test_no_agent_thread_bucket_masquerades_as_a_service(self):
        payload = {"unresolved": 3,
                   "service_threads": {"coderabbit": {"unresolved": 0}}}
        counts = merge_pr._service_thread_counts(payload, "agent")
        # Falls back to the whole-evidence dict rather than borrowing another
        # service's bucket, so the retired label reads the outer counts.
        self.assertIs(counts, payload)


class CodeRabbitStatusEvidenceTests(unittest.TestCase):
    @patch.object(merge_pr, "_gh_json")
    def test_rest_read_failure_rejects_status_payload(self, gh_json):
        gh_json.side_effect = [None, {"total_count": 0, "statuses": []}]
        self.assertIsNone(merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123"))

    @patch.object(merge_pr, "_gh_json")
    def test_missing_context_total_count_rejects_status_payload(self, gh_json):
        gh_json.side_effect = [
            {"check_runs": []},
            {"total_count": 0, "statuses": []},
        ]
        self.assertIsNone(merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123"))

    @patch.object(merge_pr, "_gh_json")
    def test_truncated_rest_contexts_are_rejected(self, gh_json):
        gh_json.side_effect = [
            {"total_count": 2, "check_runs": [{
                "name": "CodeRabbit", "status": "completed",
                "conclusion": "success", "app": {"slug": "coderabbitai"},
            }]},
            {"total_count": 0, "statuses": []},
        ]
        self.assertIsNone(merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123"))

    @patch.object(merge_pr, "_gh_json")
    def test_truncated_rest_statuses_are_rejected(self, gh_json):
        gh_json.side_effect = [
            {"total_count": 0, "check_runs": []},
            {"total_count": 2, "statuses": [{
                "context": "CI", "state": "success",
                "creator": {"login": "github-actions[bot]", "type": "Bot"},
            }]},
        ]
        self.assertIsNone(merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123"))

    @patch.object(merge_pr, "_gh_json")
    def test_rest_statuses_are_mapped_to_typed_merge_evidence(self, gh_json):
        coderabbit = {
            "__typename": "CheckRun",
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "checkSuite": {"app": {"slug": "coderabbitai"}},
        }
        other = {
            "__typename": "StatusContext",
            "context": "CI",
            "state": "SUCCESS",
            "creator": {"login": "github-actions[bot]", "__typename": "Bot"},
        }
        gh_json.side_effect = [
            {"total_count": 1, "check_runs": [{
                "name": "CodeRabbit", "status": "completed",
                "conclusion": "success", "app": {"slug": "coderabbitai"},
            }]},
            {"total_count": 1, "statuses": [{
                "context": "CI", "state": "success",
                "creator": {"login": "github-actions[bot]", "type": "Bot"},
            }]},
            {"head": {"sha": "head123"}},
        ]

        self.assertEqual(
            merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123"),
            [coderabbit, other],
        )
        self.assertTrue(all("graphql" not in call.args[0] for call in gh_json.call_args_list))

    @patch.object(merge_pr, "_gh_json")
    def test_status_snapshot_rejects_concurrent_head_change(self, gh_json):
        gh_json.side_effect = [
            {"total_count": 0, "check_runs": []},
            {"total_count": 0, "statuses": []},
            {"head": {"sha": "new-head"}},
        ]
        self.assertIsNone(
            merge_pr._coderabbit_status_evidence("owner", "repo", 17, "head123")
        )



def labelled(*names, reviews=None, pr_login="gillella", review_login="gillella"):
    """A linked PR whose default reviews come from the same GitHub account."""
    labels = list(names)
    if not any(name.startswith("review:") for name in labels):
        labels.append("review:coderabbit")
    default = [{
        "id": "default-review",
        "state": "APPROVED",
        "submittedAt": "2026-01-01T00:00:00Z",
        "author": {"login": review_login},
    }]
    return {
        "author": {"login": pr_login},
        "reviews": reviews if reviews is not None else default,
        "labels": [{"name": n} for n in labels],
        "body": "Closes #1",
    }


def coderabbit_evidence(head="gated-sha", *, state="COMMENTED",
                        login="coderabbitai[bot]", body="Review complete."):
    return {
        "head_oid": head, "unresolved": 0, "unfixed": 0,
        "outdated_unfixed": 0, "withdrawn": 0, "reviewed_head": False,
        "coderabbit_status": [{
            "__typename": "CheckRun",
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "checkSuite": {"app": {"slug": "coderabbitai"}},
        }],
        "reviews": [{
            "id": "coderabbit-review", "state": state,
            "submittedAt": "2026-08-23T20:00:00Z", "body": body,
            "author": {"login": login, "__typename": "Bot"},
            "commit": {"oid": head},
        }],
    }


def coderabbit_pr(*labels):
    pr = labelled(*labels)
    pr["statusCheckRollup"] = [{
        "name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
    }]
    return pr


def _pr(
    state="CLEAN", mergeable="MERGEABLE", base="main", head="deadbeef",
    number=370, base_oid="base-tip",
):
    return {
        "number": number,
        "mergeStateStatus": state,
        "mergeable": mergeable,
        "baseRefName": base,
        "baseRefOid": base_oid,
        "headRefOid": head,
    }


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


# Freshness gate anchor; every check below sits on one side or the other.
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


def _actions_check_run(name, started, run_id, conclusion="SUCCESS"):
    run = _check_run(name, started, conclusion)
    run["detailsUrl"] = (
        f"https://github.com/owner/repo/actions/runs/{run_id}/jobs/{run_id + 1000}"
    )
    return run


def _workflow_run(run_id, *, pr_number=370, base_oid="base-tip", head="deadbeef",
                  event="pull_request", run_attempt=1, created_at=_AFTER_ADVANCE,
                  run_started_at=_AFTER_ADVANCE, linked_base_oid=None,
                  linked_head=None):
    linked_base = base_oid if linked_base_oid is None else linked_base_oid
    linked_head_sha = head if linked_head is None else linked_head
    return {
        "id": run_id,
        "name": "CI Pipeline",
        "head_branch": "fix/issue-369-fixmerge-accept-behind-branche",
        "head_sha": head,
        "display_title": "fix(merge): accept behind branches disjoint from the base advance",
        "event": event,
        "status": "completed",
        "conclusion": "success",
        "workflow_id": 329393520,
        "url": f"https://api.github.com/repos/owner/repo/actions/runs/{run_id}",
        "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
        "pull_requests": [{
            "number": pr_number,
            "base": {"ref": "main", "sha": linked_base},
            "head": {"ref": "fix/issue-369-fixmerge-accept-behind-branche",
                     "sha": linked_head_sha},
        }],
        "created_at": created_at,
        "updated_at": run_started_at,
        "run_attempt": run_attempt,
        "run_started_at": run_started_at,
        "previous_attempt_url": None if run_attempt == 1 else (
            f"https://api.github.com/repos/owner/repo/actions/runs/{run_id}/attempts/"
            f"{run_attempt - 1}"
        ),
    }


def _workflow_runs(payloads):
    return lambda run_id: payloads.get(run_id)


def _ci_pr(runs, **kwargs):
    pr = dict(_pr(**kwargs))
    pr["statusCheckRollup"] = list(runs)
    return pr


def _boom(*_args):
    raise AssertionError("resolver must not be consulted on this path")


def _matching_parents(pr):
    """merge_parents_resolver stub: the tested merge commit matches `pr` exactly."""
    return [pr["baseRefOid"], pr["headRefOid"]]


def _stable_base(pr):
    """base_tip_resolver stub: the live base tip still equals the snapshot.

    The default resolver reads GitHub, so every test reaching the identity
    proof injects a stub. This one models the ordinary case -- the base has
    not moved since the PR was read -- and each read returns the same SHA.
    """
    return pr["baseRefOid"]


def _moving_base(*shas):
    """base_tip_resolver stub returning `shas` in order, then repeating the last.

    Models a base advancing between the reads that bracket the merge-parent
    lookup, which is the race the bracketing exists to catch (#371 review).
    """
    seen = []

    def resolve(_pr):
        sha = shas[min(len(seen), len(shas) - 1)]
        seen.append(sha)
        return sha

    resolve.calls = seen
    return resolve


# Sentinel distinguishing "caller passed nothing" from "caller passed None",
# since None is itself a meaningful merge_parents_resolver stub result
# (unresolvable).
_UNSET = object()


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
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], state="CLEAN")
        ok, msg = merge_pr.check_rebased(
            pr, _behind(3),
            _paths(["scripts/merge_pr.py"], ["docs/releases.md"]), _advance(),
            merge_parents_resolver=_matching_parents, base_tip_resolver=_stable_base)
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
                                       _advance(), merge_parents_resolver=_matching_parents,
                                       base_tip_resolver=_stable_base)
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

    def _check(self, pr, behind=2, ours=("a.py",), theirs=("b.py",), when=_ADVANCE_AT,
               run_resolver=None, merge_parents_resolver=_UNSET,
               base_tip_resolver=_UNSET):
        if merge_parents_resolver is _UNSET:
            merge_parents_resolver = _matching_parents
        if base_tip_resolver is _UNSET:
            base_tip_resolver = _stable_base
        return merge_pr.check_rebased(
            pr, _behind(behind),
            _paths(list(ours), list(theirs),
                   base=pr.get("baseRefName", "main"),
                   head=pr.get("headRefOid", "deadbeef")),
            _advance(when,
                     base=pr.get("baseRefName", "main"),
                     head=pr.get("headRefOid", "deadbeef")),
            run_resolver,
            merge_parents_resolver=merge_parents_resolver,
            base_tip_resolver=base_tip_resolver,
        )

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

    def test_fresh_attempt_one_current_head_event_after_advance_passes(self):
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 91)],
            base_oid="base-now",
            head="current-head",
        )
        ok, msg = self._check(
            pr,
            run_resolver=_workflow_runs({
                91: _workflow_run(91, base_oid="base-now", head="current-head"),
            }),
        )
        self.assertTrue(ok)
        self.assertIn("disjoint", msg)

    def test_live_resolved_nested_pr_metadata_cannot_false_pass(self):
        """Historical run 32576962919 proves nested PR OIDs drift with time."""
        current_head = "7daca5ded45321bed41aac186fabfcddeb0eee61"
        current_base = "83309704548b7716d3f71f620762cd3d465d63d9"
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 32576962919)],
            base_oid=current_base,
            head=current_head,
        )
        ok, msg = self._check(
            pr,
            run_resolver=_workflow_runs({
                32576962919: _workflow_run(
                    32576962919,
                    head="5b727c16387ee350de1cfd559419eb49ab000776",
                    base_oid=current_base,
                    created_at="2026-08-22T13:51:04Z",
                    run_started_at="2026-08-22T13:51:04Z",
                    linked_base_oid=current_base,
                    linked_head=current_head,
                ),
            }),
        )
        self.assertFalse(ok)
        self.assertIn("event-time head", msg)
        self.assertIn("5b727c16387ee350de1cfd559419eb49ab000776", msg)

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

    def test_stale_message_preserves_the_head_and_not_a_rebase(self):
        """#371: rebasing would destroy the head-bound review attestation."""
        msg = self._check(_ci_pr([_check_run("Lint", _BEFORE_ADVANCE)]))[1]
        self.assertIn("fresh pull_request event", msg)
        self.assertIn("close and reopen", msg)
        self.assertIn("Do not rebase", msg)
        self.assertNotIn("Rebase on main", msg)

    def test_actions_rerun_of_a_superseded_event_fails_closed(self):
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 92)],
            base_oid="base-now",
            head="current-head",
        )
        ok, msg = self._check(
            pr,
            run_resolver=_workflow_runs({
                92: _workflow_run(
                    92,
                    base_oid="base-now",
                    head="current-head",
                    run_attempt=2,
                ),
            }),
        )
        self.assertFalse(ok)
        self.assertIn("attempt 2", msg)
        self.assertIn("fresh pull_request event", msg)
        self.assertIn("close and reopen", msg)
        self.assertIn("Do not rebase", msg)

    def test_actions_run_with_wrong_event_time_head_fails_closed(self):
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 93)],
            base_oid="base-now",
            head="current-head",
        )
        ok, msg = self._check(
            pr,
            run_resolver=_workflow_runs({
                93: _workflow_run(93, base_oid="base-now", head="old-head"),
            }),
        )
        self.assertFalse(ok)
        self.assertIn("event-time head", msg)

    def test_actions_run_without_event_time_head_fails_closed(self):
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 94)],
            base_oid="base-now",
            head="current-head",
        )
        payload = _workflow_run(94, base_oid="base-now", head="current-head")
        del payload["head_sha"]
        ok, msg = self._check(pr, run_resolver=_workflow_runs({94: payload}))
        self.assertFalse(ok)
        self.assertIn("event-time head", msg)

    def test_malformed_actions_event_time_metadata_fails_closed(self):
        pr = _ci_pr(
            [_actions_check_run("Lint", _AFTER_ADVANCE, 95)],
            base_oid="base-now",
            head="current-head",
        )
        ok, msg = self._check(
            pr,
            run_resolver=_workflow_runs({
                95: _workflow_run(
                    95,
                    base_oid="base-now",
                    head="current-head",
                    created_at="not-a-timestamp",
                ),
            }),
        )
        self.assertFalse(ok)
        self.assertIn("event-time timestamps", msg)

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

    # -- Literal merge-commit-parent identity proof (issue #371) --------
    #
    # Timing alone cannot name the commit a check actually tested. These
    # cover the residual `_ci_saw_base_advance` itself flags: once
    # disjointness and timing both pass, the PR's own test-merge commit must
    # still name the current base tip and this head as its parents.

    def test_current_base_parent_passes(self):
        """Default acceptance case: the tested merge commit is current."""
        ok, msg = self._check(_ci_pr([_check_run("Lint", _AFTER_ADVANCE)]))
        self.assertTrue(ok)
        self.assertIn("tested merge commit names the current base tip", msg)

    def test_superseded_base_parent_blocks(self):
        """The merge commit's base-side parent is an older base tip.

        Disjointness and check-start timing can both look fresh while the
        PR's live test-merge commit still merges onto a base GitHub has not
        finished (re)computing against -- or has since moved again. This is
        the false-pass the literal check exists to catch.
        """
        ok, msg = self._check(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
            merge_parents_resolver=lambda pr: ["superseded-base-sha", pr["headRefOid"]],
        )
        self.assertFalse(ok)
        self.assertIn("does not name exactly the current base tip", msg)
        self.assertIn("retry", msg)

    def test_unresolvable_merge_parents_fails_closed(self):
        """An unresolvable test-merge commit is unverified, not a pass."""
        ok, msg = self._check(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
            merge_parents_resolver=lambda pr: None,
        )
        self.assertFalse(ok)
        self.assertIn("could not be resolved", msg)

    def test_malformed_merge_parents_fail_closed(self):
        """A resolver reporting other than exactly the base tip and head."""
        for parents in ([], ["only-one-parent"],
                        ["deadbeef", "deadbeef", "base-tip"],
                        ["unrelated-a", "unrelated-b"]):
            ok, msg = self._check(
                _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
                merge_parents_resolver=lambda pr, p=parents: p,
            )
            self.assertFalse(ok, f"{parents!r}")
            self.assertIn("does not name exactly the current base tip", msg)

    def test_merge_parents_resolver_exception_fails_closed(self):
        def boom(_pr):
            raise RuntimeError("commit lookup exploded")
        ok, msg = self._check(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]), merge_parents_resolver=boom)
        self.assertFalse(ok)
        self.assertIn("commit lookup exploded", msg)

    def test_merge_parent_gate_never_instructs_rebase(self):
        """#371: the remedy is a fresh CI run or a wait, never a rebase.

        A rebase rewrites the head SHA and destroys the head-bound review
        attestation this whole gate exists to preserve (#369).
        """
        cases = [
            self._check(
                _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
                merge_parents_resolver=lambda pr: ["stale-base", pr["headRefOid"]],
            ),
            self._check(
                _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
                merge_parents_resolver=lambda pr: None,
            ),
        ]
        boom_msg = self._check(
            _ci_pr([_check_run("Lint", _AFTER_ADVANCE)]),
            merge_parents_resolver=lambda pr: (_ for _ in ()).throw(RuntimeError("x")),
        )
        cases.append(boom_msg)
        for ok, msg in cases:
            self.assertFalse(ok)
            self.assertIn("Do not rebase", msg)
            self.assertNotIn("Rebase and", msg)
            self.assertNotIn("Rebase on main", msg)

    def test_merge_parents_resolver_receives_the_pr(self):
        seen = []

        def record(pr):
            seen.append(pr.get("number"))
            return [pr["baseRefOid"], pr["headRefOid"]]

        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], number=4242)
        ok, _ = self._check(pr, merge_parents_resolver=record)
        self.assertTrue(ok)
        self.assertEqual(seen, [4242])


class MergeParentBaseRaceTests(unittest.TestCase):
    """Issue #371 review: the identity proof must own the base it compares to.

    `check_rebased` receives a PR snapshot, and the merge-parent lookup reads
    GitHub separately afterwards. If the base advances in between while
    GitHub's merge ref still lags, the superseded merge commit matches the
    superseded snapshot and the gate passes -- then the merge itself runs
    against a newer base no CI ever saw. These tests pin the two properties
    that close it: the base is read live either side of the parent lookup, and
    any movement -- since the snapshot or during the lookup -- is refused.

    They also pin that malformed resolver output fails closed as a refusal
    rather than escaping as a `TypeError` from `set()` or `", ".join`.
    """

    def _check(self, pr=None, **kwargs):
        pr = pr or _ci_pr([_check_run("Lint", _AFTER_ADVANCE)])
        kwargs.setdefault("merge_parents_resolver", _matching_parents)
        kwargs.setdefault("base_tip_resolver", _stable_base)
        return merge_pr.check_rebased(
            pr, _behind(2),
            _paths(["a.py"], ["b.py"],
                   base=pr.get("baseRefName", "main"),
                   head=pr.get("headRefOid", "deadbeef")),
            _advance(_ADVANCE_AT,
                     base=pr.get("baseRefName", "main"),
                     head=pr.get("headRefOid", "deadbeef")),
            None,
            **kwargs,
        )

    def test_the_parent_lookup_is_bracketed_by_two_live_base_reads(self):
        """A single read before or after the lookup cannot see movement."""
        order = []

        def base(pr):
            order.append("base")
            return pr["baseRefOid"]

        def parents(pr):
            order.append("parents")
            return [pr["baseRefOid"], pr["headRefOid"]]

        ok, _ = self._check(merge_parents_resolver=parents, base_tip_resolver=base)
        self.assertTrue(ok)
        self.assertEqual(order, ["base", "base", "parents", "base"])

    def test_base_advanced_since_the_snapshot_blocks_even_when_parents_match(self):
        """The reported race: parents name the *new* base, the snapshot is old.

        Disjointness and CI timing were both computed against the snapshot, so
        a live base that is no longer that commit invalidates them however
        well-formed the merge commit looks.
        """
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-a")
        ok, msg = self._check(
            pr,
            base_tip_resolver=_moving_base("base-a", "base-b"),
            merge_parents_resolver=lambda p: ["base-b", p["headRefOid"]],
        )
        self.assertFalse(ok)
        self.assertIn("advanced from", msg)
        self.assertIn("superseded", msg)
        self.assertIn("Do not rebase", msg)

    def test_historical_pr_base_oid_with_stable_live_base_tip_passes(self):
        """Issue #460: historical PR baseRefOid must not block a stable live base.

        When GitHub's pull-request payload reports a historical baseRefOid
        from when the PR branch was created, but the live base branch tip is
        stable at a newer commit and merge-ref parents match that live tip,
        the gate must snapshot the live tip and succeed.
        """
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-historical")
        ok, msg = self._check(
            pr,
            base_tip_resolver=lambda _pr: "base-live",
            merge_parents_resolver=lambda p: ["base-live", p["headRefOid"]],
        )
        self.assertTrue(ok)
        self.assertIn("names the current base tip", msg)
        self.assertNotIn("advanced from", msg)

    def test_stale_merge_ref_plus_advanced_base_blocks(self):
        """The exact false-pass: lagging merge ref still matches the old snapshot.

        Before the live re-read, `set(parents) == {snapshot_base, head}` held
        and the gate passed while the base had already moved on.
        """
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-a")
        ok, msg = self._check(
            pr,
            base_tip_resolver=lambda _pr: "base-b",
            merge_parents_resolver=lambda p: ["base-a", p["headRefOid"]],
        )
        self.assertFalse(ok)
        self.assertIn("Do not rebase", msg)

    def test_base_moving_between_the_two_reads_is_refused(self):
        """Movement observed mid-lookup settles, and the settled tip is judged.

        The retry lets the reads agree, and the snapshot comparison then
        catches that the agreed tip is not the one the rest of the gate used.
        """
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-a")
        ok, msg = self._check(
            pr,
            base_tip_resolver=_moving_base("base-a", "base-b", "base-c"),
            merge_parents_resolver=lambda p: ["base-c", p["headRefOid"]],
        )
        self.assertFalse(ok)
        self.assertIn("advanced from", msg)

    def test_a_flapping_read_that_settles_still_proves_identity(self):
        """Bounded retry exists so replica lag is not mistaken for a push.

        The first pair of reads disagrees, the second agrees on the snapshot
        tip, and the proof then proceeds normally rather than refusing.
        """
        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-a")
        ok, msg = self._check(
            pr, base_tip_resolver=_moving_base("base-a", "base-b", "base-a"))
        self.assertTrue(ok)
        self.assertIn("names the current base tip", msg)

    def test_a_base_that_never_settles_is_refused_after_bounded_retries(self):
        """No unbounded spin: the attempts are capped and end in a refusal."""
        seen = []

        def base(_pr):
            sha = f"base-{len(seen)}"
            seen.append(sha)
            return sha

        ok, msg = self._check(base_tip_resolver=base)
        self.assertFalse(ok)
        self.assertIn("kept advancing", msg)
        self.assertIn("Do not rebase", msg)
        self.assertEqual(len(seen), 1 + 2 * merge_pr.MERGE_PARENT_BASE_RECHECK_ATTEMPTS)

    def test_unreadable_base_tip_fails_closed(self):
        """An unknown live base is unverified, never a pass."""
        for value in (None, "", 0, ["base-a"]):
            ok, msg = self._check(base_tip_resolver=lambda _pr, v=value: v)
            self.assertFalse(ok, f"{value!r}")
            self.assertIn("could not be read", msg)

    def test_unreadable_base_tip_on_the_second_read_fails_closed(self):
        ok, msg = self._check(base_tip_resolver=_moving_base("base-a", "base-a", None))
        self.assertFalse(ok)
        self.assertIn("could not be re-read", msg)

    def test_base_tip_resolver_exception_fails_closed(self):
        def boom(_pr):
            raise RuntimeError("base ref lookup exploded")

        ok, msg = self._check(base_tip_resolver=boom)
        self.assertFalse(ok)
        self.assertIn("base ref lookup exploded", msg)
        self.assertIn("Do not rebase", msg)

    def test_unhashable_parent_entries_fail_closed_without_raising(self):
        """`set(parents)` would raise TypeError outside the fail-closed handlers.

        A dict, list or set entry is unhashable, so the shape check has to run
        before the set comparison or the gate crashes instead of refusing.
        """
        for parents in ([{"sha": "base-a"}, "gated-sha"],
                        [["base-a"], ["gated-sha"]],
                        [{"base-a"}, "gated-sha"],
                        [{}, {}]):
            ok, msg = self._check(
                merge_parents_resolver=lambda _pr, p=parents: p)
            self.assertFalse(ok, f"{parents!r}")
            self.assertIn("does not name exactly the current base tip", msg)
            self.assertIn(repr(parents), msg)

    def test_non_string_parent_entries_fail_closed_without_raising(self):
        """`", ".join(parents)` would raise TypeError on any non-string entry."""
        for parents in ([1, 2], [None, None], [b"base-a", b"gated-sha"],
                        ["base-a", None], [3.5, "gated-sha"]):
            ok, msg = self._check(
                merge_parents_resolver=lambda _pr, p=parents: p)
            self.assertFalse(ok, f"{parents!r}")
            self.assertIn("does not name exactly the current base tip", msg)

    def test_non_list_parent_results_fail_closed(self):
        """A resolver may return any object; only a two-item list is a proof."""
        for parents in ("base-a gated-sha", ("base-a", "gated-sha"), 7,
                        {"base-a": 1, "gated-sha": 2}, object()):
            ok, msg = self._check(
                merge_parents_resolver=lambda _pr, p=parents: p)
            self.assertFalse(ok, f"{parents!r}")
            self.assertIn("does not name exactly the current base tip", msg)

    def test_empty_string_parent_entries_fail_closed(self):
        """An empty SHA names nothing, so it cannot stand in for the base tip."""
        ok, msg = self._check(merge_parents_resolver=lambda p: ["", p["headRefOid"]])
        self.assertFalse(ok)
        self.assertIn("does not name exactly the current base tip", msg)

    def test_a_str_subclass_parent_is_not_accepted(self):
        """A `str` subclass can lie in `__eq__`/`__hash__`; require exactly `str`.

        This entry compares equal to anything, so a plain set comparison would
        accept it as both the base tip and the head.
        """
        class Liar(str):
            def __eq__(self, _other):
                return True

            def __hash__(self):
                return hash("base-a")

        pr = _ci_pr([_check_run("Lint", _AFTER_ADVANCE)], base_oid="base-a")
        ok, msg = self._check(
            pr, merge_parents_resolver=lambda p: [Liar("nonsense"), p["headRefOid"]])
        self.assertFalse(ok)
        self.assertIn("does not name exactly the current base tip", msg)

    def test_malformed_parents_never_instruct_a_rebase(self):
        """#371: the remedy stays a wait, even for output this broken."""
        for parents in ([{"sha": "x"}], None, [1, 2], "nope"):
            ok, msg = self._check(
                merge_parents_resolver=lambda _pr, p=parents: p)
            self.assertFalse(ok, f"{parents!r}")
            self.assertIn("Do not rebase", msg)
            self.assertNotIn("Rebase on main", msg)


class CurrentBaseTipTests(unittest.TestCase):
    """`_current_base_tip` must answer None for anything it cannot read."""

    def _tip(self, payload, slug="o/r", branch="main"):
        with patch.object(merge_pr, "get_repo_slug", return_value=slug), \
             patch.object(merge_pr, "_gh_json", return_value=payload):
            return merge_pr._current_base_tip({"baseRefName": branch})

    def test_reads_the_ref_object_sha(self):
        self.assertEqual(self._tip({"object": {"sha": "base-a"}}), "base-a")

    def test_queries_the_base_branch_ref(self):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json",
                          return_value={"object": {"sha": "x"}}) as gh:
            merge_pr._current_base_tip({"baseRefName": "release/v2"})
        self.assertEqual(gh.call_args[0][0],
                         ["gh", "api", "repos/o/r/git/ref/heads/release/v2"])

    def test_unusable_payloads_are_none(self):
        for payload in (None, {}, [], "sha", {"object": None}, {"object": "sha"},
                        {"object": {}}, {"object": {"sha": ""}},
                        {"object": {"sha": 7}}):
            self.assertIsNone(self._tip(payload), f"{payload!r}")

    def test_missing_branch_or_slug_is_none(self):
        self.assertIsNone(self._tip({"object": {"sha": "x"}}, branch=""))
        self.assertIsNone(self._tip({"object": {"sha": "x"}}, slug=None))


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


class MergeCommitParentsTests(unittest.TestCase):
    """`_merge_commit_parents`: the literal merge-commit identity proof (#371).

    REST's `merge_commit_sha` -- distinct from GraphQL's `mergeCommit`, which
    stays null until actually merged -- names the PR's live test-merge commit;
    its `parents` name what it actually merges.
    """

    @staticmethod
    def _gh_json_stub(pull, commit, number=370):
        def resolve(args):
            url = args[-1]
            if url == f"repos/o/r/pulls/{number}":
                return pull
            if url == "repos/o/r/commits/merge-sha":
                return commit
            raise AssertionError(f"unexpected gh api call: {url}")
        return resolve

    def _resolve(self, pull, commit, number=370):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"), \
             patch.object(merge_pr, "_gh_json",
                          side_effect=self._gh_json_stub(pull, commit, number)):
            return merge_pr._merge_commit_parents({"number": number})

    def test_two_parents_are_returned(self):
        pull = {"merge_commit_sha": "merge-sha"}
        commit = {"parents": [{"sha": "base-tip"}, {"sha": "head-sha"}]}
        self.assertEqual(self._resolve(pull, commit), ["base-tip", "head-sha"])

    def test_missing_pr_number_returns_none(self):
        with patch.object(merge_pr, "get_repo_slug", return_value="o/r"):
            self.assertIsNone(merge_pr._merge_commit_parents({}))

    def test_missing_slug_returns_none(self):
        with patch.object(merge_pr, "get_repo_slug", return_value=None):
            self.assertIsNone(merge_pr._merge_commit_parents({"number": 370}))

    def test_unresolvable_pull_returns_none(self):
        self.assertIsNone(self._resolve(None, {"parents": []}))

    def test_missing_merge_commit_sha_returns_none(self):
        self.assertIsNone(self._resolve({}, {}))

    def test_non_string_merge_commit_sha_returns_none(self):
        self.assertIsNone(self._resolve({"merge_commit_sha": 12345}, {}))

    def test_empty_merge_commit_sha_returns_none(self):
        self.assertIsNone(self._resolve({"merge_commit_sha": ""}, {}))

    def test_unresolvable_commit_returns_none(self):
        self.assertIsNone(self._resolve({"merge_commit_sha": "merge-sha"}, None))

    def test_wrong_parent_count_returns_none(self):
        for parents in ([], [{"sha": "only-one"}],
                        [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]):
            commit = {"parents": parents}
            self.assertIsNone(
                self._resolve({"merge_commit_sha": "merge-sha"}, commit), f"{parents!r}")

    def test_malformed_parent_entries_return_none(self):
        for parents in (
            "not-a-list",
            [{"sha": "a"}, "not-a-dict"],
            [{"sha": "a"}, {"no_sha": "b"}],
            [{"sha": "a"}, {"sha": 5}],
            [{"sha": "a"}, {"sha": ""}],
        ):
            commit = {"parents": parents}
            self.assertIsNone(
                self._resolve({"merge_commit_sha": "merge-sha"}, commit), f"{parents!r}")

    def test_uses_the_prs_own_number(self):
        pull = {"merge_commit_sha": "merge-sha"}
        commit = {"parents": [{"sha": "base-tip"}, {"sha": "head-sha"}]}
        self.assertEqual(
            self._resolve(pull, commit, number=9001), ["base-tip", "head-sha"])


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


class RetiredReviewRoundSurfaceTests(unittest.TestCase):
    """#414: repeated review rounds are history, not a gate or a work source.

    The removed machinery counted rework rounds, added a soft `review rounds`
    DoD gate, and -- once past a threshold -- posted split guidance and filed
    follow-up issues on the board. Automatic issue creation is exactly the
    project-management layer the kernel is not; these assertions keep it gone.
    """

    RETIRED_ATTRIBUTES = (
        "check_review_rounds", "count_review_rounds", "_is_rework_review",
        "build_review_round_split_plan", "emit_review_round_split",
        "fetch_unresolved_finding_summaries", "_split_item_marker",
        "_find_existing_split_follow_ups", "_attach_follow_up_to_board",
        "_pr_comments_bodies", "_flatten_comment_pages",
        "REVIEW_ROUND_THRESHOLD", "REVIEW_ROUND_SPLIT_MARKER",
        "REVIEW_ROUND_SPLIT_ITEM_FMT", "REVIEW_ROUND_SPLIT_ITEM_RE",
    )

    def test_no_review_round_or_split_surface_remains(self):
        for name in self.RETIRED_ATTRIBUTES:
            self.assertFalse(hasattr(merge_pr, name),
                             f"merge_pr still exposes {name}")

    def test_the_merge_gate_has_no_review_rounds_entry(self):
        pr = {"number": 42, "body": "Closes #98\n",
              "reviews": [{"state": "CHANGES_REQUESTED", "author": {"login": "r"}}] * 5}
        with patch.object(merge_pr, "check_open", return_value=(True, "open")), \
             patch.object(merge_pr, "check_issue_link", return_value=(True, "linked")), \
             patch.object(merge_pr, "check_verification", return_value=(True, "ok")), \
             patch.object(merge_pr, "check_ci", return_value=(True, "green")), \
             patch.object(merge_pr, "check_reviews", return_value=(True, "reviewed")), \
             patch.object(merge_pr, "check_rebased", return_value=(True, "current")), \
             patch.object(merge_pr, "check_size", return_value=(True, "small")), \
             patch.object(merge_pr, "check_test_coverage", return_value=(True, "tests")), \
             patch.object(merge_pr, "check_spec_sync", return_value=(True, "ok")), \
             patch.object(merge_pr, "check_acceptance", return_value=(True, "accept")), \
             patch.object(merge_pr, "linked_issues", return_value=[98]):
            ok, gates = merge_pr.evaluate_dod(pr, {98: "- [x] done\n"}, evidence={})
        self.assertTrue(ok)
        self.assertNotIn("review rounds", [name for name, _, _ in gates])

    def test_the_cli_no_longer_offers_review_round_splitting(self):
        source = Path(merge_pr.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--emit-review-split", source)
        self.assertNotIn("aru-review-round-split", source)
        self.assertNotIn("gh issue create", source)


class SpecSyncGateOrderingTests(unittest.TestCase):
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
            9, {"headRefOid": "gated-sha", "baseRefOid": "base-sha"}, "squash",
            "base-sha", base_tip_resolver=lambda pr: "base-sha",
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
        final, message = merge_pr.execute_merge(
            9, {"headRefOid": "sha", "baseRefOid": "base-sha"}, "squash",
            "base-sha", base_tip_resolver=lambda pr: "base-sha",
        )

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
        return_value=coderabbit_evidence(),
    )
    @patch.object(merge_pr, "check_spec_sync", return_value=(True, "ok"))
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    @patch.object(merge_pr, "_current_base_tip", return_value="base-sha")
    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
    def test_successful_merge_with_branch_delete_failure_is_resumable(
        self, _base_tip, fetch, _json, _sync, _threads, execute, _root, _chdir, _prune, _local,
        _remote, _close, _done, _issue_claim, merger_claim,
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
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviews": [{
                "id": "peer-approval", "state": "APPROVED",
                "submittedAt": "2026-01-01T00:00:00Z",
                "author": {"login": "peer"},
            }],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}, {"name": "review:coderabbit"}],
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

        execute.assert_called_once_with(9, fetch.return_value, "merge", "base-sha")
        self.assertEqual(_close.call_count, 4)
        self.assertEqual(_done.call_count, 4)
        self.assertEqual(_issue_claim.call_count, 4)
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
        return_value=coderabbit_evidence(),
    )
    @patch.object(merge_pr, "check_spec_sync", return_value=(True, "ok"))
    @patch.object(merge_pr, "_gh_json", return_value={"body": "## Acceptance Criteria\n- [x] done"})
    @patch.object(merge_pr, "fetch_pr")
    @patch.object(merge_pr, "_current_base_tip", return_value="base-sha")
    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
    def test_default_merge_method_is_merge(
        self, _base_tip, fetch, _json, _sync, _threads, execute, _root, closeout
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
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviews": [{
                "id": "peer-approval", "state": "APPROVED",
                "submittedAt": "2026-01-01T00:00:00Z",
                "author": {"login": "peer"},
            }],
            "author": {"login": "author"},
            "labels": [{"name": "author:agent-1"}, {"name": "review:coderabbit"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_OK)

        execute.assert_called_once_with(9, fetch.return_value, "merge", "base-sha")


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

    def _race_window(self, *, live_base, merge_parents):
        """Drive main() to the serialized re-read after the base moved mid-flight.

        The base-OID lock only proves nothing moved *during* the merge
        command; it says nothing about a move that landed between the
        initial DoD gate and grabbing this lock. That race is no longer
        disqualifying on its own (issue #371): check_rebased re-derives
        freshness -- disjointness, CI timing, and the tested merge commit's
        parents -- against whatever the base is right now, rather than
        comparing OIDs and demanding a rebase.
        """
        initial = self._open_pr("base-a")
        fresh = self._open_pr(live_base)
        fresh.update({
            "baseRefName": "main",
            "headRefOid": "gated-sha",
            "mergeStateStatus": "BEHIND",
            "statusCheckRollup": [_check_run("Lint", _AFTER_ADVANCE)],
        })
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr", side_effect=[initial, fresh]), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": "gated-sha"}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "_behind_by", new=lambda _base, _head: 1), \
             patch.object(merge_pr, "_compare_paths", _paths(["a.py"], ["b.py"],
                                                             base="main", head="gated-sha")), \
             patch.object(merge_pr, "_base_advance_time",
                          _advance(base="main", head="gated-sha")), \
             patch.object(merge_pr, "_merge_commit_parents", return_value=merge_parents), \
             patch.object(merge_pr, "_current_base_tip", return_value=live_base), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "execute_merge",
                          return_value=(merged_pr(), "merged")) as execute:
            return merge_pr.main(), execute

    def test_base_move_inside_window_still_merges_once_the_merge_ref_catches_up(self):
        """A base move mid-flight is not itself disqualifying.

        check_rebased re-derives freshness against the live base -- disjoint
        changes, CI that started after the advance, and a tested merge commit
        that already names the new base tip as a parent -- so the merge
        proceeds without ever asking anyone to rebase.
        """
        code, execute = self._race_window(
            live_base="base-b", merge_parents=["base-b", "gated-sha"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        execute.assert_called_once()

    def test_base_move_inside_window_blocks_while_the_merge_ref_still_lags(self):
        """The base moved, but GitHub has not finished recomputing the merge ref.

        Its parents still name the superseded base, so the literal identity
        proof refuses -- correctly, since what is about to be merged has not
        been verified against what the base is now.
        """
        code, execute = self._race_window(
            live_base="base-b", merge_parents=["base-a", "gated-sha"])
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
             patch.object(merge_pr, "_merge_commit_parents",
                          return_value=["base-a", "gated-sha"]), \
             patch.object(merge_pr, "_current_base_tip", return_value="base-a"), \
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


class FinalWindowBaseMovementTests(unittest.TestCase):
    """The gap between the last identity proof and the server-side merge.

    `gh pr merge` pins only the head: REST's `PUT /pulls/{n}/merge` takes a
    head `sha` and GraphQL's `mergePullRequest` an `expectedHeadOid`, and
    neither accepts a base-side precondition, so GitHub will happily merge the
    reviewed head into whatever the base is when the request lands. Every test
    here drives a base that advances inside that final window and asserts the
    merge command is never issued (#371 review).
    """

    HEAD = "gated-sha"

    def _pr(self, base="base-a"):
        return {"number": 9, "headRefOid": self.HEAD, "baseRefOid": base,
                "baseRefName": "main"}

    def _blocked(self, expected_base, resolver):
        with patch.object(merge_pr.subprocess, "run") as run, \
             patch.object(merge_pr, "fetch_pr") as fetch:
            final, message = merge_pr.execute_merge(
                9, self._pr(), "merge", expected_base, base_tip_resolver=resolver,
            )
        run.assert_not_called()
        fetch.assert_not_called()
        self.assertIsNone(final)
        self.assertTrue(message.startswith(merge_pr.MERGE_NOT_ATTEMPTED), message)
        return message

    def test_base_advance_in_the_final_window_blocks_the_merge_command(self):
        message = self._blocked("base-a", lambda pr: "base-b")
        self.assertIn("base-a", message)
        self.assertIn("base-b", message)
        self.assertIn("no check ever tested", message)

    def test_the_refusal_never_asks_for_a_rebase(self):
        message = self._blocked("base-a", lambda pr: "base-b")
        self.assertIn("Do not rebase", message)
        self.assertNotIn("Rebase on main", message)

    def test_unreadable_base_tip_before_the_merge_fails_closed(self):
        message = self._blocked("base-a", lambda pr: None)
        self.assertIn("could not be re-read", message)

    def test_malformed_base_tip_before_the_merge_fails_closed(self):
        for live in ("", 0, b"base-a", ["base-a"]):
            with self.subTest(live=live):
                message = self._blocked("base-a", lambda pr, live=live: live)
                self.assertIn("could not be re-read", message)

    def test_raising_base_tip_resolver_before_the_merge_fails_closed(self):
        def explode(pr):
            raise RuntimeError("api down")

        message = self._blocked("base-a", explode)
        self.assertIn("RuntimeError: api down", message)

    def test_unpinned_base_is_itself_a_refusal(self):
        for expected in (None, "", 0, b"base-a"):
            with self.subTest(expected=expected):
                message = self._blocked(expected, lambda pr: "base-a")
                self.assertIn("no proved base tip", message)

    def test_base_is_reproved_before_the_merge_command_not_after(self):
        """Ordering is the whole guarantee: anything after the command is too late."""
        order = []

        def resolver(pr):
            order.append("base-read")
            return "base-a"

        def run(cmd, **kwargs):
            order.append(" ".join(cmd[:3]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(merge_pr.subprocess, "run", side_effect=run), \
             patch.object(merge_pr, "fetch_pr", return_value=merged_pr()):
            final, message = merge_pr.execute_merge(
                9, self._pr(), "merge", "base-a", base_tip_resolver=resolver,
            )

        self.assertEqual(order, ["base-read", "gh pr merge"])
        self.assertEqual(final["state"], "MERGED")
        self.assertIn("accepted the merge", message)

    def _drive_main(self, base_reads):
        """Run main() with the real execute_merge and a scripted base ref."""
        pr = {
            "number": 9, "title": "open", "body": "Closes #7", "state": "OPEN",
            "isDraft": False, "headRefName": "fix/issue-7-example",
            "headRefOid": self.HEAD, "baseRefOid": "base-a", "baseRefName": "main",
            "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
        }
        commands = []

        def run(cmd, **kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr",
                          side_effect=[pr, dict(pr), merged_pr()]), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": self.HEAD}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "_behind_by", new=lambda _base, _head: 0), \
             patch.object(merge_pr, "_current_base_tip", side_effect=base_reads), \
             patch.object(merge_pr, "run_closeout", return_value=True), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "checkpoint written")), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr.subprocess, "run", side_effect=run):
            code = merge_pr.main()
        merges = [c for c in commands if c[:3] == ["gh", "pr", "merge"]]
        return code, merges

    def test_end_to_end_base_move_after_the_final_gates_never_reaches_github(self):
        """The exact reported race, end to end.

        Every gate passes against base-a, then the base advances to base-b in
        the instant before the server merge. Nothing downstream can undo a
        merge, so the only fail-closed outcome is that `gh pr merge` is never
        run at all.
        """
        code, merges = self._drive_main(["base-a", "base-b"])
        # Blocked, not error: nothing was mutated, so re-running is the remedy.
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        self.assertEqual(merges, [])

    def test_end_to_end_held_base_still_merges_with_the_head_pinned(self):
        code, merges = self._drive_main(["base-a", "base-a"])
        self.assertEqual(code, merge_pr.EXIT_OK)
        self.assertEqual(len(merges), 1)
        self.assertIn("--match-head-commit", merges[0])
        self.assertIn(self.HEAD, merges[0])

    def test_end_to_end_unreadable_final_base_tip_fails_closed_before_merge(self):
        code, merges = self._drive_main([None, "base-a"])
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        self.assertEqual(merges, [])

    def test_a_failed_merge_command_is_still_reported_as_an_error(self):
        """The blocked classification must not swallow a real merge failure.

        Only refusals taken before the command ran are blocks; a command that
        ran and left the PR open may have mutated something and stays an error.
        """
        with patch.object(merge_pr, "execute_merge",
                          return_value=(None, "GitHub still reports OPEN; refused")), \
             patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "fetch_pr",
                          return_value=dict(self._pr(), title="open",
                                            body="Closes #7", state="OPEN",
                                            isDraft=False, mergeStateStatus="CLEAN",
                                            mergeable="MERGEABLE")), \
             patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
             patch.object(merge_pr, "review_evidence",
                          return_value={"head_oid": self.HEAD}), \
             patch.object(merge_pr, "evaluate_dod", return_value=(True, [])), \
             patch.object(merge_pr, "_behind_by", new=lambda _base, _head: 0), \
             patch.object(merge_pr, "_current_base_tip", return_value="base-a"), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_ERROR)


class CloseOutRecoveryTests(unittest.TestCase):
    def _run(self, failing):
        outcomes = {
            "prune_worktree": (True, "worktree ok"),
            "cleanup_local_branch": (True, "local ok"),
            "delete_remote_branch": (True, "remote ok"),
            "record_terminal_lease": (True, "lease ok"),
            "detect_stale_writer": (True, "no stale write"),
            "ensure_issue_closed": (True, "closed"),
            "reconcile_issue_done": (True, "done"),
            "clear_issue_claims": (True, "issue claim clear"),
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

    @patch.object(merge_pr, "detect_stale_writer", return_value=(True, "no stale write"))
    @patch.object(merge_pr, "record_terminal_lease", return_value=(True, "lease ok"))
    @patch.object(merge_pr, "sweep_leftovers", return_value=(True, "janitor ok"))
    @patch.object(merge_pr, "clear_merger_claims", return_value=(True, "merger clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(True, "remote"))
    @patch.object(merge_pr, "cleanup_local_branch", return_value=(True, "local"))
    @patch.object(merge_pr, "prune_worktree", return_value=(True, "worktree"))
    @patch.object(merge_pr.os, "chdir")
    def test_changes_to_surviving_root_before_pruning_caller_worktree(
        self, chdir, prune, _local, _remote, _close, _done, _issue, _merger,
        _janitor, _lease, _stale,
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
            patch.object(merge_pr, "record_terminal_lease", return_value=(True, "lease ok")),
            patch.object(merge_pr, "detect_stale_writer", return_value=(True, "no stale write")),
            patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed")),
            patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done")),
            patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue claim clear")),
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
                 patch.object(merge_pr, "record_terminal_lease", return_value=(True, "lease ok")), \
                 patch.object(merge_pr, "detect_stale_writer", return_value=(True, "no stale write")), \
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
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))):
            rc = merge_pr.main()

        self.assertEqual(rc, merge_pr.EXIT_BLOCKED)
        execute.assert_not_called()

    @patch.object(merge_pr, "execute_merge")
    @patch.object(merge_pr, "evaluate_dod", side_effect=[(True, []), (False, [
        ("review", False, "CodeRabbit evidence changed"),
    ])])
    @patch.object(merge_pr, "review_evidence", return_value={"head_oid": "H1"})
    @patch.object(merge_pr, "_gh_json", return_value={"body": ""})
    @patch.object(merge_pr, "fetch_pr")
    def test_mutable_review_evidence_is_revalidated_immediately_before_merge(
        self, fetch_pr, _issue, evidence, evaluate, execute
    ):
        first = self.open_pr("H1")
        first["baseRefOid"] = "B1"
        fresh = dict(first)
        fetch_pr.side_effect = [first, fresh]
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]), \
             patch.object(merge_pr, "repository_merge_lock",
                          return_value=nullcontext((True, "serialized"))), \
             patch.object(merge_pr, "check_rebased", return_value=(True, "current")):
            rc = merge_pr.main()

        self.assertEqual(rc, merge_pr.EXIT_BLOCKED)
        self.assertEqual(evidence.call_count, 2)
        self.assertEqual(evaluate.call_count, 2)
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
            **_pull_version(cls.HEAD),
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
            "owner", "repo", 248, _VERSION,
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
            "owner", "repo", 248, _VERSION,
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
            "owner", "repo", 248, _VERSION,
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
            "owner", "repo", 248, _VERSION,
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
            "owner", "repo", 248, _VERSION,
        )
        self.assertEqual(len(events["size-waiver"]), 1)
        self.assertIn("cursor=older", gh_json.call_args_list[1].args[0])

        changed = self.page([], after)
        changed["data"]["repository"]["pullRequest"]["headRefOid"] = "moved"
        gh_json.side_effect = None
        gh_json.return_value = changed
        self.assertIsNone(
            merge_pr._body_edit_events("owner", "repo", 248, _VERSION)
        )


class ReviewBodyEditIntegrationTests(unittest.TestCase):
    HEAD = "a" * 40
    REVIEW = {
        "id": "peer-review",
        "state": "COMMENTED",
        "submittedAt": "2026-08-17T00:20:00Z",
        "body": "Verdict: approved after fixes.",
        "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
        "commit": {"oid": HEAD},
    }

    @staticmethod
    def thread_page(comments):
        return {"data": {"repository": {"pullRequest": {
            **_pull_version(ReviewBodyEditIntegrationTests.HEAD),
            "commits": {"nodes": [{"commit": {
                "committedDate": "2026-08-17T00:00:00Z",
            }}]},
            "reviewThreads": {
                "nodes": [
                    {
                        "isResolved": True,
                        "isOutdated": False,
                        "comments": _complete_comments([{
                            "createdAt": "2026-08-17T00:23:58Z",
                            "body": body,
                            "author": {"login": "coderabbitai[bot]", "__typename": "Bot"},
                        }]),
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
                return_value=(_VERSION, True, [self.REVIEW]),
            ),
            patch.object(
                merge_pr, "_review_comment_evidence",
                return_value={
                    "coderabbit_full_review_comments": [],
                    "codeant_status_comments": [],
                },
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
        evidence["coderabbit_status"] = [{
            "__typename": "CheckRun",
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "checkSuite": {"app": {"slug": "coderabbitai"}},
        }]

        self.assertEqual(evidence["unfixed"], 0)
        self.assertEqual(evidence["body_addressed"], 2)
        ok, message = merge_pr.check_reviews(
            coderabbit_pr("author:agent-1"), evidence,
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
    """Resolving a thread is not proof that the underlying finding was fixed."""

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
            coderabbit_pr(*self.PASSING), coderabbit_evidence(),
        )
        self.assertTrue(ok)

    def test_an_explicitly_withdrawn_finding_passes_without_a_commit(self):
        """The escape hatch that keeps the commit rule from forcing no-op commits.

        Without it a reviewer who withdraws a finding leaves the PR unmergeable,
        and the rational response is to manufacture an empty commit - an audit
        trail that lies, which is worse than the gap being closed.
        """
        evidence = coderabbit_evidence()
        evidence["withdrawn"] = 1
        ok, msg = merge_pr.check_reviews(coderabbit_pr(*self.PASSING), evidence)
        self.assertTrue(ok)
        self.assertIn("withdrawn, not fixed", msg)

    def test_the_audit_line_distinguishes_fixed_from_withdrawn(self):
        """A later reader must be able to tell why the merge was allowed."""
        _, fixed = merge_pr.check_reviews(
            coderabbit_pr(*self.PASSING), coderabbit_evidence(),
        )
        withdrawn_evidence = coderabbit_evidence()
        withdrawn_evidence["withdrawn"] = 2
        _, withdrawn = merge_pr.check_reviews(
            coderabbit_pr(*self.PASSING), withdrawn_evidence,
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
    """A review attests only to the commit it was submitted against."""

    PASSING = ("author:agent-1", "reviewed-by:agent-2")

    def test_a_review_of_an_earlier_head_blocks(self):
        ok, msg = merge_pr.check_reviews(
            labelled(*self.PASSING),
            {"unresolved": 0, "unfixed": 0, "withdrawn": 0, "reviewed_head": False},
        )
        self.assertFalse(ok)
        self.assertIn("CodeRabbit", msg)

    def test_a_review_at_head_passes(self):
        ok, msg = merge_pr.check_reviews(
            coderabbit_pr(*self.PASSING), coderabbit_evidence(),
        )
        self.assertTrue(ok)
        self.assertIn("CodeRabbit", msg)


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
            coderabbit_pr(*self.PASSING), coderabbit_evidence(),
        )
        self.assertTrue(ok)

    @patch("merge_pr.get_repo_slug", return_value="owner/repo")
    @patch("merge_pr._gh_json")
    def test_review_evidence_parses_outdated_threads_with_and_without_evidence(self, mock_gh_json, _mock_slug):
        gql_data = {
            "data": {
                "repository": {
                    "pullRequest": {
                        **_pull_version(),
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
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 1", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 2", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 3", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": True, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 4 (anchor line deleted)", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
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
                        **_pull_version(),
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
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 1", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 2", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": False, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 3", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
                                {"isResolved": False, "isOutdated": True, "comments": _complete_comments([{"createdAt": "2026-08-10T11:00:00Z", "body": "finding 4 (anchor line deleted)", "author": {"login": "coderabbitai[bot]", "__typename": "Bot"}}])},
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
             patch.object(merge_pr, "_current_base_tip", return_value="base-sha"), \
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

    def test_ensure_pr_head_checkout_fetches_from_fork_remote_for_cross_repository_pr(self):
        pr = {
            "number": 12,
            "headRefOid": "abc1234567890",
            "headRefName": "fix/bug",
            "isCrossRepository": True,
            "headRepository": {
                "nameWithOwner": "contributor/repo",
                "url": "https://github.com/contributor/repo",
            },
        }
        with patch.object(merge_pr, "get_repo_slug", return_value="base/repo"), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(tempfile, "mkdtemp", return_value="/tmp/checkout"), \
             patch.object(merge_pr, "run_cmd") as run_cmd:
            def fake_run(argv, check=False, cwd=None):
                if argv[:4] == ["git", "fetch", "--no-tags", "https://github.com/contributor/repo"]:
                    return (0, "", "")
                if argv[:4] == ["git", "worktree", "add", "--detach"]:
                    return (0, "", "")
                if argv == ["git", "rev-parse", "HEAD"]:
                    return (0, "abc1234567890\n", "")
                if argv == ["git", "status", "--porcelain"]:
                    return (0, "", "")
                return (0, "", "")
            run_cmd.side_effect = fake_run
            dest, err = merge_pr.ensure_pr_head_checkout(pr)
        self.assertEqual(dest, "/tmp/checkout")
        self.assertIsNone(err)
        fetch_call = next(c for c in run_cmd.call_args_list if c.args[0][:2] == ["git", "fetch"])
        self.assertEqual(fetch_call.args[0][3], "https://github.com/contributor/repo")

    def test_ensure_pr_head_checkout_falls_back_to_pull_head_ref(self):
        pr = {
            "number": 12,
            "headRefOid": "abc1234567890",
            "headRefName": "fix/bug",
            "isCrossRepository": False,
        }
        with patch.object(merge_pr, "get_repo_slug", return_value="base/repo"), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(tempfile, "mkdtemp", return_value="/tmp/checkout"), \
             patch.object(merge_pr, "run_cmd") as run_cmd:
            def fake_run(argv, check=False, cwd=None):
                if argv == ["git", "fetch", "--no-tags", "origin", "abc1234567890"]:
                    return (1, "", "fetch sha failed")
                if argv == ["git", "fetch", "--no-tags", "origin", "fix/bug"]:
                    return (1, "", "fetch ref failed")
                if argv == ["git", "fetch", "--no-tags", "origin", "pull/12/head"]:
                    return (0, "", "")
                if argv[:4] == ["git", "worktree", "add", "--detach"]:
                    return (0, "", "")
                if argv == ["git", "rev-parse", "HEAD"]:
                    return (0, "abc1234567890\n", "")
                if argv == ["git", "status", "--porcelain"]:
                    return (0, "", "")
                return (0, "", "")
            run_cmd.side_effect = fake_run
            dest, err = merge_pr.ensure_pr_head_checkout(pr)
        self.assertEqual(dest, "/tmp/checkout")
        self.assertIsNone(err)

    def test_persist_acceptance_evidence_verifies_post_write(self):
        pr = {"number": 9, "headRefOid": "head123"}
        records = [{"command": ["python3", "-m", "unittest"], "status": "passed", "exit_code": 0, "duration_seconds": 0.1}]
        rendered = merge_pr.render_verification_evidence({"schema": "aru.verification.v1", "head_sha": "head123", "commands": records})
        with patch.object(merge_pr, "_gh_json") as gh_json, \
             patch.object(merge_pr, "run_cmd", return_value=(0, "", "")):
            gh_json.side_effect = [
                {"body": "Closes #1\n", "headRefOid": "head123"},
                {"body": f"Closes #1\n{rendered}\n", "headRefOid": "head123"},
            ]
            ok, msg = merge_pr.persist_acceptance_evidence(9, pr, records)
        self.assertTrue(ok)
        self.assertIn("persisted 1 acceptance record", msg)

    def test_persist_acceptance_evidence_retries_on_concurrent_modification(self):
        pr = {"number": 9, "headRefOid": "head123"}
        records = [{"command": ["python3", "-m", "unittest"], "status": "passed", "exit_code": 0, "duration_seconds": 0.1}]
        rendered = merge_pr.render_verification_evidence({"schema": "aru.verification.v1", "head_sha": "head123", "commands": records})
        with patch.object(merge_pr, "_gh_json") as gh_json, \
             patch.object(merge_pr, "run_cmd", return_value=(0, "", "")):
            gh_json.side_effect = [
                {"body": "Closes #1\n", "headRefOid": "head123"},
                {"body": "Closes #1 - author concurrent edit\n", "headRefOid": "head123"},
                {"body": "Closes #1 - author concurrent edit\n", "headRefOid": "head123"},
                {"body": f"Closes #1 - author concurrent edit\n{rendered}\n", "headRefOid": "head123"},
            ]
            ok, msg = merge_pr.persist_acceptance_evidence(9, pr, records)
        self.assertTrue(ok)
        self.assertIn("persisted 1 acceptance record", msg)

    def test_persist_acceptance_evidence_fails_closed_when_verification_fails(self):
        pr = {"number": 9, "headRefOid": "head123"}
        records = [{"command": ["python3", "-m", "unittest"], "status": "passed", "exit_code": 0, "duration_seconds": 0.1}]
        with patch.object(merge_pr, "_gh_json", return_value={"body": "Closes #1\n", "headRefOid": "head123"}), \
             patch.object(merge_pr, "run_cmd", return_value=(0, "", "")):
            ok, msg = merge_pr.persist_acceptance_evidence(9, pr, records, max_retries=2)
        self.assertFalse(ok)
        self.assertIn("could not verify persisted acceptance evidence", msg)


if __name__ == "__main__":
    unittest.main()
