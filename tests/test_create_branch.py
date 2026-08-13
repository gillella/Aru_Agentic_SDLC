import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import create_branch as cb


class CreateBranchPlanGateTests(unittest.TestCase):
    def test_requires_plan_for_feat_branch_type(self):
        self.assertTrue(cb.requires_plan({}, branch_type="feat"))

    def test_requires_plan_for_needs_design_label(self):
        issue = {"labels": [{"name": "needs-design"}, {"name": "type:chore"}]}
        self.assertTrue(cb.requires_plan(issue, branch_type="chore"))

    def test_requires_plan_for_high_risk_terms(self):
        issue = {"body": "This changes the database schema and user migration path.", "labels": []}
        self.assertTrue(cb.requires_plan(issue, branch_type="chore"))

    def test_does_not_require_plan_for_routine_chore_or_fix(self):
        issue = {"body": "Simple bug fix in parser.", "labels": [{"name": "type:fix"}]}
        self.assertFalse(cb.requires_plan(issue, branch_type="fix"))

    def test_has_implementation_plan_in_issue_body(self):
        issue = {"body": "## Implementation Plan\n\n- Step 1\n- Step 2"}
        self.assertTrue(cb.has_implementation_plan(104, issue=issue, comments=[]))

    def test_has_implementation_plan_in_issue_comments(self):
        issue = {"body": "Feature request."}
        comments = [{"body": "### Implementation Plan\n\nApproach details..."}]
        self.assertTrue(cb.has_implementation_plan(104, issue=issue, comments=comments))

    def test_has_implementation_plan_returns_false_when_missing(self):
        issue = {"body": "Feature request without plan."}
        comments = [{"body": "General comment."}]
        self.assertFalse(cb.has_implementation_plan(104, issue=issue, comments=comments))

    @patch("create_branch.get_issue")
    @patch("create_branch.has_implementation_plan")
    def test_create_branch_refuses_unplanned_feature(self, mock_has_plan, mock_get_issue):
        mock_get_issue.return_value = {"title": "feat: new feature", "labels": [{"name": "type:feat"}]}
        mock_has_plan.return_value = False

        with patch("sys.stderr.write") as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                cb.create_branch(999, branch_type="feat", fetch_remote=False)
            self.assertEqual(ctx.exception.code, 1)
            written = "".join(call.args[0] for call in mock_stderr.call_args_list)
            self.assertIn("[BLOCKED] Plan gate", written)
            self.assertIn("gh issue comment 999", written)

    @patch("create_branch.create_worktree")
    @patch("create_branch.get_issue")
    @patch("create_branch.has_implementation_plan")
    def test_create_branch_succeeds_for_planned_feature(self, mock_has_plan, mock_get_issue, mock_worktree):
        mock_get_issue.return_value = {"title": "feat: planned feature", "labels": [{"name": "type:feat"}]}
        mock_has_plan.return_value = True
        mock_worktree.return_value = ".worktrees/feat-issue-999-planned-feature"

        path = cb.create_branch(999, branch_type="feat", use_worktree=True, fetch_remote=False)
        self.assertEqual(path, ".worktrees/feat-issue-999-planned-feature")


if __name__ == "__main__":
    unittest.main()
