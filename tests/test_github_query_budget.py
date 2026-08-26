"""Regression tests for bounded GitHub reads in one factory picker cycle."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_next_issue  # noqa: E402
import fetch_next_work as fnw  # noqa: E402


def rest_pr(number):
    return {
        "number": number,
        "title": f"PR {number}",
        "draft": False,
        "labels": [{"name": "author:agent-1"}],
        "updated_at": "2026-08-25T12:00:00Z",
        "created_at": "2026-08-25T11:00:00Z",
        "head": {"ref": f"fix/issue-{number}-x", "sha": f"sha-{number}"},
        "body": f"Closes #{number}",
    }


class RestFallbackTests(unittest.TestCase):
    def test_unavailable_open_issue_inventory_is_not_treated_as_empty(self):
        result = fnw.select(
            "agent-1", "openai",
            prs_snapshot=[], issues_snapshot=None,
        )

        self.assertEqual(result["work"]["type"], "error")
        self.assertIn("open issue queue could not be read", result["work"]["reason"])

    def test_failed_project_metadata_snapshot_is_reused_without_retry(self):
        inventory = patch.object(fnw, "_governed_open_issue_statuses", return_value=None)
        projects = patch.object(fnw, "get_repo_projects")
        with inventory as board, projects as project_lookup:
            with self.assertRaises(fnw.AutoTriageError):
                fnw._idle_backlog_candidate(
                    "agent-1",
                    issues_snapshot=[{"number": 7}],
                    prs_snapshot=[],
                    repo_slug_snapshot="owner/repo",
                    projects_snapshot=None,
                )

        board.assert_called_once_with("owner/repo", {7}, projects=None)
        project_lookup.assert_not_called()

    def test_unassigned_gate_fix_skips_review_evidence_query(self):
        pr = {"number": 9, "labels": [{"name": "review:unknown"}]}
        with patch.object(fnw, "review_evidence") as evidence:
            self.assertFalse(fnw._author_can_repair_review(pr))
        evidence.assert_not_called()

    def test_rich_pr_snapshot_does_not_repeat_the_file_query(self):
        rich = [{
            "number": 7, "title": "PR 7", "isDraft": False, "labels": [],
            "reviews": [], "statusCheckRollup": [], "updatedAt": "now",
            "createdAt": "now", "headRefName": "fix/7", "headRefOid": "abc",
            "body": "Closes #7", "reviewDecision": "", "state": "OPEN",
            "mergedAt": None, "files": [{"path": "a.py"}], "changedFiles": 1,
        }]
        with patch.object(
            fnw, "run_cmd", return_value=(0, json.dumps(rich), ""),
        ) as run:
            self.assertEqual(fnw.list_open_prs(), rich)

        run.assert_called_once()

    def test_failed_graphql_list_uses_two_bounded_calls_total(self):
        output = "\n".join(json.dumps(rest_pr(number)) for number in (7, 8))
        with patch.object(fnw, "get_repo_slug", return_value="acme/widgets"), \
             patch.object(fnw, "run_cmd", side_effect=[
                 (1, "", "GraphQL rate limit exhausted"),
                 (0, output, ""),
             ]) as run:
            prs = fnw.list_open_prs()

        self.assertEqual([item["number"] for item in prs], [7, 8])
        self.assertTrue(all(item["_degraded_rest_snapshot"] for item in prs))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][:3], ["gh", "api", "--paginate"])

    def test_degraded_snapshot_stops_before_per_pr_queries(self):
        candidate = {
            "number": 7,
            "title": "PR 7",
            "labels": [],
            "_degraded_rest_snapshot": True,
        }
        with patch.object(fnw, "review_thread_count") as threads, \
             patch.object(fnw, "dod_status") as dod, \
             patch.object(fnw, "list_open_issues") as issues:
            result = fnw.select(
                "agent-1", "openai",
                prs_snapshot=[candidate], issues_snapshot=[],
            )

        self.assertEqual(result["work"]["type"], "error")
        self.assertEqual(result["degraded_rest_prs"], [7])
        threads.assert_not_called()
        dod.assert_not_called()
        issues.assert_not_called()

    def test_empty_rest_inventory_still_fails_closed_without_more_queue_reads(self):
        snapshot = fnw.DegradedPrSnapshot()
        with patch.object(fnw, "list_merged_needing_closeout") as merged, \
             patch.object(fnw, "list_open_issues") as issues:
            with patch.object(fnw, "list_open_prs", return_value=snapshot):
                self.assertIs(fnw.list_work_prs(), snapshot)
            result = fnw.select(
                "agent-1", "openai",
                prs_snapshot=snapshot, issues_snapshot=[],
            )

        self.assertEqual(result["work"]["type"], "error")
        self.assertEqual(result["degraded_rest_prs"], [])
        merged.assert_not_called()
        issues.assert_not_called()


class PerCandidateBudgetTests(unittest.TestCase):
    @staticmethod
    def pr(checks):
        rollups = {
            "green": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "red": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
            "pending": [{"status": "IN_PROGRESS"}],
            "none": [],
        }
        return {
            "number": 9,
            "title": "not merge-ready",
            "labels": [],
            "isDraft": False,
            "statusCheckRollup": rollups[checks],
        }

    def test_non_green_ci_never_queries_review_threads_or_dod(self):
        for state in ("red", "pending", "none"):
            with self.subTest(state=state), \
                 patch.object(fnw, "review_thread_count") as threads, \
                 patch.object(fnw, "dod_status") as dod:
                verdict = fnw.merge_eligibility(self.pr(state), "agent-1")
            self.assertFalse(verdict["eligible"])
            threads.assert_not_called()
            dod.assert_not_called()

    def test_picker_bounds_full_dod_evaluations_per_cycle(self):
        candidates = [dict(self.pr("green"), number=n) for n in range(1, 11)]
        with patch.object(fnw, "dod_status", return_value=(False, "unmet: review")) as dod:
            result = fnw.select("agent-1", "openai", prs_snapshot=candidates,
                                issues_snapshot=[])
        self.assertEqual(result["work"]["type"], "idle")
        self.assertEqual(dod.call_count, fnw.DOD_CANDIDATE_LIMIT)


class PullRequestFileBudgetTests(unittest.TestCase):
    @staticmethod
    def page(*nodes):
        return {
            "nodes": list(nodes),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }

    @staticmethod
    def node(number, change_type):
        return {
            "number": number,
            "body": f"Closes #{number}",
            "headRefName": f"fix/issue-{number}-x",
            "changedFiles": 1,
            "files": {"nodes": [{"path": f"file-{number}.py", "changeType": change_type}]},
        }

    def test_ordinary_prs_do_not_trigger_rest_file_calls(self):
        nodes = [self.node(number, "MODIFIED") for number in range(1, 101)]
        with patch.object(fetch_next_issue, "get_repo_slug", return_value="acme/widgets"), \
             patch.object(
                 fetch_next_issue, "_pr_files_page",
                 return_value=self.page(*nodes),
             ), patch.object(fetch_next_issue, "_rest_pr_files") as rest:
            records = fetch_next_issue.load_open_pr_file_records()

        self.assertEqual(len(records), 100)
        rest.assert_not_called()

    def test_only_renamed_pr_fetches_rest_file_details(self):
        renamed = [{
            "path": "new.py", "previous_filename": "old.py", "changeType": "RENAMED",
        }]
        with patch.object(fetch_next_issue, "get_repo_slug", return_value="acme/widgets"), \
             patch.object(
                 fetch_next_issue, "_pr_files_page",
                 return_value=self.page(self.node(1, "MODIFIED"), self.node(2, "RENAMED")),
             ), patch.object(
                 fetch_next_issue, "_rest_pr_files", return_value=renamed
             ) as rest:
            records = fetch_next_issue.load_open_pr_file_records()

        rest.assert_called_once_with("acme", "widgets", 2)
        self.assertEqual(records[1]["files"], renamed)


class OpenPrQueryCapTests(unittest.TestCase):
    """``gh pr list`` cannot page, so a result at the cap proves nothing (#463)."""

    @staticmethod
    def capped():
        return [{
            "number": number, "title": f"PR {number}", "isDraft": False, "labels": [],
            "reviews": [], "statusCheckRollup": [], "updatedAt": "now", "createdAt": "now",
            "headRefName": f"fix/{number}", "headRefOid": f"sha-{number}", "body": "",
            "reviewDecision": "", "state": "OPEN", "mergedAt": None,
            "files": [], "changedFiles": 0,
        } for number in range(1, fnw.OPEN_PR_QUERY_LIMIT + 1)]

    def test_result_at_the_cap_is_unreadable_rather_than_a_complete_inventory(self):
        with patch.object(fnw, "run_cmd", return_value=(0, json.dumps(self.capped()), "")) as run, \
             patch.object(fnw, "_rest_open_prs") as rest, \
             patch("sys.stderr", io.StringIO()) as err:
            self.assertIsNone(fnw.list_open_prs())

        run.assert_called_once()
        rest.assert_not_called()
        self.assertIn("truncated", err.getvalue())

    def test_one_pr_below_the_cap_is_still_a_usable_inventory(self):
        usable = self.capped()[:-1]
        with patch.object(fnw, "run_cmd", return_value=(0, json.dumps(usable), "")):
            self.assertEqual(fnw.list_open_prs(), usable)

    def test_capped_inventory_never_reaches_close_out_recovery_or_selection(self):
        with patch.object(fnw, "run_cmd", return_value=(0, json.dumps(self.capped()), "")), \
             patch.object(fnw, "list_merged_needing_closeout") as recovery, \
             patch("sys.stderr", io.StringIO()):
            self.assertIsNone(fnw.list_work_prs())

        recovery.assert_not_called()
        result = fnw.select("agent-1", "openai", prs_snapshot=None)
        self.assertEqual(result["work"]["type"], "error")
        self.assertIn("could not be read", result["work"]["reason"])

    def test_capped_inventory_stops_the_cycle_before_any_claim_is_reaped(self):
        with patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--json"]), \
             patch("sys.stdout", io.StringIO()) as out, patch("sys.stderr", io.StringIO()), \
             patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "run_cmd", return_value=(0, json.dumps(self.capped()), "")), \
             patch.object(fnw, "list_open_issues") as issues, \
             patch.object(fnw, "reap_stale_merges") as merges, \
             patch.object(fnw, "reap_stale_claims") as claims:
            self.assertEqual(fnw.main(), 1)

        merges.assert_not_called()
        claims.assert_not_called()
        issues.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["work"]["type"], "error")

    def test_close_out_recovery_prs_do_not_fake_a_truncated_open_queue(self):
        merged = [{"number": 900 + n, "title": "done", "labels": [], "state": "MERGED",
                   "mergedAt": "now", "body": ""} for n in range(20)]
        snapshot = self.capped()[:-1] + merged
        with patch.object(fnw, "needs_my_attention", return_value=False), \
             patch.object(fnw, "merge_eligibility",
                          return_value={"eligible": False, "reason": "unmet: ci"}), \
             patch.object(fnw, "author_gate_fix", return_value=None):
            result = fnw.select("agent-1", "openai", prs_snapshot=snapshot,
                                issues_snapshot=None)

        self.assertEqual(result["work"]["type"], "error")
        self.assertIn("open issue queue could not be read", result["work"]["reason"])


class CycleSnapshotTests(unittest.TestCase):
    @staticmethod
    def idle():
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

    def test_reapers_and_selector_share_one_cycle_snapshot(self):
        prs = [{"number": 1, "state": "OPEN", "labels": []}]
        issues = [{"number": 2, "labels": []}]
        with patch("sys.argv", ["fetch_next_work.py", "--agent", "agent-1", "--json"]), \
             patch("sys.stdout", io.StringIO()), \
             patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "list_work_prs", return_value=prs) as list_prs, \
             patch.object(fnw, "list_open_issues", return_value=issues) as list_issues, \
             patch.object(fnw, "reap_stale_merges", return_value=[]) as merges, \
             patch.object(fnw, "reap_stale_claims", return_value=[]) as claims, \
             patch.object(fnw, "select", return_value=self.idle()) as select:
            fnw.main()

        list_prs.assert_called_once_with()
        list_issues.assert_called_once_with()
        merges.assert_called_once_with(4, prs_snapshot=prs)
        claims.assert_called_once_with(issues, 4, open_prs_snapshot=prs)
        self.assertIs(select.call_args.kwargs["prs_snapshot"], prs)
        self.assertIs(select.call_args.kwargs["issues_snapshot"], issues)

    def test_post_promotion_selector_reuses_authoritative_readback_snapshots(self):
        idle = self.idle()
        selected = {**idle, "work": {"type": "issue", "issue": 7}}
        post = {
            "prs": [{"number": 1}], "issues": [{"number": 7}],
            "_selection": selected,
        }

        def promote(*_args, **kwargs):
            kwargs["post_snapshot_out"].update(post)
            return 7

        with patch("sys.argv", [
            "fetch_next_work.py", "--agent", "agent-1", "--claim", "--json",
            "--reap-after", "0",
        ]), patch("sys.stdout", io.StringIO()), \
             patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "select", return_value=idle) as select, \
             patch.object(fnw, "promote_one_idle_backlog_issue", side_effect=promote), \
             patch("claim_issue.claim_issue", return_value=fnw.EXIT_OK):
            fnw.main()

        select.assert_called_once()


class LoopOrchestrationBudgetTests(unittest.TestCase):
    """The desktop loop must not multiply bounded helper reads across a tick."""

    @classmethod
    def setUpClass(cls):
        cls.skill = (ROOT / "skills/run-aru-factory/SKILL.md").read_text()
        cls.prompt = (ROOT / "prompts/fleet-worker.md").read_text()

    def test_tick_starts_with_one_github_bearing_entrypoint(self):
        start = self.prompt.split("### The loop", 1)[1].split("#### A.", 1)[0]
        commands = [
            line.strip() for line in start.splitlines()
            if line.strip().startswith("python3 ")
        ]
        self.assertEqual(len(commands), 1)
        self.assertIn("fetch_next_work.py", commands[0])
        self.assertIn("--claim --json", commands[0])
        contract = " ".join(start.split()).lower()
        self.assertIn("only routine github-bearing entrypoint", contract)
        for redundant in (
            "fleet_status.py", "triage_backlog.py", "gh issue", "gh pr",
            "check_ci.py", "merge_pr.py --dry-run",
        ):
            self.assertIn(redundant, contract)

    def test_waiting_card_uses_picker_snapshot_without_enrichment(self):
        waiting = self.prompt.split(
            "### Waiting, continuity, intentional stop, and Slack alerts", 1
        )[1].split("**Slack control-room alerts", 1)[0]
        lowered = " ".join(waiting.split()).lower()
        self.assertIn("assemble only from the current picker result", lowered)
        self.assertIn("zero follow-up github reads", lowered)
        self.assertNotIn("triage_backlog.py", waiting)

    def test_merge_path_has_no_probe_or_confirmation_read(self):
        merge = self.prompt.split("#### B.", 1)[1].split("#### C.", 1)[0]
        self.assertNotIn("--dry-run", merge)
        confirmations = [
            line for line in merge.splitlines()
            if "claim_issue.py" in line and "--release" not in line
        ]
        self.assertEqual(confirmations, [])
        self.assertEqual(merge.count("merge_pr.py"), 1)
        self.assertIn("exactly one fresh picker call", merge)

    def test_router_carries_the_same_tick_budget(self):
        loop = self.skill.split("### loop", 1)[1].split("### doctor", 1)[0]
        lowered = " ".join(loop.split()).lower()
        self.assertIn("exactly one authoritative picker call", lowered)
        self.assertIn("zero follow-up github reads", lowered)
        self.assertIn("build the **status card** only from the picker result", lowered)


if __name__ == "__main__":
    unittest.main()
