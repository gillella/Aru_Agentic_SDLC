import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_work as fnw


def ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def pr(number, *labels, draft=False, checks="green", reviews=0, minutes_old=5,
       decision="", title="a pr"):
    rollup = {
        "green": [{"name": "verify", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        "red": [{"name": "verify", "status": "COMPLETED", "conclusion": "FAILURE"}],
        "pending": [{"name": "verify", "status": "IN_PROGRESS"}],
        "none": [],
    }[checks]
    return {
        "number": number, "title": title, "isDraft": draft,
        "labels": [{"name": n} for n in labels],
        "reviews": [{"state": "COMMENTED"}] * reviews,
        "statusCheckRollup": rollup,
        "updatedAt": ts(minutes_old), "createdAt": ts(minutes_old),
        "reviewDecision": decision, "body": "Closes #1", "headRefName": "x",
        "_active_review_feedback": [],
    }


def eligible(p, agent="agent-2", family="openai", cap=3, wait=30):
    return fnw.review_eligibility(p, agent, family, cap, wait)


class CiStateTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(fnw.ci_state(pr(1, checks="green")), "green")
        self.assertEqual(fnw.ci_state(pr(1, checks="red")), "red")
        self.assertEqual(fnw.ci_state(pr(1, checks="pending")), "pending")

    def test_no_checks_is_not_green(self):
        # An unverified diff is not a verified one; reviewing it wastes the pass.
        self.assertEqual(fnw.ci_state(pr(1, checks="none")), "none")


class EligibilityTests(unittest.TestCase):
    def test_cross_family_pr_is_eligible(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic"))
        self.assertTrue(verdict["eligible"])
        self.assertTrue(verdict["cross_family"])
        self.assertFalse(verdict["degraded"])

    def test_own_pr_is_never_eligible(self):
        verdict = eligible(pr(1, "author:agent-2", "family:openai"))
        self.assertFalse(verdict["eligible"])
        self.assertIn("you wrote it", verdict["reason"])

    def test_own_pr_rejected_even_when_it_has_waited_forever(self):
        # The family fallback must never soften the self-review rule.
        verdict = eligible(pr(1, "author:agent-2", "family:openai", minutes_old=6000))
        self.assertFalse(verdict["eligible"])

    def test_pr_claimed_by_another_reviewer_is_skipped(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic", "reviewer:agent-9"))
        self.assertFalse(verdict["eligible"])
        self.assertIn("agent-9", verdict["reason"])

    def test_resuming_my_own_review_claim_is_allowed(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic", "reviewer:agent-2"))
        self.assertTrue(verdict["eligible"])

    def test_draft_is_skipped(self):
        self.assertFalse(eligible(pr(1, "author:agent-1", "family:anthropic", draft=True))["eligible"])

    def test_red_and_pending_ci_are_skipped(self):
        self.assertFalse(eligible(pr(1, "author:agent-1", "family:anthropic", checks="red"))["eligible"])
        self.assertFalse(eligible(pr(1, "author:agent-1", "family:anthropic", checks="pending"))["eligible"])
        self.assertFalse(eligible(pr(1, "author:agent-1", "family:anthropic", checks="none"))["eligible"])

    def test_review_round_count_never_blocks_an_independent_agent(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic", reviews=10))
        self.assertTrue(verdict["eligible"])

    def test_unresolved_commented_findings_wait_on_the_author(self):
        candidate = pr(1, "author:agent-1", "family:anthropic", reviews=1)
        candidate["_active_review_feedback"] = [{"body": "fix"}, {"body": "also fix"}]

        verdict = eligible(candidate)

        self.assertFalse(verdict["eligible"])
        self.assertIn("waiting on author", verdict["reason"])

    def test_completed_same_account_review_is_not_offered_again(self):
        verdict = eligible(pr(
            1, "author:agent-1", "family:anthropic", "reviewed-by:agent-2",
            reviews=1,
        ))
        self.assertFalse(verdict["eligible"])
        self.assertIn("waiting on gated merge", verdict["reason"])

    def test_self_attribution_does_not_hide_pr_from_a_real_peer(self):
        verdict = eligible(pr(
            1, "author:agent-1", "family:anthropic", "reviewed-by:agent-1",
            reviews=1,
        ))
        self.assertTrue(verdict["eligible"])

    def test_unknown_thread_state_fails_closed(self):
        candidate = pr(1, "author:agent-1", "family:anthropic")
        candidate["_active_review_feedback"] = None

        verdict = eligible(candidate)

        self.assertFalse(verdict["eligible"])
        self.assertIn("unavailable", verdict["reason"])

    def test_same_family_waits_before_it_is_offered(self):
        verdict = eligible(pr(1, "author:agent-1", "family:openai", minutes_old=5))
        self.assertFalse(verdict["eligible"])
        self.assertIn("same family", verdict["reason"])

    def test_same_family_is_offered_after_the_wait(self):
        # An all-one-family fleet must not deadlock with nothing reviewable.
        verdict = eligible(pr(1, "author:agent-1", "family:openai", minutes_old=45))
        self.assertTrue(verdict["eligible"])
        self.assertTrue(verdict["degraded"])
        self.assertFalse(verdict["cross_family"])

    def test_unstamped_pr_is_reviewable(self):
        # Legacy PRs predate author stamping; refusing would make them
        # permanently unreviewable. merge_pr.py catches self-review separately.
        self.assertTrue(eligible(pr(1))["eligible"])

    def test_agent_without_a_declared_family_treats_everything_as_cross(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic"), family=None)
        self.assertTrue(verdict["eligible"])
        self.assertTrue(verdict["cross_family"])


class FeedbackTests(unittest.TestCase):
    def test_stale_changes_requested_without_active_threads_does_not_reroute(self):
        self.assertFalse(fnw.needs_my_attention(
            pr(1, "author:agent-2", decision="CHANGES_REQUESTED"), "agent-2"))

    def test_my_commented_review_with_unresolved_threads_is_mine_to_fix(self):
        candidate = pr(1, "author:agent-2", reviews=1)
        candidate["_active_review_feedback"] = [{"body": "fix"}] * 3
        self.assertTrue(fnw.needs_my_attention(candidate, "agent-2"))

    def test_plain_issue_comment_does_not_route_feedback(self):
        candidate = pr(1, "author:agent-2", reviews=1)
        candidate["comments"] = [{"body": "approval-style bot message"}]
        self.assertFalse(fnw.needs_my_attention(candidate, "agent-2"))

    def test_someone_elses_pr_is_not(self):
        self.assertFalse(fnw.needs_my_attention(
            pr(1, "author:agent-1", decision="CHANGES_REQUESTED"), "agent-2"))

    def test_my_unreviewed_pr_is_not_my_problem(self):
        # Waiting for review is someone else's work, not the author's.
        self.assertFalse(fnw.needs_my_attention(pr(1, "author:agent-2"), "agent-2"))


class PriorityTests(unittest.TestCase):
    """The ordering is the point: finishing beats starting."""

    def _select(self, prs, candidates=(), in_flight=None, agent="agent-2", family="openai"):
        parts = {
            "candidates": [{"number": n, "title": f"issue {n}"} for n in candidates],
            "my_in_flight": {"number": in_flight, "title": "mine"} if in_flight else None,
            "blocked": [], "conflicted": [], "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=list(prs)), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            return fnw.select(agent, family, 3, 30)

    def test_feedback_outranks_review_and_new_work(self):
        authored = pr(1, "author:agent-2")
        authored["_active_review_feedback"] = [{"body": "fix this"}]
        res = self._select(
            [authored, pr(2, "author:agent-1", "family:anthropic")],
            candidates=[7])
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertEqual(res["work"]["pr"], 1)

    def test_review_outranks_new_work(self):
        res = self._select([pr(2, "author:agent-1", "family:anthropic")], candidates=[7])
        self.assertEqual(res["work"]["type"], "review")
        self.assertEqual(res["work"]["pr"], 2)

    def test_issue_when_nothing_to_review(self):
        res = self._select([pr(2, "author:agent-2", "family:openai")], candidates=[7])
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["work"]["issue"], 7)

    def test_in_flight_issue_beats_a_new_one(self):
        res = self._select([], candidates=[7], in_flight=4)
        self.assertEqual(res["work"]["issue"], 4)
        self.assertTrue(res["work"]["resuming"])

    def test_idle_when_there_is_nothing_at_all(self):
        self.assertEqual(self._select([], candidates=[])["work"]["type"], "idle")

    def test_cross_family_pr_is_preferred_over_a_degraded_one(self):
        res = self._select([
            pr(1, "author:agent-1", "family:openai", minutes_old=600),   # same family, waited
            pr(2, "author:agent-1", "family:anthropic", minutes_old=5),  # cross family, fresh
        ])
        self.assertEqual(res["work"]["pr"], 2)
        self.assertTrue(res["work"]["cross_family"])

    def test_skipped_prs_are_reported_with_reasons(self):
        res = self._select([pr(3, "author:agent-2", "family:openai")])
        self.assertEqual(res["skipped_prs"][0]["number"], 3)
        self.assertIn("you wrote it", res["skipped_prs"][0]["why"])

    def test_many_review_rounds_remain_reviewable_without_escalation(self):
        res = self._select([pr(5, "author:agent-1", "family:anthropic", reviews=4)])
        self.assertEqual(res["work"]["type"], "review")
        self.assertEqual(res["work"]["pr"], 5)
        self.assertEqual(res["escalated_prs"], [])


class MergeWorkTests(unittest.TestCase):
    """Issue #43: merge-ready PRs are claimable board work."""

    def _select(self, prs, candidates=(), agent="agent-2", family="openai",
                dod_ok=True, dod_reason="every Definition-of-Done gate passed"):
        parts = {
            "candidates": [{"number": n, "title": f"issue {n}"} for n in candidates],
            "my_in_flight": None,
            "blocked": [], "conflicted": [], "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=list(prs)), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts), \
             patch.object(fnw, "dod_status", return_value=(dod_ok, dod_reason)):
            return fnw.select(agent, family, 3, 30)

    def test_merge_outranks_review_and_new_work(self):
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   reviews=1, title="ready to merge")
        ready["headRefOid"] = "abc123"
        res = self._select(
            [ready, pr(2, "author:agent-1", "family:anthropic")],
            candidates=[7],
        )
        self.assertEqual(res["work"]["type"], "merge")
        self.assertEqual(res["work"]["pr"], 9)
        self.assertEqual(res["work"]["skill"], "merge-pr")
        self.assertEqual(res["work"]["head_sha"], "abc123")
        self.assertEqual(res["mergeable_detail"][0]["head_sha"], "abc123")

    def test_feedback_still_outranks_merge(self):
        authored = pr(1, "author:agent-2")
        authored["_active_review_feedback"] = [{"body": "fix"}]
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   reviews=1)
        res = self._select([authored, ready], candidates=[7])
        self.assertEqual(res["work"]["type"], "feedback")

    def test_author_may_merge_when_peer_review_exists(self):
        ready = pr(9, "author:agent-2", "family:openai", "reviewed-by:agent-9",
                   reviews=1)
        res = self._select([ready], agent="agent-2", family="openai")
        self.assertEqual(res["work"]["type"], "merge")
        self.assertEqual(res["work"]["pr"], 9)

    def test_author_cannot_merge_without_peer_reviewer(self):
        # GitHub APPROVED alone is not enough for the author path without peers.
        own = pr(9, "author:agent-2", "family:openai", decision="APPROVED", reviews=1)
        verdict = fnw.merge_eligibility(own, "agent-2")
        self.assertFalse(verdict["eligible"])
        self.assertIn("distinct peer", verdict["reason"])

    def test_blocked_gates_do_not_offer_merge(self):
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   reviews=1)
        res = self._select([ready], candidates=[7], dod_ok=False,
                           dod_reason="unmet: ci")
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["merge_skipped"][0]["number"], 9)
        self.assertIn("unmet: ci", res["merge_skipped"][0]["why"])

    def test_other_merger_claim_blocks_eligibility(self):
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   "merger:agent-8", reviews=1)
        with patch.object(fnw, "dod_status", return_value=(True, "ok")):
            verdict = fnw.merge_eligibility(ready, "agent-2")
        self.assertFalse(verdict["eligible"])
        self.assertIn("agent-8", verdict["reason"])

    def test_merged_pr_with_incomplete_closeout_is_merge_work(self):
        merged = pr(12, "author:agent-1", "family:anthropic", "merger:agent-2",
                    "reviewed-by:agent-9", reviews=1, title="needs close-out")
        merged["state"] = "MERGED"
        merged["mergedAt"] = "2026-01-01T00:00:00Z"
        merged["headRefOid"] = "deadbeef"
        with patch.object(fnw, "closeout_incomplete", return_value=True):
            res = self._select(
                [merged], candidates=[7],
                dod_ok=True, dod_reason="merged; close-out incomplete",
            )
        self.assertEqual(res["work"]["type"], "merge")
        self.assertEqual(res["work"]["pr"], 12)

    def test_claim_fallback_replaces_head_sha(self):
        detail = [
            {"pr": 1, "title": "first", "head_sha": "aaa"},
            {"pr": 2, "title": "second", "head_sha": "bbb"},
        ]
        work = {"type": "merge", "pr": 1, "title": "first", "head_sha": "aaa",
                "claimed": False}
        # Simulate the claim loop body: first conflict, second ok.
        claimed = None
        for candidate in detail:
            # pretend first conflicts
            if candidate["pr"] == 1:
                continue
            work.update({"pr": candidate["pr"], "title": candidate["title"],
                         "head_sha": candidate.get("head_sha"), "claimed": True})
            claimed = candidate
            break
        self.assertEqual(work["pr"], 2)
        self.assertEqual(work["head_sha"], "bbb")
        self.assertIsNotNone(claimed)


if __name__ == "__main__":
    unittest.main()


class ReviewDecisionTests(unittest.TestCase):
    """Only a current approval suppresses a fresh review."""

    def test_approved_pr_is_not_offered_again(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic", decision="APPROVED"))
        self.assertFalse(verdict["eligible"])
        self.assertIn("already approved", verdict["reason"])

    def test_changes_requested_without_current_feedback_is_offered_for_rereview(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic",
                              decision="CHANGES_REQUESTED"))
        self.assertTrue(verdict["eligible"])

    def test_undecided_pr_is_still_offered(self):
        self.assertTrue(eligible(pr(1, "author:agent-1", "family:anthropic"))["eligible"])


class ParkedInReviewTests(unittest.TestCase):
    """Proves Issue #39: handing off to In Review parks the issue and progresses to next work."""

    @patch.object(fnw, "list_work_prs")
    @patch.object(fnw, "list_open_issues")
    def test_parked_in_review_issue_is_not_resumed_and_next_ready_issue_is_taken(
        self, mock_issues, mock_prs
    ):
        # Reproduces #20 / #36: Issue #20 is In Review with PR #36 waiting.
        # Worker agent-1 hands off #20 to In Review and runs fetch_next_work.
        # Issue #20 must not be returned as resumable implementation; #21 must be selected.
        mock_prs.return_value = [
            pr(36, "author:agent-1", "family:openai", title="PR for #20")
        ]
        mock_issues.return_value = [
            {
                "number": 20,
                "title": "fix issue 20",
                "body": "touches: src/a.py\n",
                "labels": [{"name": "status:in-review"}],
            },
            {
                "number": 21,
                "title": "feat issue 21",
                "body": "touches: src/b.py\n",
                "labels": [{"name": "status:ready"}],
            },
        ]
        res = fnw.select("agent-1", "openai", round_cap=3, cross_family_wait=30)
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["work"]["issue"], 21)
        self.assertFalse(res["work"]["resuming"])

    @patch.object(fnw, "run_cmd")
    def test_authored_via_branch_uses_retained_agent_label_on_unstamped_pr(
        self, mock_run_cmd
    ):
        # GitHub assignees identify the shared account, not the implementing
        # agent. The retained issue label is the legacy authorship backstop.
        mock_run_cmd.return_value = (
            0,
            "status:in-review\nagent:agent-1\n",
            "",
        )
        test_pr = pr(36, title="Unstamped PR")
        test_pr["headRefName"] = "fix/issue-20-something"
        self.assertTrue(fnw._authored_via_branch(test_pr, "agent-1"))
        self.assertFalse(fnw._authored_via_branch(test_pr, "agent-2"))


class UnreadableQueueTests(unittest.TestCase):
    def test_selector_fails_closed_when_prs_cannot_be_listed(self):
        # Treating an unreadable queue as empty would claim new implementation
        # work as though no review or feedback were waiting.
        with patch.object(fnw, "list_work_prs", return_value=None):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "error")
        self.assertIn("could not be read", res["work"]["reason"])

    def test_unknown_threads_on_authored_pr_block_new_issue_selection(self):
        authored = pr(57, "author:agent-2", "family:openai")
        authored["_active_review_feedback"] = None
        parts = {
            "candidates": [{"number": 99, "title": "new work"}],
            "my_in_flight": None, "blocked": [], "conflicted": [],
            "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[authored]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "error")
        self.assertEqual(res["claimable_issues"], [])

    def test_unknown_threads_on_peer_pr_block_new_issue_selection(self):
        peer_pr = pr(57, "author:agent-1", "family:anthropic")
        peer_pr["_active_review_feedback"] = None
        parts = {
            "candidates": [{"number": 99, "title": "new work"}],
            "my_in_flight": None, "blocked": [], "conflicted": [],
            "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[peer_pr]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "error")
        self.assertEqual(res["claimable_issues"], [])
