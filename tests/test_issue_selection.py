import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue  # noqa: E402
from fetch_next_issue import build_candidates, parse_dependencies  # noqa: E402


def issue(number, body="", labels=()):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
    }


class IssueSelectionTests(unittest.TestCase):
    def test_dependency_parser_ignores_prose(self):
        body = """A `depends-on:` field is required.

depends-on: #2, #4
"""
        self.assertEqual(parse_dependencies(body), [2, 4])

    def test_claimed_and_overlapping_work_are_not_candidates(self):
        issues = [
            issue(
                1,
                "touches: src/**\n",
                labels=("agent:agent-a", "status:in-progress"),
            ),
            issue(2, "touches: src/models.py\n", labels=("status:ready",)),
            issue(3, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-b")

        self.assertEqual([item["number"] for item in result["candidates"]], [3])
        self.assertEqual(result["conflicted"][0]["number"], 2)

    def test_in_review_issue_contributes_touches_and_is_not_candidate(self):
        issues = [
            issue(
                20,
                "touches: src/auth.py\n",
                labels=("status:in-review",),
            ),
            issue(21, "touches: src/auth.py\n", labels=("status:ready",)),
            issue(22, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        # Issue 20 (in-review) is not a candidate, and with no PR-file map the
        # declared touches still block candidate 21 (fail closed).
        self.assertEqual([item["number"] for item in result["candidates"]], [22])
        self.assertEqual(result["conflicted"][0]["number"], 21)

    def test_in_review_pr_files_not_declared_path_do_not_block(self):
        issues = [
            issue(
                20,
                "touches: src/auth.py, src/unused.py\n",
                labels=("status:in-review",),
            ),
            issue(21, "touches: src/unused.py\n", labels=("status:ready",)),
            issue(22, "touches: src/auth.py\n", labels=("status:ready",)),
        ]

        result = build_candidates(
            issues, "agent-a", pr_files_by_issue={20: ["src/auth.py"]},
        )

        self.assertEqual([item["number"] for item in result["candidates"]], [21])
        self.assertEqual(result["conflicted"][0]["number"], 22)

    def test_in_progress_declared_touches_ignore_pr_file_map(self):
        issues = [
            issue(
                20,
                "touches: src/auth.py, src/unused.py\n",
                labels=("agent:agent-a", "status:in-progress"),
            ),
            issue(21, "touches: src/unused.py\n", labels=("status:ready",)),
        ]

        result = build_candidates(
            issues, "agent-b", pr_files_by_issue={20: ["src/auth.py"]},
        )

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["conflicted"][0]["number"], 21)

    def test_backlog_and_missing_touches_are_not_claimable(self):
        issues = [
            issue(4, "touches: docs/**\n", labels=("status:backlog",)),
            issue(5, "touches:\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["not_ready"], [4])
        self.assertEqual(result["missing_touches"], [5])

    def test_ready_needs_human_issue_is_not_a_candidate(self):
        issues = [
            issue(
                1,
                "touches: operator/slack-setup\n",
                labels=("status:ready", "needs-human"),
            ),
            issue(2, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        self.assertEqual([item["number"] for item in result["candidates"]], [2])
        self.assertEqual(result["operator_only"], [1])

    def test_claimed_needs_human_issue_is_not_resumable(self):
        issues = [
            issue(
                1,
                "touches: operator/slack-setup\n",
                labels=("status:in-progress", "agent:agent-a", "needs-human"),
            ),
            issue(2, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        self.assertIsNone(result["my_in_flight"])
        self.assertEqual([item["number"] for item in result["candidates"]], [2])
        self.assertEqual(result["operator_only"], [1])

    def test_active_needs_human_touches_still_block_overlapping_agent_work(self):
        issues = [
            issue(
                1,
                "touches: src/operator.py\n",
                labels=("status:in-progress", "needs-human"),
            ),
            issue(2, "touches: src/operator.py\n", labels=("status:ready",)),
            issue(3, "touches: docs/**\n", labels=("status:ready",)),
        ]

        result = build_candidates(issues, "agent-a")

        self.assertEqual([item["number"] for item in result["candidates"]], [3])
        self.assertEqual(result["conflicted"][0]["number"], 2)
        self.assertEqual(result["operator_only"], [1])

    @patch.object(fetch_next_issue, "update_status", return_value=True)
    @patch.object(fetch_next_issue, "run_cmd")
    def test_reaper_synchronizes_board_before_removing_claim(self, run_cmd, update_status):
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        stale = issue(8, "touches: src/**\n", labels=("agent:agent-a", "status:in-progress"))
        stale["updatedAt"] = old

        def command_result(command, check=False):
            if command[:3] == ["gh", "pr", "list"]:
                return 0, "[]", ""
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return 0, "", ""
            return 0, "", ""

        run_cmd.side_effect = command_result

        current = dict(stale)
        with patch.object(fetch_next_issue, "get_issue", side_effect=[current, current]):
            released = fetch_next_issue.reap_stale_claims([stale], 4)

        self.assertEqual(released, [8])
        update_status.assert_called_once_with(8, "Ready", require_board=True)
        release_command = run_cmd.call_args_list[-1].args[0]
        self.assertIn("--remove-assignee", release_command)
        self.assertIn("agent:agent-a", release_command)

    @patch.object(fetch_next_issue, "update_status", return_value=True)
    @patch.object(fetch_next_issue, "run_cmd")
    def test_reaper_returns_needs_human_claim_to_backlog(self, run_cmd, update_status):
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        stale = issue(
            9,
            "touches: operator/slack-setup\n",
            labels=("agent:agent-a", "status:in-progress", "needs-human"),
        )
        stale["updatedAt"] = old
        current = dict(stale)
        run_cmd.side_effect = [
            (0, "[]", ""),
            (0, "", ""),
            (0, "", ""),
        ]

        with patch.object(fetch_next_issue, "get_issue", side_effect=[current, current]):
            released = fetch_next_issue.reap_stale_claims([stale], 4)

        self.assertEqual(released, [9])
        update_status.assert_called_once_with(9, "Backlog", require_board=True)

    @patch.object(fetch_next_issue, "update_status", return_value=True)
    @patch.object(fetch_next_issue, "run_cmd")
    def test_reaper_detects_needs_human_label_added_after_stale_snapshot(
        self, run_cmd, update_status
    ):
        old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        stale = issue(
            9,
            "touches: operator/slack-setup\n",
            labels=("agent:agent-a", "status:in-progress"),
        )
        stale["updatedAt"] = old
        current = issue(
            9,
            "touches: operator/slack-setup\n",
            labels=("agent:agent-a", "status:in-progress", "needs-human"),
        )
        current["updatedAt"] = old
        run_cmd.side_effect = [
            (0, "[]", ""),
            (0, "", ""),
            (0, "", ""),
        ]

        with patch.object(fetch_next_issue, "get_issue", side_effect=[current, current]):
            released = fetch_next_issue.reap_stale_claims([stale], 4)

        self.assertEqual(released, [9])
        update_status.assert_called_once_with(9, "Backlog", require_board=True)


class ClaimWalkTests(unittest.TestCase):
    @patch("claim_issue.claim_issue")
    @patch.object(fetch_next_issue, "get_current_branch", return_value="main")
    @patch.object(fetch_next_issue, "list_open_pr_files_by_issue", return_value={})
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_claim_rebuilds_candidates_after_conflict(
        self, list_open_issues, _pr_files, _branch, claim_issue_fn
    ):
        """After losing #10, rebuild so #11 overlapping touches is deferred."""
        initial = [
            issue(10, "touches: src/a.py\n", labels=("status:ready",)),
            issue(11, "touches: src/a.py\n", labels=("status:ready",)),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]
        after_conflict = [
            issue(
                10,
                "touches: src/a.py\n",
                labels=("agent:agent-a", "status:in-progress"),
            ),
            issue(11, "touches: src/a.py\n", labels=("status:ready",)),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]
        list_open_issues.side_effect = [initial, after_conflict]
        claim_issue_fn.side_effect = [
            fetch_next_issue_claim_conflict(),
            fetch_next_issue_claim_ok(),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--claim", "--json"]):
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"claimed_now": 12', output)
        self.assertEqual(
            [call.args[0] for call in claim_issue_fn.call_args_list],
            [10, 12],
        )
        self.assertEqual(list_open_issues.call_count, 2)

    @patch.object(fetch_next_issue, "get_current_branch", return_value="feat/issue-99-stale")
    @patch.object(fetch_next_issue, "list_open_pr_files_by_issue", return_value={})
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_stale_branch_resume_is_ignored(self, list_open_issues, _pr_files, _branch):
        list_open_issues.return_value = [
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--json"]):
            buf = io.StringIO()
            err = io.StringIO()
            with patch.object(sys, "stdout", buf), patch.object(sys, "stderr", err):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"resumable_in_flight_issue": null', output)
        self.assertIn("ignoring stale resume", err.getvalue())

    @patch.object(fetch_next_issue, "get_current_branch", return_value="feat/issue-9-wip")
    @patch.object(fetch_next_issue, "list_open_pr_files_by_issue", return_value={})
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_active_branch_resume_is_honored(self, list_open_issues, _pr_files, _branch):
        list_open_issues.return_value = [
            issue(
                9,
                "touches: src/**\n",
                labels=("agent:agent-b", "status:in-progress"),
            ),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--json"]):
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                fetch_next_issue.main()
            output = buf.getvalue()

        self.assertIn('"resumable_in_flight_issue": 9', output)

    @patch.object(fetch_next_issue, "get_current_branch", return_value="feat/issue-9-operator")
    @patch.object(fetch_next_issue, "list_open_pr_files_by_issue", return_value={})
    @patch.object(fetch_next_issue, "list_open_issues")
    def test_needs_human_branch_resume_is_ignored(self, list_open_issues, _pr_files, _branch):
        list_open_issues.return_value = [
            issue(
                9,
                "touches: operator/slack-setup\n",
                labels=("agent:agent-b", "status:in-progress", "needs-human"),
            ),
            issue(12, "touches: docs/**\n", labels=("status:ready",)),
        ]

        with patch.object(sys, "argv", ["fetch_next_issue.py", "--agent", "agent-b", "--json"]):
            buf = io.StringIO()
            err = io.StringIO()
            with patch.object(sys, "stdout", buf), patch.object(sys, "stderr", err):
                fetch_next_issue.main()

        self.assertIn('"resumable_in_flight_issue": null', buf.getvalue())
        self.assertIn('"next_progressive_issue": 12', buf.getvalue())
        self.assertIn("operator-only (needs-human)", err.getvalue())


class PrFileReservationTests(unittest.TestCase):
    def test_closes_line_beats_branch_name(self):
        pr = {
            "body": "Closes #20\n",
            "headRefName": "feat/issue-99-other",
            "files": [{"path": "src/a.py"}],
        }
        self.assertEqual(fetch_next_issue.linked_issue_numbers_from_pr(pr), [20])

    def test_branch_name_used_when_body_has_no_closes(self):
        pr = {
            "body": "no closure",
            "headRefName": "fix/issue-21-hotfix",
            "files": [{"path": "src/b.py"}],
        }
        self.assertEqual(fetch_next_issue.linked_issue_numbers_from_pr(pr), [21])

    def test_maps_union_of_files_per_issue(self):
        prs = [
            {
                "body": "Closes #20",
                "headRefName": "feat/issue-20-a",
                "files": [{"path": "src/a.py"}, {"path": "src/b.py"}],
            },
            {
                "body": "Closes #20",
                "headRefName": "feat/issue-20-followup",
                "files": [{"path": "src/b.py"}, {"path": "src/c.py"}],
            },
        ]
        mapping = fetch_next_issue.pr_files_by_issue_from_prs(prs)
        self.assertEqual(mapping[20], ["src/a.py", "src/b.py", "src/c.py"])

    def test_lookup_failure_returns_empty_map(self):
        with patch.object(fetch_next_issue, "get_repo_slug", return_value=None):
            self.assertEqual(fetch_next_issue.list_open_pr_files_by_issue(), {})

    def test_graphql_error_returns_empty_map(self):
        with patch.object(fetch_next_issue, "get_repo_slug", return_value="o/r"), \
             patch.object(fetch_next_issue, "run_gh_json", return_value={"errors": ["boom"]}):
            self.assertEqual(fetch_next_issue.list_open_pr_files_by_issue(), {})

    def test_truncated_snapshot_omits_the_issue(self):
        prs = [{
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 101,
            "files": [{"path": f"src/{i}.py"} for i in range(100)],
        }]
        self.assertEqual(fetch_next_issue.pr_files_by_issue_from_prs(prs), {})

    def test_mixed_partial_file_lists_omit_that_issue(self):
        prs = [
            {
                "body": "Closes #20",
                "headRefName": "feat/issue-20-a",
                "files": [{"path": "src/a.py"}],
            },
            {
                "body": "Closes #20",
                "headRefName": "feat/issue-20-b",
                "files": [],
            },
            {
                "body": "Closes #21",
                "headRefName": "feat/issue-21-a",
                "files": [{"path": "src/b.py"}],
            },
        ]
        mapping = fetch_next_issue.pr_files_by_issue_from_prs(prs)
        self.assertNotIn(20, mapping)
        self.assertEqual(mapping[21], ["src/b.py"])

    def test_malformed_file_entry_omits_that_issue(self):
        prs = [{
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "files": [{"path": "src/a.py"}, None],
        }]
        self.assertEqual(fetch_next_issue.pr_files_by_issue_from_prs(prs), {})

    def test_non_dict_pr_record_returns_empty_map(self):
        prs = [
            {
                "body": "Closes #21",
                "headRefName": "feat/issue-21-a",
                "files": [{"path": "src/b.py"}],
            },
            None,
        ]
        self.assertEqual(fetch_next_issue.pr_files_by_issue_from_prs(prs), {})

    def test_rename_with_previous_path_locks_both_sides(self):
        prs = [{
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "files": [{
                "path": "src/new.py",
                "changeType": "RENAMED",
                "previousFileName": "src/old.py",
            }],
        }]
        mapping = fetch_next_issue.pr_files_by_issue_from_prs(prs)
        self.assertEqual(mapping[20], ["src/new.py", "src/old.py"])

    def test_rename_without_previous_path_omits_the_issue(self):
        prs = [{
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "files": [{"path": "src/new.py", "changeType": "RENAMED"}],
        }]
        self.assertEqual(fetch_next_issue.pr_files_by_issue_from_prs(prs), {})

    def test_graphql_nodes_normalize_into_mapper_records(self):
        record = fetch_next_issue._normalize_pr_file_record({
            "number": 7,
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 1,
            "files": {"nodes": [{"path": "src/a.py", "changeType": "MODIFIED"}]},
        })
        mapping = fetch_next_issue.pr_files_by_issue_from_prs([record])
        self.assertEqual(mapping[20], ["src/a.py"])

    def _graphql_page(self, nodes, page_info):
        return {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": page_info,
                        "nodes": nodes,
                    }
                }
            }
        }

    def test_rest_rename_locks_previous_filename(self):
        node = {
            "number": 7,
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 1,
            "files": {"nodes": [{"path": "src/new.py", "changeType": "RENAMED"}]},
        }
        graphql = self._graphql_page([node], {"hasNextPage": False, "endCursor": None})
        rest = [{
            "filename": "src/new.py",
            "status": "renamed",
            "previous_filename": "src/old.py",
        }]

        def fake_gh(cmd):
            if cmd[:3] == ["gh", "api", "graphql"]:
                return graphql
            if "repos/o/r/pulls/7/files" in cmd:
                return rest
            return None

        with patch.object(fetch_next_issue, "get_repo_slug", return_value="o/r"), \
             patch.object(fetch_next_issue, "run_gh_json", side_effect=fake_gh):
            mapping = fetch_next_issue.list_open_pr_files_by_issue()
        self.assertEqual(mapping[20], ["src/new.py", "src/old.py"])

    def test_missing_page_info_fails_closed(self):
        node = {
            "number": 7,
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 1,
            "files": {"nodes": [{"path": "src/a.py"}]},
        }
        graphql = self._graphql_page([node], None)
        graphql["data"]["repository"]["pullRequests"].pop("pageInfo", None)

        with patch.object(fetch_next_issue, "get_repo_slug", return_value="o/r"), \
             patch.object(fetch_next_issue, "run_gh_json", return_value=graphql):
            self.assertIsNone(fetch_next_issue.load_open_pr_file_records())
            self.assertEqual(fetch_next_issue.list_open_pr_files_by_issue(), {})

    def test_empty_page_info_fails_closed(self):
        node = {
            "number": 7,
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 1,
            "files": {"nodes": [{"path": "src/a.py"}]},
        }
        graphql = self._graphql_page([node], {})

        with patch.object(fetch_next_issue, "get_repo_slug", return_value="o/r"), \
             patch.object(fetch_next_issue, "run_gh_json", return_value=graphql):
            self.assertIsNone(fetch_next_issue.load_open_pr_file_records())

    def test_non_object_page_info_fails_closed(self):
        node = {
            "number": 7,
            "body": "Closes #20",
            "headRefName": "feat/issue-20-a",
            "changedFiles": 1,
            "files": {"nodes": [{"path": "src/a.py"}]},
        }
        graphql = self._graphql_page([node], "bad")

        with patch.object(fetch_next_issue, "get_repo_slug", return_value="o/r"), \
             patch.object(fetch_next_issue, "run_gh_json", return_value=graphql):
            self.assertIsNone(fetch_next_issue.load_open_pr_file_records())


def fetch_next_issue_claim_conflict():
    import claim_issue
    return claim_issue.EXIT_CONFLICT


def fetch_next_issue_claim_ok():
    import claim_issue
    return claim_issue.EXIT_OK


if __name__ == "__main__":
    unittest.main()
