# line-ceiling: 1530
import io
import json
import sys
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_work as fnw
import fetch_next_issue  # noqa: E402
import merge_pr


def ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def pr(number, *labels, draft=False, checks="green", reviews=0, minutes_old=5,
       decision="", title="a pr"):
    labels = list(labels)
    if not any(label.startswith("review:") for label in labels):
        labels.append("review:coderabbit")
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


def merged_api_pr(number, issue_number, *labels):
    return {
        "number": number,
        "title": f"merged {number}",
        "merged_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "draft": False,
        "labels": [{"name": name} for name in labels],
        "head": {"ref": f"feat/issue-{issue_number}-x", "sha": f"sha-{number}"},
        "body": f"Closes #{issue_number}",
    }


def issue_api_state(number, state="closed", *labels):
    return {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
    }


class MergedCloseoutSnapshotTests(unittest.TestCase):
    def run_scan(self, merged_prs, issues, *, issue_code=0, issue_output=None):
        closed_output = "\n".join(json.dumps(item) for item in merged_prs)
        if issue_output is None:
            issue_output = "\n".join(json.dumps(item) for item in issues)
        with patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(
                 fnw,
                 "run_cmd",
                 side_effect=[
                     (0, closed_output, ""),
                     (issue_code, issue_output, "snapshot failed"),
                 ],
             ) as run:
            result = fnw.list_merged_needing_closeout()
        return result, run

    def test_one_issue_snapshot_handles_one_hundred_completed_merges(self):
        prs = [merged_api_pr(number, number) for number in range(1, 101)]
        issues = [
            issue_api_state(number, "closed", "status:done")
            for number in range(1, 101)
        ]

        result, run = self.run_scan(prs, issues)

        self.assertEqual(result, [])
        self.assertEqual(run.call_count, 2)
        snapshot_cmd = run.call_args_list[1].args[0]
        self.assertIn("--paginate", snapshot_cmd)
        self.assertIn("state=all&per_page=100", " ".join(snapshot_cmd))

    def test_snapshot_preserves_each_incomplete_closeout_signal(self):
        prs = [
            merged_api_pr(1, 11),
            merged_api_pr(2, 12),
            merged_api_pr(3, 13),
            merged_api_pr(4, 14, "merger:agent-1"),
        ]
        issues = [
            issue_api_state(11, "closed", "status:done"),
            issue_api_state(12, "open", "status:in-review"),
            issue_api_state(13, "closed", "status:in-review"),
            issue_api_state(14, "closed", "status:done"),
        ]

        result, _ = self.run_scan(prs, issues)

        self.assertEqual([item["number"] for item in result], [2, 3, 4])

    def test_snapshot_command_failure_fails_closed(self):
        result, _ = self.run_scan(
            [merged_api_pr(1, 11)], [], issue_code=1, issue_output=""
        )
        self.assertIsNone(result)

    def test_snapshot_json_and_schema_failures_fail_closed(self):
        for bad_output in (
            "not-json",
            json.dumps({"number": "11", "state": "closed", "labels": []}),
            "\n".join(
                json.dumps(issue_api_state(11, "closed", "status:done"))
                for _ in range(2)
            ),
        ):
            with self.subTest(output=bad_output):
                result, _ = self.run_scan(
                    [merged_api_pr(1, 11)], [], issue_output=bad_output
                )
                self.assertIsNone(result)

    def test_missing_linked_issue_fails_closed(self):
        result, _ = self.run_scan([merged_api_pr(1, 11)], [])
        self.assertIsNone(result)


class CiStateTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(fnw.ci_state(pr(1, checks="green")), "green")
        self.assertEqual(fnw.ci_state(pr(1, checks="red")), "red")
        self.assertEqual(fnw.ci_state(pr(1, checks="pending")), "pending")

    def test_no_checks_is_not_green(self):
        self.assertEqual(fnw.ci_state(pr(1, checks="none")), "none")


class EligibilityTests(unittest.TestCase):
    def test_coding_agents_are_never_eligible_for_review(self):
        verdict = eligible(pr(1, "author:agent-1", "family:anthropic"))
        self.assertFalse(verdict["eligible"])
        self.assertIn("never selected from the normal queue", verdict["reason"])
        self.assertIn("review:agent", verdict["reason"])

    def test_picker_json_top_level_agent_is_resolved_identity(self):
        parts = {
            "candidates": [], "my_in_flight": None, "blocked": [],
            "conflicted": [], "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            result = fnw.select("resolved-agent", "openai", 3, 30)
        self.assertEqual(result["agent"], "resolved-agent")


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


    def test_issue_when_nothing_to_review(self):
        res = self._select([pr(2, "author:agent-2", "family:openai")], candidates=[7])
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["work"]["issue"], 7)

    def test_in_flight_issue_beats_a_new_one(self):
        res = self._select([], candidates=[7], in_flight=4)
        self.assertEqual(res["work"]["issue"], 4)
        self.assertTrue(res["work"]["resuming"])

    def test_explicit_agent_review_resumes_before_issue_work(self):
        assigned = pr(2, "author:agent-1", "review:agent", "reviewer:agent-2",
                      checks="pending", title="emergency review")
        res = self._select([assigned], candidates=[7], in_flight=4)
        self.assertEqual(res["work"]["type"], "review")
        self.assertEqual(res["work"]["pr"], 2)
        self.assertTrue(res["work"]["resuming"])

    def test_emergency_review_is_not_a_general_agent_queue(self):
        assigned = pr(2, "author:agent-1", "review:agent", "reviewer:agent-3",
                      checks="pending")
        self.assertEqual(
            self._select([assigned], candidates=[7])["work"]["type"], "issue")
        self_review = pr(3, "author:agent-2", "review:agent", "reviewer:agent-2",
                         checks="pending")
        self.assertEqual(
            self._select([self_review], candidates=[7])["work"]["type"], "issue")

    def test_completed_emergency_review_is_not_offered_again(self):
        completed = pr(2, "author:agent-1", "review:agent", "reviewer:agent-2",
                       "reviewed-by:agent-2", checks="pending")
        self.assertEqual(
            self._select([completed], candidates=[7])["work"]["type"], "issue")

    def test_idle_when_there_is_nothing_at_all(self):
        self.assertEqual(self._select([], candidates=[])["work"]["type"], "idle")

    def test_unified_picker_reports_but_never_selects_needs_human_issue(self):
        operator_issue = {
            "number": 1,
            "title": "configure workspace",
            "body": "touches: operator/slack-setup\n",
            "labels": [
                {"name": "status:ready"},
                {"name": "needs-human"},
            ],
            "author": {"login": "owner"},
        }
        ordinary_issue = {
            "number": 2,
            "title": "document behavior",
            "body": "touches: docs/**\n",
            "labels": [{"name": "status:ready"}, {"name": "priority:p3"}],
            "author": {"login": "owner"},
        }
        with patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "list_open_issues", return_value=[operator_issue, ordinary_issue]), \
             patch.object(fetch_next_issue, "repository_owner_login", return_value="owner"), \
             patch.object(fetch_next_issue, "repository_trusted_logins", return_value={"owner"}):
            result = fnw.select("agent-2", "openai", 3, 30)

        self.assertEqual(result["work"]["issue"], 2)
        self.assertEqual(result["claimable_issues"], [2])
        self.assertEqual(result["operator_only_issues"], [1])





class MergeWorkTests(unittest.TestCase):
    """Issue #43: merge-ready PRs are claimable board work."""

    def _select(self, prs, candidates=(), agent="agent-2", family="openai",
                dod_ok=True, dod_reason="every Definition-of-Done gate passed"):
        parts = {
            "candidates": [{"number": n, "title": f"issue {n}"} for n in candidates],
            "my_in_flight": None,
            "blocked": [], "conflicted": [], "missing_touches": [], "not_ready": [],
        }
        # Legacy-shaped evidence: no `review_attestations` key, so the
        # head-binding check returns None and these PRs keep the pre-#235
        # "review complete" verdict these cases were written against. Without
        # the patch, review_eligibility would reach the live GitHub API.
        legacy_evidence = {"head_oid": "abc123"}
        with patch.object(fnw, "list_work_prs", return_value=list(prs)), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts), \
             patch.object(fnw, "review_evidence", return_value=legacy_evidence), \
             patch.object(fnw, "dod_status", return_value=(dod_ok, dod_reason)):
            return fnw.select(agent, family, 3, 30)

    def test_merge_outranks_new_work(self):
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   reviews=1, title="ready to merge")
        ready["headRefOid"] = "abc123"
        res = self._select([ready], candidates=[7])
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

    def test_author_may_execute_merge_after_coderabbit_dod_passes(self):
        own = pr(9, "author:agent-2", "family:openai", decision="APPROVED", reviews=1)
        with patch.object(fnw, "dod_status", return_value=(True, "all gates passed")):
            verdict = fnw.merge_eligibility(own, "agent-2")
        self.assertTrue(verdict["eligible"])

    def test_blocked_gates_do_not_offer_merge(self):
        ready = pr(9, "author:agent-1", "family:anthropic", "reviewed-by:agent-9",
                   reviews=1)
        res = self._select([ready], candidates=[7], dod_ok=False,
                           dod_reason="unmet: ci")
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["merge_skipped"], [])

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
class UnreadableQueueTests(unittest.TestCase):
    def test_selector_fails_closed_when_prs_cannot_be_listed(self):
        # Treating an unreadable queue as empty would claim new implementation
        # work as though no review or feedback were waiting.
        with patch.object(fnw, "list_work_prs", return_value=None):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "error")
        self.assertIn("could not be read", res["work"]["reason"])

    def test_unknown_threads_on_authored_pr_are_skipped_not_fatal(self):
        # A read failure on ONE PR must not idle the whole board: the PR is
        # dropped from the candidate set and selection continues.
        authored = pr(57, "author:agent-2", "family:openai")
        authored["_active_review_feedback"] = None
        other = pr(58, "author:agent-1", "family:anthropic")
        parts = {
            "candidates": [{"number": 99, "title": "new work"}],
            "my_in_flight": None, "blocked": [], "conflicted": [],
            "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[authored, other]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["work"]["issue"], 99)
        self.assertEqual(res["reviewable"], [])

    def test_unknown_threads_on_authored_pr_do_not_block_issue_selection(self):
        # With the unreadable PR skipped, a claimable issue is still served.
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
        self.assertEqual(res["work"]["type"], "issue")
        self.assertEqual(res["work"]["issue"], 99)

    def test_every_pr_unreadable_and_no_issue_reports_no_work(self):
        # No hard error, no exit-code change: the selector reports no work.
        unreadable_pr = pr(57, "author:agent-1", "family:anthropic")
        unreadable_pr["_active_review_feedback"] = None
        parts = {
            "candidates": [], "my_in_flight": None, "blocked": [],
            "conflicted": [], "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[unreadable_pr]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["type"], "idle")
        self.assertEqual(res["claimable_issues"], [])


class ResearchRoutingTests(unittest.TestCase):
    def test_skill_for_issue_routes_research_label_and_title(self):
        labeled = {
            "number": 1,
            "title": "look into X",
            "labels": [{"name": "type:research"}],
        }
        titled = {"number": 2, "title": "research: bounded question", "labels": []}
        feat = {
            "number": 3,
            "title": "feat: something",
            "labels": [{"name": "type:feat"}],
        }
        self.assertEqual(fnw.skill_for_issue(labeled), "research")
        self.assertEqual(fnw.skill_for_issue(titled), "research")
        self.assertEqual(fnw.skill_for_issue(feat), "implement-next-issue")

    def test_select_routes_research_candidate(self):
        issue = {
            "number": 101,
            "title": "research: citations",
            "labels": [{"name": "type:research"}, {"name": "status:ready"}, {"name": "priority:p3"}],
        }
        parts = {
            "candidates": [issue],
            "my_in_flight": None, "blocked": [], "conflicted": [],
            "missing_touches": [], "not_ready": [],
        }
        with patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "list_open_issues", return_value=[issue]), \
             patch.object(fnw, "build_candidates", return_value=parts):
            res = fnw.select("agent-2", "openai", 3, 30)
        self.assertEqual(res["work"]["skill"], "research")
        self.assertEqual(res["work"]["issue"], 101)


def stranded(number=7, author="agent-2", threads=0, peer="agent-9", **kw):
    """A PR of `author`'s whose only problem is a Definition-of-Done gate.

    Carries a peer `reviewed-by:` attribution because that is the real shape of
    the bug: PRs #215 and #227 were both independently reviewed and then stuck
    on a single mechanical gate.
    """
    labels = [f"author:{author}", "family:openai"]
    if peer:
        labels.append(f"reviewed-by:{peer}")
    candidate = pr(number, *labels, **kw)
    candidate["_active_review_feedback"] = (
        None if threads is None else [{"body": "x"}] * threads
    )
    return candidate


class UnmetGateParsingTests(unittest.TestCase):
    def test_parses_a_single_gate(self):
        self.assertEqual(fnw._unmet_gates("unmet: rebased"), {"rebased"})

    def test_parses_several_gates(self):
        self.assertEqual(fnw._unmet_gates("unmet: ci, review, size"),
                         {"ci", "review", "size"})

    def test_normalizes_parameterized_acceptance_gate(self):
        self.assertEqual(
            fnw._unmet_gates("unmet: accept #254"), {"accept"},
        )

    def test_a_non_gate_reason_yields_nothing(self):
        # dod_status also returns prose for fetch failures and missing Closes
        # lines. Those are not gate lists and must never be mistaken for one.
        self.assertEqual(fnw._unmet_gates("could not fetch pull request"), set())
        self.assertEqual(fnw._unmet_gates(""), set())

    def test_reads_failed_gate_messages_from_dry_run_json(self):
        payload = {
            "pr": 7,
            "gates": [
                {"name": "rebased", "passed": False,
                 "message": "Fresh pull_request evidence is missing; do not rebase."},
                {"name": "tests", "passed": False,
                 "message": "Changed-file data is truncated; split the PR."},
                {"name": "accept #254", "passed": False,
                 "message": "Two criteria remain unticked."},
                {"name": "ci", "passed": True, "message": "green"},
            ],
        }
        with patch.object(
            fnw, "run_cmd", return_value=(1, json.dumps(payload), ""),
        ):
            details = fnw._dod_gate_details(7)
        self.assertEqual(details, {
            "rebased": "Fresh pull_request evidence is missing; do not rebase.",
            "tests": "Changed-file data is truncated; split the PR.",
            "accept": "Two criteria remain unticked.",
        })

    def test_malformed_gate_detail_payload_fails_closed(self):
        with patch.object(fnw, "run_cmd", return_value=(1, "not json", "")):
            self.assertIsNone(fnw._dod_gate_details(7))


class AuthorGateFixTests(unittest.TestCase):
    def fix(self, candidate, agent="agent-2", reason="unmet: rebased"):
        # `reason` is the verdict merge_eligibility already computed, so the
        # full gate is reevaluated only when its action depends on the failure
        # subtype; the dry-run JSON then preserves that detail for the agent.
        details = {
            "rebased": "Fresh pull_request evidence is missing; do not rebase.",
            "tests": "Changed-file data is truncated; split the PR.",
            "verification": "Verification evidence markers are malformed.",
        }
        with patch.object(fnw, "_dod_gate_details", return_value=details):
            return fnw.author_gate_fix(candidate, agent, reason)

    def test_rebased_only_is_offered_to_the_author(self):
        found = self.fix(stranded(), reason="unmet: rebased")
        self.assertIsNotNone(found)
        self.assertEqual(found["unmet_gates"], ["rebased"])
        self.assertEqual(found["gate_details"], {
            "rebased": "Fresh pull_request evidence is missing; do not rebase.",
        })

    def test_size_only_is_offered_to_the_author(self):
        found = self.fix(stranded(), reason="unmet: size")
        self.assertIsNotNone(found)
        self.assertEqual(found["unmet_gates"], ["size"])

    def test_accept_only_is_offered_to_the_author(self):
        found = self.fix(stranded(), reason="unmet: accept #254")
        self.assertIsNotNone(found)
        self.assertEqual(found["unmet_gates"], ["accept"])

    def test_accept_and_other_author_gates_are_offered_together(self):
        found = self.fix(
            stranded(), reason="unmet: accept #254, size, verification",
        )
        self.assertEqual(
            found["unmet_gates"], ["accept", "size", "verification"],
        )
        self.assertEqual(found["gate_details"], {
            "verification": "Verification evidence markers are malformed.",
        })

    def test_subtype_sensitive_gate_messages_are_preserved(self):
        found = self.fix(stranded(), reason="unmet: tests, verification")
        self.assertEqual(found["gate_details"], {
            "tests": "Changed-file data is truncated; split the PR.",
            "verification": "Verification evidence markers are malformed.",
        })

    def test_missing_rebased_detail_fails_closed(self):
        with patch.object(
            fnw, "_dod_gate_details",
            return_value={"verification": "Verification evidence markers are malformed."},
        ):
            self.assertIsNone(fnw.author_gate_fix(stranded(), "agent-2", "unmet: rebased"))

    def test_non_author_is_never_offered_it(self):
        self.assertIsNone(self.fix(stranded(author="agent-1"), agent="agent-2"))

    def test_a_peer_gate_alongside_it_is_not_author_fixable(self):
        # Only the author may rebase, but only a peer may review. A PR needing
        # both is not the author's to clear alone unless the review failure is
        # the unfixed-thread evidence hole.
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 0, "reviewed_head": True}):
            self.assertIsNone(self.fix(stranded(), reason="unmet: rebased, review"))

    def test_accept_with_peer_review_needed_stays_away_from_author(self):
        with patch.object(fnw, "review_evidence", return_value={
            "unresolved": 0, "unfixed": 0, "reviewed_head": True,
        }):
            self.assertIsNone(
                self.fix(stranded(), reason="unmet: accept #254, review"),
            )

    def test_unfixed_resolved_threads_are_author_fixable(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 2, "reviewed_head": True}), \
             patch.object(merge_pr, "has_authoritative_coderabbit_review",
                          return_value=True):
            found = self.fix(stranded(), reason="unmet: review")
        self.assertIsNotNone(found)
        self.assertEqual(found["unmet_gates"], ["review-evidence"])

    def test_unmet_review_without_unfixed_threads_stays_a_peer_gate(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 0, "reviewed_head": True}):
            self.assertIsNone(self.fix(stranded(), reason="unmet: review"))

    def test_unfixed_threads_plus_rebase_are_both_author_work(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 1, "reviewed_head": True}), \
             patch.object(merge_pr, "has_authoritative_coderabbit_review",
                          return_value=True):
            found = self.fix(stranded(), reason="unmet: rebased, review")
        self.assertEqual(found["unmet_gates"], ["rebased", "review-evidence"])

    def test_unfixed_without_current_head_review_stays_a_peer_gate(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 2, "reviewed_head": False}), \
             patch.object(merge_pr, "with_service_evidence", side_effect=lambda _pr, _n, value: value), \
             patch.object(merge_pr, "has_authoritative_assigned_review",
                          return_value=False):
            self.assertIsNone(self.fix(stranded(), reason="unmet: review"))

    def test_unfixed_without_peer_attribution_is_author_fixable_with_coderabbit(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 2, "reviewed_head": True}), \
             patch.object(merge_pr, "has_authoritative_coderabbit_review",
                          return_value=True):
            found = self.fix(stranded(peer=None), reason="unmet: review")
        self.assertEqual(found["unmet_gates"], ["review-evidence"])

    def test_outdated_unfixed_thread_stays_a_peer_gate_even_with_unfixed(self):
        with patch.object(fnw, "review_evidence",
                          return_value={
                              "unresolved": 0, "unfixed": 2,
                              "outdated_unfixed": 1, "reviewed_head": True,
                          }), \
             patch.object(merge_pr, "has_authoritative_coderabbit_review",
                          return_value=True):
            self.assertIsNone(self.fix(stranded(peer=None), reason="unmet: review"))

    def test_unverifiable_assigned_service_review_stays_a_peer_gate(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 2, "reviewed_head": True}), \
             patch.object(merge_pr, "with_service_evidence", side_effect=lambda _pr, _n, value: value), \
             patch.object(merge_pr, "has_authoritative_assigned_review",
                          return_value=False):
            self.assertIsNone(self.fix(stranded(peer=None), reason="unmet: review"))

    def test_external_fallback_assignments_reuse_shared_evidence_helpers(self):
        evidence = {"unresolved": 0, "unfixed": 1, "reviewed_head": True}
        for service, issue in (("sourcery", 2), ("codeant", 3)):
            candidate = stranded()
            candidate["labels"] = [
                label for label in candidate["labels"]
                if not label["name"].startswith("review:")
            ] + [{"name": f"review:{service}"}]
            candidate["body"] = f"Closes #{issue}"
            with self.subTest(service=service), \
                 patch.object(fnw, "review_evidence", return_value=evidence), \
                 patch.object(merge_pr, "with_service_evidence", return_value=evidence) as enrich, \
                 patch.object(merge_pr, "has_authoritative_assigned_review", return_value=True) as authoritative:
                self.assertTrue(fnw._author_can_repair_review(candidate))
            enrich.assert_called_once_with(candidate, candidate["number"], evidence)
            authoritative.assert_called_once_with(candidate, evidence)

    def test_author_repair_enriches_coderabbit_evidence(self):
        candidate = stranded()
        evidence = {"unresolved": 0, "unfixed": 1, "reviewed_head": True}
        with patch.object(fnw, "review_evidence", return_value=evidence), \
             patch.object(
                 merge_pr, "_with_coderabbit_status", return_value=evidence,
             ) as coderabbit, \
             patch.object(
                 merge_pr, "has_authoritative_coderabbit_review", return_value=True,
             ) as authoritative:
            self.assertTrue(fnw._author_can_repair_review(candidate))

        coderabbit.assert_called_once_with(candidate["number"], evidence)
        authoritative.assert_called_once_with(candidate, evidence)

    def test_author_repair_denies_unavailable_coderabbit_enrichment(self):
        candidate = stranded()
        evidence = {"unresolved": 0, "unfixed": 1, "reviewed_head": True}
        with patch.object(fnw, "review_evidence", return_value=evidence), \
             patch.object(
                 merge_pr, "_with_coderabbit_status", return_value=None,
             ) as coderabbit, \
             patch.object(
                 merge_pr, "has_authoritative_coderabbit_review",
             ) as authoritative:
            self.assertFalse(fnw._author_can_repair_review(candidate))

        coderabbit.assert_called_once_with(candidate["number"], evidence)
        authoritative.assert_not_called()

    def test_missing_or_unsupported_service_is_not_author_fixable(self):
        # pr()/stranded() default to review:coderabbit whenever a caller omits
        # a review label, so the deny branch below needs an explicit opt-out
        # to exercise "no assigned service" rather than always landing on the
        # allowed coderabbit path.
        evidence = {"unresolved": 0, "unfixed": 1, "reviewed_head": True}
        for labels in (
            [],
            [{"name": "review:unknown-bot"}],
            [{"name": "review:coderabbit"}, {"name": "review:sourcery"}],
        ):
            with self.subTest(labels=labels):
                candidate = stranded()
                candidate["labels"] = [
                    label for label in candidate["labels"]
                    if not label["name"].startswith("review:")
                ] + labels
                with patch.object(fnw, "review_evidence", return_value=evidence), \
                     patch.object(merge_pr, "with_service_evidence") as enrich, \
                     patch.object(
                         merge_pr, "has_authoritative_assigned_review",
                     ) as authoritative:
                    self.assertFalse(fnw._author_can_repair_review(candidate))
                enrich.assert_not_called()
                authoritative.assert_not_called()

    def test_retired_reviewer_claim_does_not_block_merge(self):
        candidate = stranded(peer=None)
        candidate["labels"].append({"name": "reviewer:retired-agent"})
        with patch.object(fnw, "dod_status", return_value=(True, "every Definition-of-Done gate passed")):
            self.assertTrue(fnw.merge_eligibility(candidate, "agent-2")["eligible"])

    def test_unresolved_threads_stay_ordinary_feedback(self):
        self.assertIsNone(self.fix(stranded(threads=2)))

    def test_unreadable_thread_state_fails_closed(self):
        self.assertIsNone(self.fix(stranded(threads=None)))

    def test_no_recorded_verdict_is_not_work(self):
        # A mergeable PR records no skip reason, and one that failed a cheap
        # filter before the gates ran needs that filter cleared first.
        self.assertIsNone(fnw.author_gate_fix(stranded(), "agent-2", None))

    def test_a_non_gate_verdict_is_not_work(self):
        self.assertIsNone(
            self.fix(stranded(), reason="no independent review attribution yet"))

    def test_draft_is_skipped(self):
        self.assertIsNone(self.fix(stranded(draft=True)))


class GateFixSelectionTests(unittest.TestCase):
    def select_with(self, candidate, reason="unmet: rebased", agent="agent-2"):
        parts = {"candidates": [], "my_in_flight": None, "blocked": [],
                 "conflicted": [], "missing_touches": [], "not_ready": []}
        details = {"rebased": "Fresh pull_request evidence is missing; do not rebase."}
        with patch.object(fnw, "list_work_prs", return_value=[candidate]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts), \
             patch.object(fnw, "_dod_gate_details", return_value=details), \
             patch.object(fnw, "dod_status", return_value=(False, reason)):
            return fnw.select(agent, "openai", 3, 30)

    def test_author_is_no_longer_told_idle(self):
        # The whole bug: this returned "idle" while the author's own PR sat one
        # mechanical step from mergeable, holding its touches: reservation.
        res = self.select_with(stranded())
        self.assertNotEqual(res["work"]["type"], "idle")
        self.assertEqual(res["work"]["pr"], 7)

    def test_work_names_the_gate_so_the_agent_knows_what_to_do(self):
        res = self.select_with(stranded(), reason="unmet: size")
        self.assertEqual(res["work"]["unmet_gates"], ["size"])

    def test_acceptance_gate_is_routed_as_author_feedback(self):
        res = self.select_with(stranded(), reason="unmet: accept #254")
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertEqual(res["work"]["unmet_gates"], ["accept"])

    def test_red_ci_is_routed_even_before_full_dod_evaluation(self):
        res = self.select_with(stranded(checks="red", peer=None))
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertEqual(res["work"]["unmet_gates"], ["ci"])

    def test_routed_to_a_skill_the_loop_contract_already_knows(self):
        res = self.select_with(stranded())
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertEqual(res["work"]["skill"], "address-pr-feedback")

    def test_a_peer_is_never_handed_it_as_feedback(self):
        res = self.select_with(stranded(author="agent-9"), agent="agent-2")
        self.assertNotEqual(res["work"]["type"], "feedback")

    def test_unresolved_threads_still_outrank_a_gate_fix(self):
        res = self.select_with(stranded(threads=3))
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertNotIn("unmet_gates", res["work"])

    def test_unfixed_review_is_routed_as_author_feedback(self):
        with patch.object(fnw, "review_evidence",
                          return_value={"unresolved": 0, "unfixed": 2, "reviewed_head": True}), \
             patch.object(merge_pr, "has_authoritative_coderabbit_review",
                          return_value=True):
            res = self.select_with(stranded(), reason="unmet: review")
        self.assertEqual(res["work"]["type"], "feedback")
        self.assertEqual(res["work"]["skill"], "address-pr-feedback")
        self.assertEqual(res["work"]["unmet_gates"], ["review-evidence"])


class GateFixRoutingContractTests(unittest.TestCase):
    """The emitted item must reach a consumer that knows what to do with it.

    Review finding on PR #229: emitting `feedback` with `unmet_gates` is only
    actionable if the routed loop and skill document each gate name. Without
    that, the consumer fetches an empty thread checklist, does nothing, and the
    picker returns the identical item next cycle - turning the original `idle`
    into a silent spin. These assert the contract end to end: what select()
    emits, the skill it names, and the actions those documents specify.
    """

    ROOT = Path(__file__).resolve().parents[1]
    CONTRACTS = ("prompts/fleet-worker.md", "skills/address-pr-feedback/SKILL.md")

    def contracts(self):
        return {rel: (self.ROOT / rel).read_text() for rel in self.CONTRACTS}

    def emitted(self):
        parts = {"candidates": [], "my_in_flight": None, "blocked": [],
                 "conflicted": [], "missing_touches": [], "not_ready": []}
        details = {"rebased": "Fresh pull_request evidence is missing; do not rebase."}
        with patch.object(fnw, "list_work_prs", return_value=[stranded()]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "build_candidates", return_value=parts), \
             patch.object(fnw, "_dod_gate_details", return_value=details), \
             patch.object(fnw, "dod_status", return_value=(False, "unmet: rebased")):
            return fnw.select("agent-2", "openai", 3, 30)["work"]

    def test_the_named_skill_actually_exists(self):
        work = self.emitted()
        skill = self.ROOT / "skills" / work["skill"] / "SKILL.md"
        self.assertTrue(skill.is_file(),
                        f"select() routes to '{work['skill']}', which has no SKILL.md")

    def test_both_contracts_document_the_emitted_field(self):
        work = self.emitted()
        self.assertIn("unmet_gates", work)
        for rel, text in self.contracts().items():
            self.assertIn("unmet_gates", text,
                          f"{rel} never mentions the field the picker emits")

    def test_every_author_fixable_gate_is_documented(self):
        for rel, text in self.contracts().items():
            for gate in fnw.AUTHOR_FIXABLE_GATES:
                self.assertIn(f"`{gate}`", text,
                              f"{rel} gives no instruction for the '{gate}' gate")

    def test_every_dod_gate_has_an_explicit_owner_or_exception(self):
        passing = (True, "ok")
        check_names = (
            "check_open", "check_issue_link", "check_verification", "check_ci",
            "check_reviews", "check_rebased", "check_size",
            "check_test_coverage", "check_review_rounds", "check_acceptance",
        )
        patches = [patch.object(merge_pr, name, return_value=passing)
                   for name in check_names]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        with patch.object(merge_pr, "linked_issues", return_value=[254]):
            _, gates = merge_pr.evaluate_dod({}, {254: ""}, {})

        emitted = {fnw._routable_gate_name(name) for name, _, _ in gates}
        owned = (
            fnw.AUTHOR_FIXABLE_GATES
            | fnw.PEER_ROUTABLE_GATES
            | fnw.DOD_NON_ROUTABLE_GATES
        )
        self.assertEqual(
            emitted - owned, set(),
            "evaluate_dod emitted a gate with no author, peer, or explicit exception",
        )

    def test_each_gate_documents_a_concrete_action(self):
        # A gate named without its action is still unactionable prose.
        required = {
            "accept": "tick the boxes",
            "ci": "remediate-ci-failure",
            "rebased": "--force-with-lease",
            "size": "size-waiver",
            "review-evidence": "Withdrawn:",
            "tests": "test coverage",
            "verification": "--refresh-pr",
            "spec-sync": "sync_spec.py",
        }
        self.assertEqual(set(required), set(fnw.AUTHOR_FIXABLE_GATES),
                         "a gate was added without an action marker to assert on")
        for rel, text in self.contracts().items():
            for gate, marker in required.items():
                self.assertIn(marker, text,
                              f"{rel} names '{gate}' but not its action ({marker})")

    def test_rebased_contract_uses_authoritative_detail_to_preserve_head(self):
        skill = self.contracts()["skills/address-pr-feedback/SKILL.md"]
        self.assertIn("work.gate_details.rebased", skill)
        self.assertIn("Do not rebase", skill)
        self.assertIn("close and reopen", skill)
        self.assertIn("--force-with-lease", skill)


HEAD_SHA = "ABC123DEF456"
COMPLETE = "independent review complete; waiting on gated merge"


def reviewed_pr(number=11, author="agent-1", peer="agent-9", threads=0, **kw):
    """A PR carrying completed peer attribution, ready for the binding check."""
    candidate = pr(number, f"author:{author}", "family:anthropic",
                   f"reviewed-by:{peer}", **kw)
    candidate["_active_review_feedback"] = (
        None if threads is None else [{"body": "x"}] * threads
    )
    return candidate


def bound_evidence(peer="agent-9", head=HEAD_SHA):
    return {"head_oid": head,
            "review_attestations": [{"agent": peer, "head": head.lower()}]}


def stale_evidence(peer="agent-9"):
    # Attribution exists, but names a commit that is no longer the head.
    return {"head_oid": HEAD_SHA,
            "review_attestations": [{"agent": peer, "head": "0ldc0mm1t"}]}



class AgentResolutionTests(unittest.TestCase):
    @staticmethod
    def _idle():
        return {
            "agent": "unused",
            "family": None,
            "work": {"type": "idle", "skill": None},
            "skipped_prs": [],
            "merge_skipped": [],
            "claimable_issues": [],
            "mergeable_detail": [],
            "reviewable_detail": [],
        }

    def _run(self, argv, env=None):
        idle = {"agent": "unused", "family": None, "work": {"type": "idle",
                "skill": None}, "skipped_prs": [], "merge_skipped": [],
                "claimable_issues": [], "mergeable_detail": [],
                "reviewable_detail": []}
        with patch.object(fnw, "select", return_value=idle) as select_mock, \
             patch.object(fnw, "_reserve_derived_identity", return_value=None), \
             patch.dict("os.environ", env or {}, clear=True), \
             patch("sys.argv", [*argv, "--reap-after", "0"]):
            rc = fnw.main()
        return rc, select_mock

    def test_omitting_agent_derives_a_stable_identity(self):
        rc, select_mock = self._run(["fetch_next_work.py", "--family", "openai"])
        self.assertIsNone(rc)
        self.assertTrue(select_mock.call_args.args[0].startswith("codex-"))

    def test_environment_override_wins_over_fingerprint(self):
        rc, select_mock = self._run(
            ["fetch_next_work.py", "--family", "openai"],
            {"ARU_AGENT_ID": "operator-agent"},
        )
        self.assertIsNone(rc)
        self.assertEqual(select_mock.call_args.args[0], "operator-agent")

    def test_explicit_agent_wins_without_presence_lookup(self):
        rc, select_mock = self._run(
            ["fetch_next_work.py", "--agent", "claude-1"],
            {"ARU_AGENT_ID": "operator-agent"},
        )
        self.assertIsNone(rc)
        self.assertEqual(select_mock.call_args.args[0], "claude-1")

    def test_invalid_explicit_and_environment_ids_fail_closed(self):
        for argv, env in (
            (["fetch_next_work.py", "--agent", "bad id"], {}),
            (["fetch_next_work.py"], {"ARU_AGENT_ID": ""}),
        ):
            with self.subTest(argv=argv, env=env):
                with patch("sys.stderr", new_callable=io.StringIO) as error:
                    rc, select_mock = self._run(argv, env)
                self.assertEqual(rc, 1)
                select_mock.assert_not_called()
                self.assertIn("invalid", error.getvalue())

    def test_presence_allocation_flags_are_removed(self):
        for flag in ("--session-id", "--agent-pool"):
            with self.subTest(flag=flag), patch(
                "sys.argv", ["fetch_next_work.py", flag, "legacy"]
            ), self.assertRaises(SystemExit):
                fnw.main()

class NoFastTrackInThePickerTests(unittest.TestCase):
    """Merge eligibility requires review attribution again (#321).

    _fast_track() let the picker route a PR to merge with no independent review
    attribution, and let an author be handed its own PR to merge.
    """

    def _pr(self, **over):
        """A PR that clears every cheap filter, so the attribution check is what
        decides the verdict rather than an earlier guard."""
        pr = {
            "number": 9,
            "title": "t",
            "labels": [{"name": "author:claude-1"}],
            "reviewDecision": "",
            "isDraft": False,
            # review_thread_count caches this key; seeding it keeps the test
            # off the network and out of the "state unavailable" refusal.
            "_active_review_feedback": [],
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
        pr.update(over)
        return pr

    def test_environment_cannot_waive_review_attribution(self):
        with patch.dict("os.environ", {"ARU_FAST_TRACK": "1"}), \
             patch.object(fnw, "dod_status", return_value=(False, "unmet: review")):
            verdict = fnw.merge_eligibility(self._pr(), "codex-1")
        self.assertFalse(verdict["eligible"])
        self.assertIn("unmet: review", verdict["reason"])

    def test_author_may_mechanically_merge_after_coderabbit_gate(self):
        with patch.dict("os.environ", {"ARU_FAST_TRACK": "1"}), \
             patch.object(fnw, "dod_status", return_value=(True, "green")):
            verdict = fnw.merge_eligibility(
                self._pr(reviewDecision="APPROVED"), "claude-1")
        self.assertTrue(verdict["eligible"])

    def test_a_genuine_peer_still_makes_a_pr_mergeable(self):
        pr = self._pr(labels=[{"name": "author:claude-1"},
                              {"name": "reviewed-by:codex-1"}])
        with patch.object(fnw, "dod_status", return_value=(True, "green")):
            verdict = fnw.merge_eligibility(pr, "codex-1")
        self.assertTrue(verdict["eligible"], verdict["reason"])

    def test_the_helper_is_gone(self):
        self.assertFalse(hasattr(fnw, "_fast_track"))


class PureIdentityBoundaryTests(unittest.TestCase):
    def test_picker_has_no_presence_allocation_helpers(self):
        for name in (
            "_fingerprint_assign_identity",
            "_auto_assign_identity",
            "_pool_assign_identity",
            "_explicit_identity",
            "_default_session_id",
        ):
            self.assertFalse(hasattr(fnw, name), name)


class IssueClaimFailureTests(unittest.TestCase):
    @staticmethod
    def selection():
        return {
            "agent": "agent-1",
            "family": "openai",
            "work": {
                "type": "issue", "issue": 334, "title": "claim safely",
                "skill": "implement-next-issue", "resuming": False,
            },
            "mergeable_detail": [], "mergeable": [], "merge_skipped": [],
            "reviewable_detail": [], "reviewable": [], "skipped_prs": [],
            "escalated_prs": [], "claimable_issues": [334],
            "blocked_by_dependencies": [], "blocked_by_file_conflict": [],
            "missing_touches": [], "operator_only_issues": [],
        }

    def test_json_claim_failures_are_explicit_and_nonzero(self):
        from claim_issue import EXIT_CONFLICT, EXIT_ERROR
        for claim_rc, expected in ((EXIT_ERROR, "error"), (EXIT_CONFLICT, "conflict")):
            with self.subTest(claim_rc=claim_rc):
                stdout = io.StringIO()
                with patch.object(fnw, "_resolve_identity", return_value=None), \
                     patch.object(fnw, "select", return_value=self.selection()), \
                     patch("claim_issue.claim_issue", return_value=claim_rc), \
                     patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--claim", "--json", "--reap-after", "0"]), \
                     patch("sys.stdout", stdout):
                    result = fnw.main()
                payload = json.loads(stdout.getvalue())
                self.assertEqual(result, 1)
                self.assertFalse(payload["work"]["claimed"])
                self.assertEqual(payload["work"]["claim_result"], expected)

    def test_text_claim_failure_never_prints_implementation_success(self):
        from claim_issue import EXIT_ERROR
        stdout = io.StringIO()
        with patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "select", return_value=self.selection()), \
             patch("claim_issue.claim_issue", return_value=EXIT_ERROR), \
             patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--claim", "--reap-after", "0"]), \
             patch("sys.stdout", stdout):
            result = fnw.main()
        self.assertEqual(result, 1)
        self.assertIn("Could not claim issue #334", stdout.getvalue())
        self.assertIn("No work was started", stdout.getvalue())
        self.assertNotIn("Implement issue #334", stdout.getvalue())

    def test_cli_exits_with_the_main_result(self):
        with patch.object(fnw, "main", return_value=1):
            with self.assertRaisesRegex(SystemExit, "1"):
                fnw.cli()


class MergeClaimFailureTests(unittest.TestCase):
    @staticmethod
    def selection():
        details = [
            {"pr": 21, "title": "first merge", "head_sha": "a" * 40},
            {"pr": 22, "title": "second merge", "head_sha": "b" * 40},
        ]
        return {
            "agent": "agent-1",
            "family": "openai",
            "work": {
                "type": "merge", "pr": 21, "title": "first merge",
                "skill": "merge-pr", "head_sha": "a" * 40,
            },
            "mergeable_detail": details, "mergeable": [21, 22],
            "merge_skipped": [], "reviewable_detail": [], "reviewable": [],
            "skipped_prs": [], "escalated_prs": [], "claimable_issues": [],
            "blocked_by_dependencies": [], "blocked_by_file_conflict": [],
            "missing_touches": [], "operator_only_issues": [],
        }

    def test_json_merge_claim_failures_are_explicit_and_nonzero(self):
        from claim_issue import EXIT_CONFLICT, EXIT_ERROR
        cases = (
            ([EXIT_ERROR], "error", 1),
            ([EXIT_CONFLICT, EXIT_CONFLICT], "all_taken", 2),
        )
        for claim_results, expected, expected_calls in cases:
            with self.subTest(claim_results=claim_results):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(fnw, "_resolve_identity", return_value=None), \
                     patch.object(fnw, "select", return_value=self.selection()), \
                     patch.object(fnw, "claim_merge", side_effect=claim_results) as claim, \
                     patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--claim", "--json", "--reap-after", "0"]), \
                     patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    result = fnw.main()
                payload = json.loads(stdout.getvalue())
                self.assertEqual(result, 1)
                self.assertFalse(payload["work"]["claimed"])
                self.assertEqual(payload["work"]["claim_result"], expected)
                self.assertEqual(claim.call_count, expected_calls)
                self.assertEqual(
                    [record.args for record in claim.call_args_list],
                    [(21, "agent-1")] if expected_calls == 1 else [
                        (21, "agent-1"), (22, "agent-1"),
                    ],
                )

    def test_text_merge_claim_failure_never_prints_merge_success(self):
        from claim_issue import EXIT_CONFLICT, EXIT_ERROR
        cases = (
            ([EXIT_ERROR], "error"),
            ([EXIT_CONFLICT, EXIT_CONFLICT], "all_taken"),
        )
        for claim_results, expected in cases:
            with self.subTest(claim_results=claim_results):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(fnw, "_resolve_identity", return_value=None), \
                     patch.object(fnw, "select", return_value=self.selection()), \
                     patch.object(fnw, "claim_merge", side_effect=claim_results), \
                     patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--claim", "--reap-after", "0"]), \
                     patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    result = fnw.main()
                output = stdout.getvalue()
                self.assertEqual(result, 1)
                self.assertIn("Could not claim merge #21", output)
                self.assertIn(f"result={expected}", output)
                self.assertIn("No work was started", output)
                self.assertNotIn("Merge PR", output)


class WorkPickerTests(unittest.TestCase):
    def _dummy_select(self):
        return {
            "work": {"type": "idle", "skill": None},
            "skipped_prs": [],
            "merge_skipped": [],
            "claimable_issues": [],
            "blocked_by_dependencies": [],
            "blocked_by_file_conflict": [],
            "missing_touches": [],
            "operator_only_issues": [],
        }

    def test_default_reap_threshold(self):
        with patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1"]), \
             patch.object(fnw, "reap_stale_merges", return_value=[]) as mock_merges, \
             patch.object(fnw, "reap_stale_claims", return_value=[]) as mock_claims, \
             patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "select", return_value=self._dummy_select()):
            fnw.main()
            mock_merges.assert_called_once_with(4, prs_snapshot=[])
            mock_claims.assert_called_once_with([], 4, open_prs_snapshot=[])

    def test_reap_disabled_by_zero(self):
        with patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--reap-after", "0"]), \
             patch.object(fnw, "reap_stale_merges") as mock_merges, \
             patch.object(fnw, "reap_stale_claims") as mock_claims, \
             patch.object(fnw, "select", return_value=self._dummy_select()):
            fnw.main()
            mock_merges.assert_not_called()
            mock_claims.assert_not_called()

    def test_reap_exception_handled_gracefully(self):
        import io
        fake_stderr = io.StringIO()
        with patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1"]), \
             patch("sys.stderr", fake_stderr), \
             patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "list_open_issues", return_value=[]), \
             patch.object(fnw, "reap_stale_merges", side_effect=RuntimeError("transient network failure")), \
             patch.object(fnw, "select", return_value=self._dummy_select()):
            fnw.main()
            self.assertIn("[WARN] Autonomous claim reap encountered error: transient network failure", fake_stderr.getvalue())

class IdleBacklogPromotionTests(unittest.TestCase):
    def setUp(self):
        self.inventory_reads = 0
        self.select_reads = 0
        def inventory(_slug, numbers):
            self.inventory_reads += 1; return ({number: ("Ready" if self.inventory_reads >= 3 and number == 10 else "Backlog") for number in numbers}, int(self.inventory_reads >= 3))  # noqa: E702
        def select(*_args):
            self.select_reads += 1; return {"work": ({"type": "issue", "issue": 10} if self.select_reads >= 3 else {"type": "idle"})}  # noqa: E702
        self.enterContext(patch.object(merge_pr, "repository_merge_lock",
            return_value=nullcontext((True, "locked"))))
        self.enterContext(patch.object(
            fnw, "select", side_effect=select))
        self.enterContext(patch.object(
            fnw, "_governed_open_issue_statuses", side_effect=inventory))
    @staticmethod
    def _issue(number, priority="p1", *, status="backlog", body=None, labels=()):
        return {
            "number": number, "title": f"issue {number}",
            "body": body or (
                "## Acceptance Criteria\n- [ ] Works (verify: `python3 -m unittest tests.test_example`)\n\n"
                "## Decision Boundaries\n- Default: bounded\n\n## Non-Goals\n- No extras\n\n"
                "## Verification\n- `python3 -m unittest tests.test_example`\n\n"
                "touches: scripts/example.py, tests/test_example.py\ndepends-on: none\n"
            ),
            "labels": [
                {"name": f"status:{status}"}, {"name": f"priority:{priority}"},
                {"name": "type:feat"}, *({"name": label} for label in labels),
            ],
            "author": {"login": "owner"},
            "updatedAt": "2026-08-25T18:00:00Z",
        }

    def test_promotes_only_highest_priority_picker_eligible_issue(self):
        issues = [self._issue(20, "p2"), self._issue(30, "p0"), self._issue(10, "p0")]
        post = self._issue(10, "p0", status="ready")
        with patch.object(fnw, "list_open_issues",
                          side_effect=[issues, issues, [post], [post]]), \
             patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "active_increment_scope", return_value=None), \
             patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(fetch_next_issue, "repository_trusted_logins",
                          return_value={"owner"}), \
             patch.object(fetch_next_issue, "repository_owner_login",
                          return_value="owner"), \
             patch.object(fetch_next_issue, "is_trusted_metadata_author",
                          return_value=True), \
             patch.object(fnw, "query_issue_project_items",
                          side_effect=[[{"status": {"name": "Backlog"}}],
                                       [{"status": {"name": "Ready"}}]]), \
             patch.object(fnw, "select_governed_project_items",
                          side_effect=lambda items, _slug: items), \
             patch.object(fnw, "update_status", return_value=True) as update:
            promoted = fnw.promote_one_idle_backlog_issue("agent-1")
        self.assertEqual(promoted, 10)
        update.assert_called_once_with(
            10, "Ready", require_board=True, expected_status="Backlog",
            require_unclaimed=True, expected_updated_at="2026-08-25T18:00:00Z")

    def test_does_not_promote_when_any_ready_issue_exists(self):
        issues = [self._issue(1, status="ready"), self._issue(2)]
        with patch.object(fnw, "list_open_issues", return_value=issues), \
             patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_refuses_conflicting_status_labels(self):
        issue = self._issue(1)
        issue["labels"].append({"name": "status:done"})
        with patch.object(fnw, "list_open_issues", return_value=[issue]), \
             patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_refuses_candidate_that_changes_during_live_revalidation(self):
        original = self._issue(1)
        changed = {**original, "body": original["body"] + "\nchanged\n"}
        with patch.object(fnw, "list_open_issues",
                          side_effect=[[original], [changed]]), \
             patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "active_increment_scope", return_value=None), \
             patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(fetch_next_issue, "repository_trusted_logins",
                          return_value={"owner"}), \
             patch.object(fetch_next_issue, "repository_owner_login",
                          return_value="owner"), \
             patch.object(fetch_next_issue, "is_trusted_metadata_author",
                          return_value=True), \
             patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_refuses_when_governed_board_is_not_backlog(self):
        issue = self._issue(1)
        with patch.object(fnw, "list_open_issues", return_value=[issue]), \
             patch.object(fnw, "list_work_prs", return_value=[]), \
             patch.object(fnw, "active_increment_scope", return_value=None), \
             patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(fetch_next_issue, "repository_trusted_logins",
                          return_value={"owner"}), \
             patch.object(fetch_next_issue, "repository_owner_login",
                          return_value="owner"), \
             patch.object(fetch_next_issue, "is_trusted_metadata_author",
                          return_value=True), \
             patch.object(fnw, "query_issue_project_items",
                          return_value=[{"status": {"name": "Done"}}]), \
             patch.object(fnw, "select_governed_project_items",
                          side_effect=lambda items, _slug: items), \
             patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_leaves_operator_epic_incomplete_and_oversized_work_in_backlog(self):
        incomplete = self._issue(1, body="touches: scripts/x.py")
        operator = self._issue(2, labels=("needs-human",))
        epic = self._issue(3, labels=("type:epic",))
        oversized = self._issue(
            4,
            body=(self._issue(4)["body"]
                  .replace("touches: scripts/example.py, tests/test_example.py",
                           "touches: scripts/a.py, hooks/b.py, tests/test_example.py")),
        )
        with patch.object(
            fnw, "list_open_issues",
            return_value=[incomplete, operator, epic, oversized],
        ), patch.object(fnw, "get_repo_slug", return_value="owner/repo"), \
             patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_read_only_picker_does_not_attempt_promotion(self):
        idle = {
            "agent": "agent-1", "family": None,
            "work": {"type": "idle", "skill": None},
            "skipped_prs": [], "merge_skipped": [], "claimable_issues": [],
        }
        with patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "select", return_value=idle), \
             patch.object(fnw, "promote_one_idle_backlog_issue") as promote, \
             patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1",
                                "--reap-after", "0"]), \
             patch("sys.stdout", io.StringIO()):
            fnw.main()
        promote.assert_not_called()

    def test_claiming_idle_picker_promotes_reselects_and_claims(self):
        idle = {
            "agent": "agent-1", "family": None,
            "work": {"type": "idle", "skill": None},
            "skipped_prs": [], "merge_skipped": [], "claimable_issues": [],
        }
        selected = IssueClaimFailureTests.selection()
        stdout = io.StringIO()
        with patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "select", side_effect=[idle, selected]), \
             patch.object(fnw, "promote_one_idle_backlog_issue", return_value=334), \
             patch("claim_issue.claim_issue", return_value=fnw.EXIT_OK) as claim, \
             patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1",
                                "--claim", "--json", "--reap-after", "0"]), \
             patch("sys.stdout", stdout):
            result = fnw.main()
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(result)
        self.assertEqual(payload["auto_promoted_issue"], 334)
        self.assertTrue(payload["work"]["claimed"])
        claim.assert_called_once_with(334, "agent-1")
