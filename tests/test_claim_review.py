import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue


class ReviewLabelParsingTests(unittest.TestCase):
    def test_reads_the_holder(self):
        self.assertEqual(claim_issue.reviewed_by(["reviewer:agent-3", "type:feat"]), "agent-3")

    def test_no_holder(self):
        self.assertIsNone(claim_issue.reviewed_by(["type:feat", "author:agent-1"]))

    def test_holders_are_sorted_so_the_tie_break_is_deterministic(self):
        held = claim_issue.reviewer_labels(["reviewer:zeta", "reviewer:alpha"])
        self.assertEqual(held, ["reviewer:alpha", "reviewer:zeta"])


class ClaimReviewTests(unittest.TestCase):
    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "_pr_labels")
    def test_uncontested_claim_succeeds(self, labels, _ensure, _run, _sleep):
        labels.side_effect = [[], ["reviewer:agent-2"]]
        self.assertEqual(claim_issue.claim_review(7, "agent-2"), claim_issue.EXIT_OK)

    @patch.object(claim_issue, "_pr_labels", return_value=["reviewer:agent-9"])
    def test_already_held_by_another_agent_is_a_conflict(self, _labels):
        self.assertEqual(claim_issue.claim_review(7, "agent-2"), claim_issue.EXIT_CONFLICT)

    @patch.object(claim_issue, "_pr_labels", return_value=["reviewer:agent-2"])
    def test_already_mine_resumes(self, _labels):
        self.assertEqual(claim_issue.claim_review(7, "agent-2"), claim_issue.EXIT_OK)

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "_pr_labels")
    def test_race_loser_releases_only_its_own_label(self, labels, _ensure, run_cmd, _sleep):
        # Both agents saw "unclaimed" and both wrote. Lowest-sorting id wins,
        # and the loser must not touch the winner's label.
        labels.side_effect = [[], ["reviewer:agent-a", "reviewer:agent-b"]]
        result = claim_issue.claim_review(7, "agent-b")
        self.assertEqual(result, claim_issue.EXIT_CONFLICT)
        cleanup = run_cmd.call_args_list[-1].args[0]
        self.assertEqual(
            cleanup, ["gh", "pr", "edit", "7", "--remove-label", "reviewer:agent-b"])

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "_pr_labels")
    def test_race_winner_keeps_the_claim(self, labels, _ensure, _run, _sleep):
        labels.side_effect = [[], ["reviewer:agent-a", "reviewer:agent-b"]]
        self.assertEqual(claim_issue.claim_review(7, "agent-a"), claim_issue.EXIT_OK)

    @patch.object(claim_issue, "_pr_labels", return_value=None)
    def test_unreadable_pr_is_an_error(self, _labels):
        self.assertEqual(claim_issue.claim_review(7, "agent-2"), claim_issue.EXIT_ERROR)

    @patch.object(claim_issue.time, "sleep")
    @patch.object(claim_issue, "run_cmd", return_value=(0, "", ""))
    @patch.object(claim_issue, "ensure_label", return_value=True)
    @patch.object(claim_issue, "_pr_labels")
    def test_claiming_a_review_never_moves_the_board(self, labels, _ensure, _run, _sleep):
        # The linked issue stays In Review while its PR is reviewed; a review
        # is not separate board work.
        labels.side_effect = [[], ["reviewer:agent-2"]]
        with patch.object(claim_issue, "update_status") as update:
            claim_issue.claim_review(7, "agent-2")
            update.assert_not_called()


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
    def test_a_claim_that_produced_a_review_is_left_alone(self, run_cmd):
        # The claim is spent, not stale. Clearing it could invite a duplicate.
        run_cmd.side_effect = [
            self._prs([{"number": 5, "labels": [{"name": "reviewer:done"}],
                        "updatedAt": self.OLD, "reviews": [{"state": "APPROVED"}]}]),
        ]
        self.assertEqual(claim_issue.reap_stale_reviews(4), [])

    @patch.object(claim_issue, "run_cmd")
    def test_recent_claim_is_left_alone(self, run_cmd):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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


if __name__ == "__main__":
    unittest.main()
