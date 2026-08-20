import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue  # noqa: E402
from fetch_next_issue import (  # noqa: E402
    build_candidates,
    parse_dependencies,
    sync_board_priority,
)


def issue(number, body="", labels=(), author="owner"):
    label_list = list(labels)
    if "status:ready" in label_list and not any(
        name.startswith("priority:") for name in label_list
    ):
        # Pickers fail closed on missing priority metadata; tests that are
        # not exercising priority behavior default ready work to P3.
        label_list.append("priority:p3")
    record = {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in label_list],
    }
    if author is not None:
        record["author"] = {"login": author}
    return record


class TrustedOwnerTests(unittest.TestCase):
    def setUp(self):
        owner = patch.object(
            fetch_next_issue, "repository_owner_login", return_value="owner")
        trusted = patch.object(
            fetch_next_issue, "repository_trusted_logins", return_value={"owner"})
        self.addCleanup(owner.stop)
        self.addCleanup(trusted.stop)
        owner.start()
        trusted.start()


class IssueSelectionTests(TrustedOwnerTests):
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

    def test_in_review_issue_releases_touches_and_is_not_candidate(self):
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

        # Issue 20 remains parked, but its open PR releases the implementation
        # reservation so both Ready issues can start.
        self.assertEqual(
            [item["number"] for item in result["candidates"]], [21, 22]
        )
        self.assertEqual(result["conflicted"], [])

    def test_in_review_pr_files_do_not_restore_released_reservation(self):
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

        self.assertEqual(
            [item["number"] for item in result["candidates"]], [21, 22]
        )
        self.assertEqual(result["conflicted"], [])

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


class ClaimWalkTests(TrustedOwnerTests):
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


def _raw_issue(number, labels=(), body="touches: docs/a.md\n",
               title=None, author="owner"):
    """Issue record without the helper's automatic priority defaulting."""
    return {
        "number": number,
        "title": title or f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
        "author": {"login": author} if author else None,
    }


class AuthorizedIncrementSelectionTests(TrustedOwnerTests):
    def test_candidates_must_belong_to_the_active_increment_scope(self):
        issues = [
            issue(10, "touches: docs/a.md\n", labels=("status:ready", "priority:p0")),
            issue(11, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
            issue(12, "touches: docs/c.md\n", labels=("status:ready", "priority:p0")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={10, 11})
        self.assertEqual([i["number"] for i in result["candidates"]], [10, 11])
        self.assertNotIn(12, [i["number"] for i in result["candidates"]])

    def test_empty_increment_scope_claims_nothing(self):
        issues = [
            issue(1, "touches: docs/a.md\n", labels=("status:ready",)),
        ]
        result = build_candidates(issues, "agent-a", increment_scope=set())
        self.assertEqual(result["candidates"], [])
        self.assertEqual([i["number"] for i in result["future_inventory"]], [1])

    def test_dependency_blocked_story_stays_blocked_even_in_scope(self):
        issues = [
            issue(99, "touches: docs/z.md\n", labels=("status:backlog",)),
            issue(20, "touches: docs/a.md\ndepends-on: #99\n",
                  labels=("status:ready", "priority:p0")),
            issue(10, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={20, 10, 99})
        self.assertEqual([i["number"] for i in result["candidates"]], [10])
        self.assertEqual(result["blocked"], [{"number": 20, "blocked_by": [99]}])


class FutureReadyIsolationTests(TrustedOwnerTests):
    def test_out_of_scope_ready_stories_are_future_inventory_not_claimable(self):
        issues = [
            issue(1, "touches: docs/a.md\n", labels=("status:ready", "priority:p0")),
            issue(2, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={1})
        self.assertEqual([i["number"] for i in result["candidates"]], [1])
        self.assertEqual([i["number"] for i in result["future_inventory"]], [2])

    def test_higher_priority_out_of_scope_still_not_claimable(self):
        issues = [
            issue(10, "touches: docs/a.md\n", labels=("status:ready", "priority:p0")),
            issue(11, "touches: docs/b.md\n", labels=("status:ready", "priority:p3")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={11})
        self.assertEqual([i["number"] for i in result["candidates"]], [11])
        self.assertEqual([i["number"] for i in result["future_inventory"]], [10])


class PriorityOrderingTests(TrustedOwnerTests):
    def test_candidates_sort_p0_p1_p2_p3_then_lowest_number(self):
        issues = [
            issue(30, "touches: docs/a.md\n", labels=("status:ready", "priority:p3")),
            issue(10, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
            issue(20, "touches: docs/c.md\n", labels=("status:ready", "priority:p0")),
            issue(5, "touches: docs/d.md\n", labels=("status:ready", "priority:p2")),
            issue(15, "touches: docs/e.md\n", labels=("status:ready", "priority:p0")),
        ]
        result = build_candidates(
            issues, "agent-a", increment_scope={30, 10, 20, 5, 15}
        )
        nums = [i["number"] for i in result["candidates"]]
        self.assertEqual(nums, [15, 20, 10, 5, 30])

    def test_lowest_number_is_the_tie_break(self):
        issues = [
            issue(42, "touches: docs/a.md\n", labels=("status:ready", "priority:p1")),
            issue(7, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={42, 7})
        self.assertEqual([i["number"] for i in result["candidates"]], [7, 42])


class DependencyPriorityTests(TrustedOwnerTests):
    def test_blocked_p0_skipped_without_weakening_dependency(self):
        issues = [
            issue(99, "touches: docs/z.md\n", labels=("status:backlog",)),
            issue(20, "touches: docs/a.md\ndepends-on: #99\n",
                  labels=("status:ready", "priority:p0")),
            issue(10, "touches: docs/b.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={20, 10})
        self.assertEqual([i["number"] for i in result["candidates"]], [10])
        self.assertEqual(result["blocked"], [{"number": 20, "blocked_by": [99]}])

    def test_highest_priority_unblocked_authorized_story_is_selected(self):
        issues = [
            issue(30, "touches: docs/a.md\n", labels=("status:ready", "priority:p2")),
            issue(20, "touches: docs/b.md\n", labels=("status:ready", "priority:p0")),
            issue(10, "touches: docs/c.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={30, 20, 10})
        self.assertEqual([i["number"] for i in result["candidates"]], [20, 10, 30])


class PriorityIntegrityTests(TrustedOwnerTests):
    def test_missing_priority_fails_closed_and_is_reported(self):
        issues = [
            _raw_issue(5, labels=("status:ready",)),
            issue(6, "touches: docs/b.md\n", labels=("status:ready", "priority:p0")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={5, 6})
        self.assertEqual([i["number"] for i in result["candidates"]], [6])
        self.assertEqual(
            result["integrity_issues"],
            [{"number": 5, "reason": "missing priority:pN label"}],
        )

    def test_duplicate_priority_fails_closed(self):
        issues = [
            _raw_issue(5, labels=("status:ready", "priority:p1", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={5})
        self.assertEqual(result["candidates"], [])
        self.assertIn(5, [i["number"] for i in result["integrity_issues"]])

    def test_contradictory_priority_fails_closed(self):
        issues = [
            _raw_issue(5, labels=("status:ready", "priority:p0", "priority:p2")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={5})
        self.assertEqual(result["candidates"], [])
        report = result["integrity_issues"][0]
        self.assertEqual(report["number"], 5)
        self.assertIn("contradictory", report["reason"])


class PriorityFieldSyncTests(TrustedOwnerTests):
    def test_agreement_returns_canonical_without_sync(self):
        iss = issue(5, "touches: docs/a.md\n", labels=("status:ready", "priority:p0"))
        with patch("fetch_next_issue.get_issue_priority_field", return_value="P0"), \
             patch("fetch_next_issue.set_issue_priority_field") as setter:
            self.assertEqual(sync_board_priority(iss), "P0")
            setter.assert_not_called()

    def test_disagreement_syncs_field_from_canonical_label(self):
        iss = issue(5, "touches: docs/a.md\n", labels=("status:ready", "priority:p1"))
        with patch("fetch_next_issue.get_issue_priority_field", return_value="P2"), \
             patch("fetch_next_issue.set_issue_priority_field", return_value=True) as setter:
            self.assertEqual(sync_board_priority(iss), "P1")
            setter.assert_called_once_with(5, "P1")

    def test_disagreement_fails_closed_when_sync_fails(self):
        iss = issue(5, "touches: docs/a.md\n", labels=("status:ready", "priority:p0"))
        with patch("fetch_next_issue.get_issue_priority_field", return_value="P3"), \
             patch("fetch_next_issue.set_issue_priority_field", return_value=False):
            self.assertIsNone(sync_board_priority(iss))

    def test_unreadable_field_fails_closed(self):
        iss = issue(5, "touches: docs/a.md\n", labels=("status:ready", "priority:p0"))
        with patch("fetch_next_issue.get_issue_priority_field", return_value=None):
            self.assertIsNone(sync_board_priority(iss))


class OperatorOnlyTests(TrustedOwnerTests):
    def test_needs_human_p0_in_scope_remains_unclaimable(self):
        issues = [
            issue(1, "touches: operator/x\n",
                  labels=("status:ready", "priority:p0", "needs-human")),
            issue(2, "touches: docs/a.md\n", labels=("status:ready", "priority:p1")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={1, 2})
        self.assertEqual([i["number"] for i in result["candidates"]], [2])
        self.assertEqual(result["operator_only"], [1])

    def test_needs_human_p0_never_becomes_future_inventory_or_candidate(self):
        issues = [
            issue(1, "touches: operator/x\n",
                  labels=("status:ready", "priority:p0", "needs-human")),
        ]
        result = build_candidates(issues, "agent-a", increment_scope={1})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["future_inventory"], [])
        self.assertEqual(result["operator_only"], [1])
