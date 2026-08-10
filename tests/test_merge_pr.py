import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        # Names the attribution the gate reads and the command that writes it,
        # so the remedy is executable rather than a label to invent.
        self.assertIn("reviewed-by:", msg)
        self.assertIn("--complete-review", msg)


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

    def test_review_without_attribution_is_refused(self):
        # Unattributable on a stamped PR: it cannot be told apart from a
        # self-review, so it must not pass.
        ok, msg = merge_pr.check_reviews(labelled("author:agent-1"), 0)
        self.assertFalse(ok)
        self.assertIn("reviewed-by:", msg)


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


class ClaimIsNotAttestationTests(unittest.TestCase):
    """A review claim records queue occupancy, not that anyone read the diff.

    An earlier revision of this fix accepted `reviewer:` as proof of review.
    That let the author leave a same-account COMMENTED review, any peer claim
    the PR, and the gate pass before that peer had looked at anything. On the
    repository's only merge gate.
    """

    def test_a_peer_claim_alone_does_not_satisfy_the_gate(self):
        # The exploit, verbatim: author's own review + a peer's bare claim.
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-2"), 0)
        self.assertFalse(ok)
        self.assertIn("complete-review", msg)

    def test_the_refusal_names_the_claimant_and_the_command(self):
        _, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-2"), 0)
        self.assertIn("agent-2", msg)
        self.assertIn("--complete-review", msg)

    def test_completed_attribution_satisfies_the_gate(self):
        ok, msg = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)
        self.assertIn("agent-2", msg)

    def test_a_claim_alongside_completed_attribution_is_fine(self):
        # Completion normally releases the claim, but a failed release must
        # not block a merge the attribution has already earned.
        ok, _ = merge_pr.check_reviews(
            labelled("author:agent-1", "reviewer:agent-2", "reviewed-by:agent-2"), 0)
        self.assertTrue(ok)

    def test_self_attribution_is_still_a_self_review(self):
        ok, msg = merge_pr.check_reviews(
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
    @patch.object(merge_pr, "unresolved_threads", return_value=0)
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
            "labels": [],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 2,
            "deletions": 1,
        }
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "9"]):
            self.assertEqual(merge_pr.main(), merge_pr.EXIT_ERROR)

        execute.assert_called_once()
        _close.assert_called_once_with(7)
        _done.assert_called_once_with(7)
        _issue_claim.assert_called_once_with(7)
        _review_claim.assert_called_once_with(9)


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

    @patch.object(merge_pr, "clear_review_claims", return_value=(True, "review clear"))
    @patch.object(merge_pr, "clear_issue_claims", return_value=(True, "issue clear"))
    @patch.object(merge_pr, "reconcile_issue_done", return_value=(True, "done"))
    @patch.object(merge_pr, "ensure_issue_closed", return_value=(True, "closed"))
    @patch.object(merge_pr, "delete_remote_branch", return_value=(True, "remote"))
    @patch.object(merge_pr, "retain_local_branch", return_value=(True, "local"))
    @patch.object(merge_pr, "prune_worktree", return_value=(True, "worktree"))
    @patch.object(merge_pr.os, "chdir")
    def test_changes_to_surviving_root_before_pruning_caller_worktree(
        self, chdir, prune, _local, _remote, _close, _done, _issue, _review
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

    @patch.object(merge_pr, "_gh_json", return_value={"state": "CLOSED"})
    def test_closed_issue_is_already_done(self, _gh):
        ok, message = merge_pr.ensure_issue_closed(7)
        self.assertTrue(ok)
        self.assertIn("already closed", message)

    @patch.object(merge_pr, "_gh_json", return_value={"labels": []})
    def test_absent_claim_labels_are_already_done(self, _gh):
        self.assertTrue(merge_pr.clear_issue_claims(7)[0])
        self.assertTrue(merge_pr.clear_review_claims(9)[0])


if __name__ == "__main__":
    unittest.main()
