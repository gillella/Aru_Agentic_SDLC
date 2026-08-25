import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claim_issue  # noqa: E402
import fetch_next_issue  # noqa: E402
import fetch_next_work as fnw  # noqa: E402


def qualified_issue(*, status="backlog", extra_labels=()):
    return {
        "number": 10,
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
            {"name": f"status:{status}"}, {"name": "priority:p0"},
            {"name": "type:feat"},
            *({"name": name} for name in extra_labels),
        ],
        "author": {"login": "owner"},
        "updatedAt": "2026-08-25T18:00:00Z",
    }


class PickerTransitionGuardTests(unittest.TestCase):
    def setUp(self):
        self.inventory_reads = 0

        def inventory(_slug, numbers):
            self.inventory_reads += 1
            return ({number: "Backlog" for number in numbers},
                    int(self.inventory_reads >= 3))

        self.enterContext(patch.object(
            fnw.merge_pr, "repository_merge_lock",
            return_value=nullcontext((True, "locked"))))
        self.enterContext(patch.object(
            fnw, "select", return_value={"work": {"type": "idle"}}))
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
                          side_effect=[[issue], [issue], [changed]]), \
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

    def test_claim_refuses_when_lifecycle_lock_is_busy(self):
        with patch.object(
            claim_issue.merge_pr, "repository_merge_lock",
            return_value=nullcontext((False, "busy")),
        ), patch.object(claim_issue, "get_issue") as get_issue:
            self.assertEqual(claim_issue.claim_issue(10, "agent-1"),
                             claim_issue.EXIT_ERROR)
        get_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
