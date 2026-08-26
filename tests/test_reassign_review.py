# +124 for #472 write-authorized marker provenance and atomic reassignment coverage.
# line-ceiling: 524
"""Tests for the governed review-reassignment helper added in #435."""

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr  # noqa: E402
import reassign_review as rr  # noqa: E402

HEAD = "0d2a6d0848b5d4e2e6ed03fff73885fbc81f832d"


def pr(*labels, state="OPEN", head=HEAD):
    return {"state": state, "headRefOid": head,
            "labels": [{"name": n} for n in labels]}


def comment(body, login="gillella", kind="User", association="OWNER"):
    """One REST issue comment with the provenance GitHub actually returns."""
    return {"body": body, "user": {"login": login, "type": kind},
            "author_association": association}


class CurrentAuthorityTests(unittest.TestCase):
    def test_single_review_label_is_returned(self):
        label, problem = rr.current_authority([{"name": "review:coderabbit"},
                                               {"name": "author:x"}])
        self.assertEqual(label, "review:coderabbit")
        self.assertIsNone(problem)

    def test_no_review_label_fails_closed(self):
        label, problem = rr.current_authority([{"name": "author:x"}])
        self.assertIsNone(label)
        self.assertIn("no review authority label", problem)

    def test_several_review_labels_fail_closed(self):
        label, problem = rr.current_authority(
            [{"name": "review:coderabbit"}, {"name": "review:sourcery"}])
        self.assertIsNone(label)
        self.assertIn("2 review labels", problem)

    def test_malformed_label_array_fails_closed(self):
        for labels in (None, "labels", [None], [{"name": 7}], [{}]):
            with self.subTest(labels=labels):
                label, problem = rr.current_authority(labels)
                self.assertIsNone(label)
                self.assertTrue(problem)


class ReassignmentHistoryTests(unittest.TestCase):
    @staticmethod
    def _history(comments, writers=("gillella",)):
        writers = None if writers is None else set(writers)
        with patch.object(rr, "get_repo_slug", return_value="owner/repo"), \
                patch.object(rr, "fetch_paginated_gh_api", return_value=comments), \
                patch.object(rr, "repository_write_logins", return_value=writers):
            return rr.reassignment_history(433)

    @staticmethod
    def _valid_marker():
        return rr.audit_body("review:coderabbit", "review:sourcery", "sourcery",
                             "provider stalled", HEAD)

    def test_complete_valid_audit_marker_is_returned(self):
        history = self._history([comment(self._valid_marker())])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["from"], "review:coderabbit")
        self.assertEqual(history[0]["to"], "review:sourcery")

    def test_malformed_audit_marker_fails_closed(self):
        body = "<!-- aru-review-reassignment:v1 not-json -->"
        self.assertIsNone(self._history([comment(body)]))

    def test_incomplete_comment_inventory_fails_closed(self):
        self.assertIsNone(self._history(None))

    def test_marker_from_an_untrusted_commenter_is_ignored(self):
        """Anyone who can see the PR can comment on it; that is not authority.

        Counting a drive-by marker would let an outsider fabricate a history
        that refuses the permitted fallback and blocks every later
        reassignment - an authorization bypass whose effect is denial of
        service. Ignoring rather than refusing is what denies that lever.
        """
        for login, kind, association in (
            ("outsider", "User", "NONE"),
            ("drive-by", "User", "CONTRIBUTOR"),
            ("first-timer", "User", "FIRST_TIME_CONTRIBUTOR"),
            ("some-app[bot]", "Bot", "OWNER"),
        ):
            with self.subTest(association=association, kind=kind):
                forged = comment(self._valid_marker(), login=login, kind=kind,
                                 association=association)
                self.assertEqual(self._history([forged]), [])
                # A trusted record alongside it still counts exactly once.
                mixed = self._history([forged, comment(self._valid_marker())])
                self.assertEqual(len(mixed), 1)

    def test_read_only_members_and_collaborators_cannot_forge_history(self):
        """GitHub's MEMBER/COLLABORATOR association does not imply push;
        repository permission, not organization relationship, is authority."""
        for association in ("MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                forged = comment(self._valid_marker(), login="read-only",
                                 association=association)
                self.assertEqual(self._history([forged]), [])

    def test_unreadable_writer_roster_fails_closed(self):
        self.assertIsNone(self._history([comment(self._valid_marker())], writers=None))

    def test_malformed_marker_from_an_untrusted_commenter_is_not_fatal(self):
        forged = comment("<!-- aru-review-reassignment:v1 not-json -->",
                         login="outsider", association="NONE")
        self.assertEqual(self._history([forged]), [])

    def test_unreadable_provenance_on_a_marker_fails_closed(self):
        """Neither trusted nor untrusted: the boundary cannot be located."""
        marker = self._valid_marker()
        for missing in ({"body": marker},
                        {"body": marker, "author_association": "OWNER"},
                        {"body": marker, "user": None, "author_association": "OWNER"},
                        {"body": marker, "user": {"login": "", "type": "User"},
                         "author_association": "OWNER"}):
            with self.subTest(comment=missing):
                self.assertIsNone(self._history([missing]))

    def test_comment_association_is_not_part_of_authorization(self):
        marker = self._valid_marker()
        writer = {"body": marker,
                  "user": {"login": "gillella", "type": "User"}}
        self.assertEqual(len(self._history([writer])), 1)

    def test_unreadable_provenance_without_a_marker_is_ignored(self):
        """Ordinary chatter has no provenance requirement to fail closed on."""
        self.assertEqual(self._history([{"body": "looks good to me"}]), [])


class ReassignHarness:
    """Shared fixture driver for every reassignment scenario."""

    REASON = "CodeRabbit reported Review rate limited at abc1234"

    def _run(self, snapshot, service="sourcery",
             edit_results=((0, "", ""), (0, "", "")),
             comment_results=((0, "", ""), (0, "", "")),
             reviewer="agent-2", family="openai", history=(),
             histories=None, snapshots=None, reviewer_login=""):
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            group = "comment" if "comment" in cmd else "edit"
            results = comment_results if group == "comment" else edit_results
            index = len([c for c in calls if (group in c)]) - 1
            return results[min(index, len(results) - 1)]

        # `reassign` re-reads the snapshot and the history before and after the
        # write, so the fixtures are sequences: pass `snapshots`/`histories` to
        # model a concurrent operator moving underneath this one.
        snapshot_reads = list(snapshots) if snapshots is not None else [snapshot]
        if histories is not None:
            history_reads = [None if item is None else list(item) for item in histories]
        else:
            initial = list(history)
            if isinstance(snapshot, dict):
                existing, _ = rr.current_authority(snapshot.get("labels", []))
                own = {"from": existing, "to": rr.FALLBACK_LABELS.get(service),
                       "head": snapshot.get("headRefOid"), "reason": self.REASON}
                history_reads = [initial, initial, [*initial, own]]
            else:
                history_reads = [initial]

        def next_read(sequence):
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        with patch.object(rr, "run_gh_json",
                          side_effect=lambda *a, **k: next_read(snapshot_reads)), \
             patch.object(rr, "reassignment_history",
                          side_effect=lambda *a, **k: next_read(history_reads)), \
             patch.object(rr, "reassignment_lock", return_value=nullcontext(True)), \
             patch.object(rr, "authenticated_login", return_value="gillella"), \
             patch.object(rr, "ensure_label", return_value=True), \
             patch.object(rr, "run_cmd", side_effect=fake_run_cmd):
            code = rr.reassign(433, service, self.REASON, reviewer, family,
                               reviewer_login)
        return code, calls

    @staticmethod
    def _comments(calls):
        return [c[-1] for c in calls if "comment" in c]


class ReassignTests(ReassignHarness, unittest.TestCase):
    def test_clean_swap_adds_before_removing(self):
        code, calls = self._run(pr("review:coderabbit", "author:x"))
        self.assertEqual(code, rr.EXIT_OK)
        edits = [c for c in calls if "edit" in c]
        self.assertIn("--add-label", edits[0])
        self.assertIn("review:sourcery", edits[0])
        self.assertIn("--remove-label", edits[1])
        self.assertIn("review:coderabbit", edits[1])

    def test_reason_and_head_are_recorded_on_the_pull_request(self):
        _, calls = self._run(pr("review:coderabbit"))
        audit = self._comments(calls)[0]
        self.assertIn(self.REASON, audit)
        self.assertIn(HEAD, audit)
        self.assertIn("review:sourcery", audit)
        self.assertIn("aru-review-reassignment:v1", audit)

    def test_each_service_is_triggered_with_its_own_command(self):
        for service in sorted(rr.EXTERNAL_FALLBACK_LABELS):
            with self.subTest(service=service):
                existing = "review:codeant" if service == "coderabbit" else "review:coderabbit"
                code, calls = self._run(pr(existing), service=service)
                self.assertEqual(code, rr.EXIT_OK)
                self.assertEqual(self._comments(calls)[1],
                                 rr.SERVICE_TRIGGERS[service])

    def test_codeant_is_a_supported_target(self):
        code, calls = self._run(pr("review:coderabbit"), service="codeant")
        self.assertEqual(code, rr.EXIT_OK)
        self.assertIn("review:codeant", [c[-1] for c in calls if "--add-label" in c])

    def test_already_switched_pr_is_refused(self):
        for service, label in sorted(rr.EXTERNAL_FALLBACK_LABELS.items()):
            with self.subTest(service=service):
                code, calls = self._run(pr(label), service=service)
                self.assertEqual(code, rr.EXIT_CONFLICT)
                self.assertEqual([c for c in calls if "edit" in c], [])

    def test_any_initial_external_authority_can_move_once(self):
        for existing, target in (("review:sourcery", "codeant"),
                                 ("review:codeant", "coderabbit")):
            with self.subTest(existing=existing, target=target):
                code, calls = self._run(pr(existing), service=target)
                self.assertEqual(code, rr.EXIT_OK)
                self.assertIn(f"review:{target}", [
                    c[-1] for c in calls if "--add-label" in c])

    def test_prior_external_reassignment_blocks_a_second_external_hop(self):
        prior = {"from": "review:coderabbit", "to": "review:codeant", "head": HEAD}
        code, calls = self._run(
            pr("review:codeant"), service="sourcery", history=[prior])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([c for c in calls if "edit" in c], [])

    def test_agent_fallback_can_follow_any_external_authority(self):
        for existing in ("review:coderabbit", "review:sourcery", "review:codeant"):
            with self.subTest(existing=existing):
                code, calls = self._run(pr(existing, "author:agent-1"), service="agent")
                self.assertEqual(code, rr.EXIT_OK)
                edits = [call for call in calls if "edit" in call]
                self.assertIn("review:agent", edits[0])
                self.assertIn("reviewer:agent-2", edits[0])
                self.assertEqual(len(self._comments(calls)), 1)

    def test_agent_fallback_refuses_impossible_repeated_external_history(self):
        prior = {"from": "review:coderabbit", "to": "review:sourcery", "head": HEAD}
        second = {"from": "review:sourcery", "to": "review:codeant", "head": HEAD}
        code, calls = self._run(
            pr("review:codeant", "author:agent-1"), service="agent",
            history=[prior, second])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])

    def test_agent_fallback_refuses_history_that_does_not_end_at_live_authority(self):
        prior = {"from": "review:coderabbit", "to": "review:sourcery", "head": HEAD}
        code, calls = self._run(
            pr("review:codeant", "author:agent-1"), service="agent", history=[prior])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])

    def test_agent_fallback_records_identity_family_head_and_reason(self):
        code, calls = self._run(pr("review:codeant", "author:agent-1"), service="agent")
        self.assertEqual(code, rr.EXIT_OK)
        audit = self._comments(calls)[0]
        for expected in ("aru-agent-review-assignment:v1", "agent-2", "openai",
                         HEAD, self.REASON):
            self.assertIn(expected, audit)

    def test_agent_fallback_rejects_self_review_and_ambiguous_author(self):
        for snapshot, reviewer in (
            (pr("review:coderabbit", "author:agent-1"), "agent-1"),
            (pr("review:coderabbit"), "agent-2"),
            (pr("review:coderabbit", "author:a", "author:b"), "agent-2"),
        ):
            with self.subTest(snapshot=snapshot, reviewer=reviewer):
                code, calls = self._run(snapshot, service="agent", reviewer=reviewer)
                self.assertEqual(code, rr.EXIT_CONFLICT)
                self.assertFalse([call for call in calls if "edit" in call])

    def test_unknown_existing_authority_is_refused(self):
        code, _ = self._run(pr("review:manual"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_missing_authority_label_is_refused(self):
        code, _ = self._run(pr("author:x"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_duplicate_authority_labels_are_refused(self):
        code, _ = self._run(pr("review:coderabbit", "review:sourcery"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_closed_pr_is_refused(self):
        code, _ = self._run(pr("review:coderabbit", state="MERGED"))
        self.assertEqual(code, rr.EXIT_CONFLICT)

    def test_unreadable_pr_fails_closed(self):
        code, _ = self._run(None)
        self.assertEqual(code, rr.EXIT_ERROR)

    def test_unreadable_head_fails_closed(self):
        """The audit record is exact-head bound, like the gate it feeds."""
        for head in (None, "", "abc", 7, "z" * 40):
            with self.subTest(head=head):
                code, calls = self._run(pr("review:coderabbit", head=head))
                self.assertEqual(code, rr.EXIT_ERROR)
                self.assertEqual([c for c in calls if "edit" in c], [])

    def test_failed_add_leaves_the_original_assignment_intact(self):
        """A partial swap must never strip the only reviewer."""
        code, calls = self._run(pr("review:coderabbit"),
                                edit_results=((1, "", "denied"), (0, "", "")))
        self.assertEqual(code, rr.EXIT_ERROR)
        self.assertEqual([c for c in calls if "--remove-label" in c], [])

    def test_failed_remove_reports_the_two_label_state(self):
        code, _ = self._run(pr("review:coderabbit"),
                            edit_results=((0, "", ""), (1, "", "denied")))
        self.assertEqual(code, rr.EXIT_ERROR)

    def test_unrecorded_reason_fails_closed(self):
        """A reassignment nobody can audit is a reassignment that did not happen."""
        code, calls = self._run(pr("review:coderabbit"),
                                comment_results=((1, "", "denied"), (0, "", "")))
        self.assertEqual(code, rr.EXIT_ERROR)
        self.assertEqual(len(self._comments(calls)), 1)
        self.assertEqual([call for call in calls if "--remove-label" in call], [])

    def test_failed_trigger_fails_closed(self):
        code, _ = self._run(pr("review:coderabbit"),
                            comment_results=((0, "", ""), (1, "", "denied")))
        self.assertEqual(code, rr.EXIT_ERROR)


class ConcurrentReassignmentTests(ReassignHarness, unittest.TestCase):
    """Two operators reassigning the same PR must not fabricate a rotation.

    Both can read an empty history, add the same label, and post a valid audit
    record. The history then shows two moves, so the terminal agent fallback -
    which refuses a history longer than one - is blocked forever even though
    authority moved exactly once.
    """

    PRIOR = {"from": "review:coderabbit", "to": "review:codeant",
             "head": HEAD, "reason": "provider stalled"}

    def test_atomic_lock_contention_refuses_before_any_authority_write(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if command[:4] == ["gh", "api", "--method", "POST"]:
                return 1, "", "reference already exists"
            return 0, "", ""

        with patch.object(rr, "run_gh_json", return_value=pr("review:coderabbit")), \
                patch.object(rr, "reassignment_history", return_value=[]), \
                patch.object(rr, "ensure_label", return_value=True), \
                patch.object(rr, "run_cmd", side_effect=run):
            code = rr.reassign(433, "sourcery", self.REASON)
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual(calls[0][:4], ["gh", "api", "--method", "POST"])
        self.assertEqual([cmd for cmd in calls if cmd[:3] == ["gh", "pr", "edit"]], [])

    def test_history_landing_before_the_write_refuses_without_mutating(self):
        code, calls = self._run(
            pr("review:coderabbit", "author:agent-1"),
            histories=[[], [self.PRIOR]])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])
        self.assertEqual(self._comments(calls), [])

    def test_authority_moving_before_the_write_refuses_without_mutating(self):
        code, calls = self._run(
            pr("review:coderabbit"),
            snapshots=[pr("review:coderabbit"), pr("review:codeant")])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])

    def test_head_advancing_before_the_write_refuses_without_mutating(self):
        code, calls = self._run(
            pr("review:coderabbit"),
            snapshots=[pr("review:coderabbit"), pr("review:coderabbit", head="b" * 40)])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])

    def test_ambiguous_authority_at_recheck_refuses_without_mutating(self):
        code, calls = self._run(
            pr("review:coderabbit"),
            snapshots=[pr("review:coderabbit"),
                       pr("review:coderabbit", "review:sourcery")])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "edit" in call], [])

    def test_a_rival_audit_landing_inside_the_window_stops_the_swap(self):
        """Detected after the audit, when no rollback is possible: leave both
        labels so the merge gate refuses loudly rather than silently shipping a
        pull request whose history overstates how often authority moved."""
        rival = {"from": "review:coderabbit", "to": "review:sourcery",
                 "head": HEAD, "reason": "rival operator"}
        code, calls = self._run(
            pr("review:coderabbit"),
            histories=[[], [], [rival, self.PRIOR]])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "--remove-label" in call], [])
        self.assertEqual(len(self._comments(calls)), 1)

    def test_only_this_commands_own_record_landing_completes_the_swap(self):
        own = {"from": "review:coderabbit", "to": "review:sourcery",
               "head": HEAD, "reason": ReassignHarness.REASON}
        code, calls = self._run(pr("review:coderabbit"), histories=[[], [], [own]])
        self.assertEqual(code, rr.EXIT_OK)
        self.assertIn("review:coderabbit",
                      [call[-1] for call in calls if "--remove-label" in call])

    def test_success_without_this_commands_visible_audit_keeps_both_labels(self):
        code, calls = self._run(pr("review:coderabbit"), histories=[[], [], []])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "--remove-label" in call], [])

    def test_a_same_cardinality_rival_audit_cannot_stand_in_for_this_command(self):
        rival = {"from": "review:coderabbit", "to": "review:codeant",
                 "head": HEAD, "reason": "rival operator"}
        code, calls = self._run(pr("review:coderabbit"), histories=[[], [], [rival]])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "--remove-label" in call], [])

    def test_unreadable_history_after_the_audit_fails_closed(self):
        code, calls = self._run(pr("review:coderabbit"), histories=[[], [], None])
        self.assertEqual(code, rr.EXIT_CONFLICT)
        self.assertEqual([call for call in calls if "--remove-label" in call], [])


class AuthorizedReviewerLoginTests(ReassignHarness, unittest.TestCase):
    """The emergency assignment names the one account allowed to review."""

    def test_assignment_records_the_authorized_github_login(self):
        code, calls = self._run(pr("review:codeant", "author:agent-1"),
                                service="agent", reviewer_login="reviewer-acct")
        self.assertEqual(code, rr.EXIT_OK)
        audit = self._comments(calls)[0]
        self.assertIn('"reviewer_login":"reviewer-acct"', audit)
        self.assertIn("@reviewer-acct", audit)

    def test_authenticated_login_is_the_default_authorized_account(self):
        code, calls = self._run(pr("review:codeant", "author:agent-1"), service="agent")
        self.assertEqual(code, rr.EXIT_OK)
        self.assertIn('"reviewer_login":"gillella"', self._comments(calls)[0])

    def test_unresolvable_or_malformed_login_refuses_before_any_write(self):
        for login in ("not a login", "-leading-hyphen", "x" * 60, "a/b"):
            with self.subTest(login=login):
                code, calls = self._run(pr("review:codeant", "author:agent-1"),
                                        service="agent", reviewer_login=login)
                self.assertEqual(code, rr.EXIT_ERROR)
                self.assertEqual([call for call in calls if "edit" in call], [])

    def test_external_reassignment_needs_no_reviewer_login(self):
        with patch.object(rr, "authenticated_login", return_value=None):
            code, calls = self._run(pr("review:coderabbit"), service="sourcery")
        self.assertEqual(code, rr.EXIT_OK)
        self.assertNotIn("reviewer_login", self._comments(calls)[0])

    def test_authenticated_login_reads_the_gh_identity(self):
        with patch.object(rr, "run_cmd", return_value=(0, "gillella\n", "")):
            self.assertEqual(rr.authenticated_login(), "gillella")
        for result in ((1, "", "no auth"), (0, "", ""), (0, "not a login", "")):
            with self.subTest(result=result), \
                    patch.object(rr, "run_cmd", return_value=result):
                self.assertIsNone(rr.authenticated_login())


class ArgumentTests(unittest.TestCase):
    def test_unknown_service_is_refused(self):
        for service in ("qodo", "claude", "", None):
            with self.subTest(service=service):
                self.assertEqual(rr.reassign(1, service, "why"), rr.EXIT_ERROR)

    def test_empty_reason_is_refused(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                self.assertEqual(rr.reassign(1, "sourcery", reason), rr.EXIT_ERROR)

    def test_agent_target_requires_safe_identity_and_known_family(self):
        for reviewer, family in (("", "openai"), ("bad/id", "openai"),
                                 ("agent-2", ""), ("agent-2", "unknown")):
            with self.subTest(reviewer=reviewer, family=family):
                self.assertEqual(
                    rr.reassign(1, "agent", "external reviewers exhausted",
                                reviewer, family), rr.EXIT_ERROR)

    def test_every_external_service_is_a_reassignment_target(self):
        self.assertEqual(
            set(rr.EXTERNAL_FALLBACK_LABELS), {"coderabbit", "sourcery", "codeant"})

    def test_every_target_is_an_authority_the_merge_gate_recognises(self):
        """A label this helper can apply but the gate cannot read strands the PR."""
        for label in rr.FALLBACK_LABELS.values():
            with self.subTest(label=label):
                self.assertIn(label, merge_pr.REVIEW_SERVICE_LABELS)
        self.assertEqual(set(rr.SERVICE_TRIGGERS),
                         set(rr.EXTERNAL_FALLBACK_LABELS))


if __name__ == "__main__":
    unittest.main()
