"""Reclaiming and adopting work an agent abandoned mid-task (#311).

The reaper used to skip any issue that showed evidence of work:

    if ts > cutoff or has_open_pr(num) or has_remote_branch(num):
        continue

so the only work it could recover was work nobody had started. An agent that
pushed a branch -- which it does early, long before the work is reviewable --
and then stopped stranded the issue, the branch, and the PR permanently.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402
import fetch_next_issue as fni  # noqa: E402


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class RemoteBranchParsingTests(unittest.TestCase):
    def test_parses_name_to_sha(self):
        text = ("abc123\trefs/heads/feat/issue-3-thing\n"
                "def456\trefs/heads/main\n")
        self.assertEqual(
            fni.parse_remote_branches(text),
            {"feat/issue-3-thing": "abc123", "main": "def456"})

    def test_ignores_tags_and_noise(self):
        self.assertEqual(fni.parse_remote_branches("abc\trefs/tags/v1\n\n"), {})


class WorkAbandonmentTests(unittest.TestCase):
    """Evidence of work now raises the bar for reclaiming, not blocks it."""

    def setUp(self):
        self.cutoff = datetime.now(timezone.utc) - timedelta(hours=4)

    def test_quiet_branch_and_quiet_pr_is_abandoned(self):
        prs = [{"number": 27, "body": "Closes #3", "headRefName": "feat/issue-3-x",
                "updatedAt": ago(9)}]
        branches = {"feat/issue-3-x": "abc"}
        with patch.object(fni, "branch_tip_time", return_value=ago_dt(9)):
            self.assertIs(
                fni.work_is_abandoned(3, self.cutoff, prs, branches), True)

    def test_recent_pr_activity_keeps_the_claim(self):
        prs = [{"number": 27, "body": "Closes #3", "headRefName": "feat/issue-3-x",
                "updatedAt": ago(1)}]
        with patch.object(fni, "branch_tip_time", return_value=ago_dt(9)):
            self.assertIs(
                fni.work_is_abandoned(3, self.cutoff, prs, {"feat/issue-3-x": "a"}),
                False)

    def test_recent_branch_commit_keeps_the_claim(self):
        # No PR yet, but the agent pushed twenty minutes ago: still working.
        with patch.object(fni, "branch_tip_time", return_value=ago_dt(0.3)):
            self.assertIs(
                fni.work_is_abandoned(3, self.cutoff, [], {"feat/issue-3-x": "a"}),
                False)

    def test_branch_with_no_pr_can_be_abandoned(self):
        # The exact case the old rule made permanent.
        with patch.object(fni, "branch_tip_time", return_value=ago_dt(30)):
            self.assertIs(
                fni.work_is_abandoned(3, self.cutoff, [], {"feat/issue-3-x": "a"}),
                True)

    def test_no_branch_and_no_pr_is_abandoned(self):
        self.assertIs(fni.work_is_abandoned(3, self.cutoff, [], {}), True)

    def test_unreadable_branch_time_is_undecided(self):
        # Absence of evidence is not evidence of abandonment: the caller retains.
        with patch.object(fni, "branch_tip_time", return_value=None):
            self.assertIsNone(
                fni.work_is_abandoned(3, self.cutoff, [], {"feat/issue-3-x": "a"}))

    def test_unreadable_pr_time_is_undecided(self):
        prs = [{"number": 27, "body": "Closes #3", "headRefName": "x",
                "updatedAt": None}]
        self.assertIsNone(fni.work_is_abandoned(3, self.cutoff, prs, {}))

    def test_another_issues_branch_is_not_consulted(self):
        with patch.object(fni, "branch_tip_time", return_value=ago_dt(0.1)):
            self.assertIs(
                fni.work_is_abandoned(3, self.cutoff, [], {"feat/issue-99-y": "a"}),
                True)


class AbandonedWorkNoteTests(unittest.TestCase):
    def test_note_names_the_branch_and_pr_for_the_successor(self):
        note = fni.abandoned_work_note(
            3, ["agent:codex-9f21"], 4,
            [{"number": 27, "body": "Closes #3", "headRefName": "feat/issue-3-x"}],
            {"feat/issue-3-x": "abc"})
        self.assertIn("#27", note)
        self.assertIn("feat/issue-3-x", note)
        self.assertIn("agent:codex-9f21", note)
        self.assertIn("--adopt", note)

    def test_note_says_so_when_nothing_was_left_behind(self):
        note = fni.abandoned_work_note(3, ["agent:codex-9f21"], 4, [], {})
        self.assertIn("No branch or pull request", note)


class AdoptPullRequestTests(unittest.TestCase):
    """Adoption moves ownership in place, preserving the work itself."""

    def snapshot(self, *, idle_hours=9, labels=None, state="OPEN"):
        return {
            "labels": labels if labels is not None else [
                {"name": "author:codex-9f21"}, {"name": "family:openai"}],
            "updatedAt": ago(idle_hours),
            "state": state,
        }

    def _adopt(self, snapshot, settled_author=None, **kwargs):
        """Drive adopt_pr. `settled_author` is who the read-back reports."""
        calls = []
        agent = kwargs.pop("agent", "claude-a3f19c")

        def fake_run_cmd(cmd, *args, **kw):
            calls.append(cmd)
            return 0, "", ""

        # adopt_pr reads the PR twice: once to qualify it, once to confirm the
        # write settled on this agent.
        after = dict(snapshot)
        after["labels"] = [{"name": f"author:{settled_author or agent}"}]
        snapshots = [snapshot, after]

        with patch.object(claim_issue, "run_gh_json",
                          side_effect=lambda *a, **k: snapshots.pop(0)
                          if snapshots else after), \
             patch.object(claim_issue, "ensure_label", return_value=True), \
             patch.object(claim_issue, "run_cmd", side_effect=fake_run_cmd):
            rc = claim_issue.adopt_pr(42, agent, **kwargs)
        return rc, calls

    def test_a_lost_adoption_race_reports_conflict_not_success(self):
        # Two successors can both qualify and both edit; the loser must not
        # print success and exit OK, or two agents believe they own one PR.
        rc, _calls = self._adopt(self.snapshot(), settled_author="gemini-77aa",
                                 family="anthropic")
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_two_author_labels_after_the_write_is_a_conflict(self):
        # pr_author returns the first match, so "the first one is us" would
        # report success while the PR carries ambiguous ownership.
        snapshot = self.snapshot()
        after = dict(snapshot)
        after["labels"] = [{"name": "author:claude-a3f19c"},
                           {"name": "author:gemini-77aa"}]
        with patch.object(claim_issue, "run_gh_json",
                          side_effect=[snapshot, after]), \
             patch.object(claim_issue, "ensure_label", return_value=True), \
             patch.object(claim_issue, "run_cmd", return_value=(0, "", "")):
            rc = claim_issue.adopt_pr(42, "claude-a3f19c", "anthropic")
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_an_unreadable_read_back_is_an_error(self):
        with patch.object(claim_issue, "run_gh_json",
                          side_effect=[self.snapshot(), None]), \
             patch.object(claim_issue, "ensure_label", return_value=True), \
             patch.object(claim_issue, "run_cmd", return_value=(0, "", "")):
            rc = claim_issue.adopt_pr(42, "claude-a3f19c", "anthropic")
        self.assertEqual(rc, claim_issue.EXIT_ERROR)

    def test_authorship_and_family_move_to_the_successor(self):
        rc, calls = self._adopt(self.snapshot(), family="anthropic")
        self.assertEqual(rc, claim_issue.EXIT_OK)
        edit = next(c for c in calls if c[:3] == ["gh", "pr", "edit"])
        self.assertIn("author:claude-a3f19c", edit)
        self.assertIn("family:anthropic", edit)
        self.assertIn("adopted-from:codex-9f21", edit)
        # The previous owner's stamps are removed, not merely shadowed.
        removed = [edit[i + 1] for i, tok in enumerate(edit)
                   if tok == "--remove-label"]
        self.assertIn("author:codex-9f21", removed)
        self.assertIn("family:openai", removed)

    def test_the_branch_and_pr_are_never_touched(self):
        _rc, calls = self._adopt(self.snapshot(), family="anthropic")
        flat = [tok for cmd in calls for tok in cmd]
        for destructive in ("close", "--delete-branch", "push"):
            self.assertNotIn(destructive, flat)

    def test_a_recently_updated_pr_is_not_taken(self):
        rc, calls = self._adopt(self.snapshot(idle_hours=1), family="anthropic")
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)
        self.assertFalse([c for c in calls if c[:3] == ["gh", "pr", "edit"]])

    def test_adopting_your_own_pr_is_refused(self):
        rc, _calls = self._adopt(self.snapshot(), agent="codex-9f21")
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_empty_agent_identity_is_refused_before_mutation(self):
        snapshot = self.snapshot()
        with patch.object(claim_issue, "run_gh_json", return_value=snapshot), \
             patch.object(claim_issue, "ensure_label") as ensure_label, \
             patch.object(claim_issue, "run_cmd") as run_cmd:
            rc = claim_issue.adopt_pr(42, "   ", "anthropic")
        self.assertEqual(rc, claim_issue.EXIT_ERROR)
        ensure_label.assert_not_called()
        run_cmd.assert_not_called()

    def test_an_unstamped_pr_cannot_be_adopted(self):
        rc, _calls = self._adopt(self.snapshot(labels=[]))
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_a_closed_pr_cannot_be_adopted(self):
        rc, _calls = self._adopt(self.snapshot(state="MERGED"))
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_an_unreadable_update_time_refuses_rather_than_assumes(self):
        snapshot = self.snapshot()
        snapshot["updatedAt"] = None
        rc, _calls = self._adopt(snapshot)
        self.assertEqual(rc, claim_issue.EXIT_CONFLICT)

    def test_missing_pr_is_an_error(self):
        with patch.object(claim_issue, "run_gh_json", return_value=None):
            self.assertEqual(claim_issue.adopt_pr(42, "claude-1"),
                             claim_issue.EXIT_ERROR)

    def test_adoption_is_audited_on_the_pr(self):
        _rc, calls = self._adopt(self.snapshot(), family="anthropic")
        comment = next(c for c in calls if c[:3] == ["gh", "pr", "comment"])
        body = comment[-1]
        self.assertIn("claude-a3f19c", body)
        self.assertIn("codex-9f21", body)
        self.assertIn("cannot review", body)


class AdoptedPullRequestReviewTests(unittest.TestCase):
    """Adoption composes with the (id, family) peer gate from #307."""

    def test_the_adopting_agent_still_cannot_review_its_own_pr(self):
        import merge_pr
        pr = {"labels": [{"name": "author:claude-a3f19c"},
                         {"name": "family:anthropic"},
                         {"name": "adopted-from:codex-9f21"},
                         {"name": "reviewed-by:claude-a3f19c"},
                         {"name": "reviewer-family:claude-a3f19c:anthropic"}]}
        peers, collisions, _unresolved = merge_pr.classify_reviewers(
            pr, ["claude-a3f19c"], "claude-a3f19c")
        self.assertEqual(peers, [])
        self.assertEqual(collisions, [])

    def test_the_previous_author_is_a_valid_peer_after_adoption(self):
        # If the original agent returns, it is now somebody else's reviewer.
        import merge_pr
        pr = {"labels": [{"name": "author:claude-a3f19c"},
                         {"name": "family:anthropic"},
                         {"name": "adopted-from:codex-9f21"},
                         {"name": "reviewed-by:codex-9f21"}]}
        peers, collisions, _unresolved = merge_pr.classify_reviewers(
            pr, ["codex-9f21"], "claude-a3f19c")
        self.assertEqual(peers, ["codex-9f21"])
        self.assertEqual(collisions, [])


def ago_dt(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


if __name__ == "__main__":
    unittest.main()
