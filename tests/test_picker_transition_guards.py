import io
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402
import fetch_next_issue  # noqa: E402
import fetch_next_work as fnw  # noqa: E402


def qualified_issue(number=10, priority="p0", *, status="backlog", extra_labels=()):
    return {
        "number": number,
        "title": "qualified",
        "body": (
            "## Acceptance Criteria\n"
            "- [ ] Works (verify: `python3 -m unittest tests.test_example`)\n\n"
            "## Decision Boundaries\n- Default: bounded\n\n"
            "## Non-Goals\n- No extras\n\n"
            "## Verification\n- `python3 -m unittest tests.test_example`\n\n"
            "touches: scripts/example.py, tests/test_example.py\n"
            "depends-on: none\n"
        ),
        "labels": [
            {"name": f"status:{status}"}, {"name": f"priority:{priority}"},
            {"name": "type:feat"},
            *({"name": name} for name in extra_labels),
        ],
        "author": {"login": "owner"},
        "updatedAt": "2026-08-25T18:00:00Z",
    }


class PickerTransitionGuardTests(unittest.TestCase):
    def setUp(self):
        self.inventory_reads = 0
        self.select_reads = 0
        self.post_ready_issue = 10
        self.post_selected_issue = 10

        def inventory(_slug, numbers):
            self.inventory_reads += 1
            return ({number: ("Ready" if self.inventory_reads >= 3
                              and number == self.post_ready_issue
                              else "Backlog") for number in numbers},
                    int(self.inventory_reads >= 3))

        def select(*_args):
            self.select_reads += 1
            return {"work": ({"type": "issue", "issue": self.post_selected_issue}
                             if self.select_reads >= 3 else {"type": "idle"})}

        self.enterContext(patch.object(
            fnw.merge_pr, "repository_merge_lock",
            return_value=nullcontext((True, "locked"))))
        self.enterContext(patch.object(
            fnw, "select", side_effect=select))
        self.enterContext(patch.object(
            fnw, "_governed_open_issue_statuses",
            side_effect=inventory))

    def qualification_context(self):
        return (
            patch.object(fnw, "list_work_prs", return_value=[]),
            patch.object(fnw, "active_increment_scope", return_value=None),
            patch.object(fnw, "get_repo_slug", return_value="owner/repo"),
            patch.object(fetch_next_issue, "repository_trusted_logins",
                         return_value={"owner"}),
            patch.object(fetch_next_issue, "repository_owner_login",
                         return_value="owner"),
            patch.object(fetch_next_issue, "is_trusted_metadata_author",
                         return_value=True),
        )

    def test_late_needs_human_change_is_rolled_back_and_reported(self):
        issue = qualified_issue()
        changed = qualified_issue(status="ready", extra_labels=("needs-human",))
        with patch.object(fnw, "list_open_issues",
                          side_effect=[[issue], [issue], [changed], [changed]]), \
             self.qualification_context()[0], self.qualification_context()[1], \
             self.qualification_context()[2], self.qualification_context()[3], \
             self.qualification_context()[4], self.qualification_context()[5], \
             patch.object(fnw, "query_issue_project_items", side_effect=[
                 [{"status": {"name": "Backlog"}}],
                 [{"status": {"name": "Ready"}}],
             ]), patch.object(
                 fnw, "select_governed_project_items",
                 side_effect=lambda items, _slug: items,
             ), patch.object(fnw, "update_status", return_value=True) as update:
            with self.assertRaises(fnw.AutoTriageError):
                fnw.promote_one_idle_backlog_issue("agent-1")

        self.assertEqual(update.call_args_list, [
            call(10, "Ready", require_board=True, expected_status="Backlog",
                 require_unclaimed=True,
                 expected_updated_at="2026-08-25T18:00:00Z"),
            call(10, "Backlog", require_board=True, expected_status="Ready",
                 require_unclaimed=True),
        ])

    def test_increment_lookup_failure_leaves_backlog_untouched(self):
        issue = qualified_issue()
        contexts = self.qualification_context()
        with patch.object(fnw, "list_open_issues", return_value=[issue]), \
             contexts[0], patch.object(
                 fnw, "active_increment_scope", side_effect=RuntimeError("broken")
             ), contexts[2], contexts[3], contexts[4], contexts[5], \
             patch.object(fnw, "update_status") as update:
            candidate, _slug = fnw._idle_backlog_candidate("agent-1")
        self.assertIsNone(candidate)
        update.assert_not_called()

    def test_unavailable_board_inventory_is_an_error_not_idle(self):
        issue = qualified_issue()
        contexts = self.qualification_context()
        with patch.object(fnw, "list_open_issues", return_value=[issue]), \
             contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
             contexts[5], patch.object(
                 fnw, "_governed_open_issue_statuses", return_value=None,
             ), patch.object(fnw, "update_status") as update:
            with self.assertRaisesRegex(
                fnw.AutoTriageError,
                "cannot prove Ready is empty",
            ):
                fnw.promote_one_idle_backlog_issue("agent-1")
        update.assert_not_called()

    def test_lost_board_readback_after_write_rolls_back(self):
        issue = qualified_issue()
        post = qualified_issue(status="ready")
        contexts = self.qualification_context()
        backlog = ({issue["number"]: "Backlog"}, 0)
        with patch.object(
                 fnw, "list_open_issues",
                 side_effect=[[issue], [issue], [post], [post]],
             ), contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
             contexts[5], patch.object(
                 fnw, "_governed_open_issue_statuses",
                 side_effect=[backlog, backlog, None],
             ), patch.object(
                 fnw, "query_issue_project_items",
                 return_value=[{"status": {"name": "Backlog"}}],
             ), patch.object(
                 fnw, "select_governed_project_items",
                 side_effect=lambda items, _slug: items,
             ), patch.object(fnw, "update_status", return_value=True) as update:
            with self.assertRaisesRegex(
                fnw.AutoTriageError,
                "failed authoritative readback",
            ):
                fnw.promote_one_idle_backlog_issue("agent-1")
        self.assertEqual(update.call_args_list, [
            call(10, "Ready", require_board=True, expected_status="Backlog",
                 require_unclaimed=True,
                 expected_updated_at="2026-08-25T18:00:00Z"),
            call(10, "Backlog", require_board=True, expected_status="Ready",
                 require_unclaimed=True),
        ])

    def test_claiming_picker_reports_unavailable_board_instead_of_idle(self):
        idle = {
            "agent": "agent-1", "family": "openai",
            "work": {"type": "idle", "skill": None},
            "skipped_prs": [], "merge_skipped": [], "claimable_issues": [],
        }
        stdout = io.StringIO()
        with patch.object(fnw, "_resolve_identity", return_value=None), \
             patch.object(fnw, "select", return_value=idle), \
             patch.object(
                 fnw, "promote_one_idle_backlog_issue",
                 side_effect=fnw.AutoTriageError(
                     "Project inventory unavailable; cannot prove Ready is empty"
                 ),
             ), patch(
                 "sys.argv",
                 ["fetch_next_work.py", "--agent", "agent-1", "--family", "openai",
                  "--claim", "--json", "--reap-after", "0"],
             ), patch("sys.stdout", stdout):
            result = fnw.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload["work"]["type"], "error")
        self.assertIn("cannot prove Ready is empty", payload["work"]["reason"])

    def test_claim_refuses_when_lifecycle_lock_is_busy(self):
        with patch.object(
            claim_issue.merge_pr, "repository_merge_lock",
            return_value=nullcontext((False, "busy")),
        ), patch.object(claim_issue, "get_issue") as get_issue:
            self.assertEqual(claim_issue.claim_issue(10, "agent-1"),
                             claim_issue.EXIT_ERROR)
        get_issue.assert_not_called()

    def test_full_picker_must_still_be_idle_immediately_before_write(self):
        issue = qualified_issue()
        contexts = self.qualification_context()
        with patch.object(fnw, "list_open_issues", side_effect=[[issue], [issue]]), \
             contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
             contexts[5], patch.object(fnw, "select", side_effect=[
                 {"work": {"type": "idle"}},
                 {"work": {"type": "feedback", "pr": 99}},
             ]), patch.object(fnw, "update_status") as update:
            self.assertIsNone(fnw.promote_one_idle_backlog_issue("agent-1"))
        update.assert_not_called()

    def test_post_picker_must_select_the_same_highest_priority_issue(self):
        issue = qualified_issue()
        post = qualified_issue(status="ready")
        contexts = self.qualification_context()
        with patch.object(fnw, "list_open_issues",
                          side_effect=[[issue], [issue], [post], [post]]), \
             contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
             contexts[5], patch.object(fnw, "select", side_effect=[
                 {"work": {"type": "idle"}}, {"work": {"type": "idle"}},
                 {"work": {"type": "issue", "issue": 99}},
             ]), patch.object(fnw, "query_issue_project_items",
                              return_value=[{"status": {"name": "Backlog"}}]), \
             patch.object(fnw, "select_governed_project_items",
                          side_effect=lambda items, _slug: items), \
             patch.object(fnw, "update_status", return_value=True) as update:
            with self.assertRaises(fnw.AutoTriageError):
                fnw.promote_one_idle_backlog_issue("agent-1")
        self.assertEqual(update.call_count, 2)

    def test_missing_repository_identity_blocks_increment_lookup(self):
        with patch.object(fetch_next_issue, "repo_project_id", return_value=None), \
             patch.object(fetch_next_issue, "DeliveryIncrementStore"):
            with self.assertRaisesRegex(RuntimeError, "repository identity"):
                fetch_next_issue.active_increment_scope(fail_on_error=True)

    def test_post_ranking_rolls_back_for_new_higher_priority_backlog(self):
        selected = qualified_issue(20, "p1")
        post = qualified_issue(20, "p1", status="ready")
        higher = qualified_issue(10, "p0")
        self.post_ready_issue = self.post_selected_issue = 20
        contexts = self.qualification_context()
        with patch.object(fnw, "list_open_issues", side_effect=[
                 [selected], [selected], [post, higher], [post, higher],
             ]), contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], \
             contexts[5], patch.object(fnw, "query_issue_project_items",
                                        return_value=[{"status": {"name": "Backlog"}}]), \
             patch.object(fnw, "select_governed_project_items",
                          side_effect=lambda items, _slug: items), \
             patch.object(fnw, "update_status", return_value=True) as update:
            with self.assertRaises(fnw.AutoTriageError):
                fnw.promote_one_idle_backlog_issue("agent-1")
        self.assertEqual(update.call_count, 2)


if __name__ == "__main__":
    unittest.main()
