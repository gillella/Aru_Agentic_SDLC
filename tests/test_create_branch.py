import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import create_branch as cb


class WorktreeAdmissionTests(unittest.TestCase):
    @staticmethod
    def issue(*labels, state="OPEN"):
        return {
            "title": "fix: safe branch",
            "body": "Routine parser fix.",
            "state": state,
            "labels": [{"name": label} for label in labels],
        }

    @staticmethod
    def project_item(status="In Progress", title="widgets Board"):
        return {
            "id": "ITEM_1",
            "status": {"name": status},
            "project": {
                "title": title,
                "repositories": {
                    "nodes": [{"nameWithOwner": "octocat/widgets"}],
                },
            },
        }

    def test_matching_claim_label_and_governed_board_are_admitted(self):
        gaps = cb.worktree_admission_gaps(
            self.issue("agent:agent-1", "status:in-progress"),
            "agent-1",
            [self.project_item()],
            "octocat/widgets",
        )
        self.assertEqual(gaps, [])

    @patch("create_branch.get_issue")
    @patch("create_branch.get_agent_id", return_value=None)
    def test_cli_requires_agent_before_git_mutation(self, _identity, issue):
        issue.return_value = self.issue("agent:agent-1", "status:in-progress")
        with patch("sys.argv", ["create_branch.py", "--issue", "999"]), \
             patch("create_branch.get_repo_slug", return_value=None), \
             patch("create_branch.create_worktree") as worktree:
            with self.assertRaisesRegex(SystemExit, "1"):
                cb.main()
        issue.assert_called_once_with(999)
        worktree.assert_not_called()

    def test_missing_ambiguous_or_divergent_authority_fails_closed(self):
        valid_issue = self.issue("agent:agent-1", "status:in-progress")
        cases = [
            (valid_issue, "", [self.project_item()], "octocat/widgets", "agent environment"),
            (None, "agent-1", [self.project_item()], "octocat/widgets", "issue is missing"),
            (self.issue("agent:agent-1", "status:in-progress", state="CLOSED"), "agent-1", [self.project_item()], "octocat/widgets", "not verifiably open"),
            (self.issue("status:in-progress"), "agent-1", [self.project_item()], "octocat/widgets", "settled claim"),
            (self.issue("agent:agent-1", "agent:agent-2", "status:in-progress"), "agent-1", [self.project_item()], "octocat/widgets", "settled claim"),
            (self.issue("agent:agent-2", "status:in-progress"), "agent-1", [self.project_item()], "octocat/widgets", "settled claim"),
            (self.issue("agent:agent-1", "status:ready"), "agent-1", [self.project_item()], "octocat/widgets", "status label"),
            (self.issue("agent:agent-1", "status:ready", "status:in-progress"), "agent-1", [self.project_item()], "octocat/widgets", "status label"),
            (valid_issue, "agent-1", None, "octocat/widgets", "Board state is unreadable"),
            (valid_issue, "agent-1", [], "octocat/widgets", "one governed Project Board"),
            (valid_issue, "agent-1", [self.project_item(title="Team"), self.project_item(title="Release")], "octocat/widgets", "one governed Project Board"),
            (valid_issue, "agent-1", [self.project_item("Ready")], "octocat/widgets", "must be In Progress"),
            (valid_issue, "agent-1", [self.project_item()], None, "repository identity"),
        ]
        for issue, agent, items, slug, fragment in cases:
            with self.subTest(fragment=fragment):
                gaps = cb.worktree_admission_gaps(issue, agent, items, slug)
                self.assertTrue(any(fragment.lower() in gap.lower() for gap in gaps), gaps)

    @patch("create_branch.run_cmd")
    @patch("create_branch.create_worktree")
    @patch("create_branch.query_issue_project_items", return_value=None)
    @patch("create_branch.get_repo_slug", return_value="octocat/widgets")
    @patch("create_branch.get_issue", return_value=None)
    def test_unreadable_authority_cannot_mutate_git(
        self, _issue, _slug, _items, worktree, run
    ):
        with self.assertRaisesRegex(SystemExit, "1"):
            cb.create_branch(999, branch_type="fix", use_worktree=True, agent="agent-1")
        worktree.assert_not_called()
        run.assert_not_called()

    @patch("create_branch.create_worktree", return_value=".worktrees/existing")
    @patch("create_branch.query_issue_project_items")
    @patch("create_branch.get_repo_slug", return_value="octocat/widgets")
    @patch("create_branch.get_issue")
    def test_same_agent_worktree_recovery_runs_after_live_admission(
        self, issue, _slug, items, worktree
    ):
        issue.return_value = self.issue("agent:agent-1", "status:in-progress")
        items.return_value = [self.project_item()]
        path = cb.create_branch(999, branch_type="fix", use_worktree=True, agent="agent-1")
        self.assertEqual(path, ".worktrees/existing")
        worktree.assert_called_once_with("fix/issue-999-safe-branch", agent="agent-1")

    @patch("create_branch.run_cmd", return_value=(0, "", ""))
    @patch("create_branch.require_worktree_admission")
    def test_untrusted_title_remains_argv_only_data(self, admission, run):
        admission.return_value = self.issue("agent:agent-1", "status:in-progress") | {
            "title": "fix: $(touch /tmp/owned); safe",
        }
        branch = cb.create_branch(999, branch_type="fix", agent="agent-1")
        self.assertNotIn("$", branch)
        self.assertNotIn(";", branch)
        run.assert_called_once_with(["git", "checkout", "-b", branch], check=False)


class CreateBranchPlanGateTests(unittest.TestCase):
    def test_requires_plan_for_feat_branch_type(self):
        self.assertTrue(cb.requires_plan({}, branch_type="feat"))

    def test_requires_plan_for_needs_design_label(self):
        issue = {"labels": [{"name": "needs-design"}, {"name": "type:chore"}]}
        self.assertTrue(cb.requires_plan(issue, branch_type="chore"))

    def test_requires_plan_for_high_risk_terms(self):
        for term in ["money", "pii", "schema", "migration", "tenancy", "security", "irreversible"]:
            with self.subTest(term=term):
                issue = {"body": f"This operation involves {term} handling.", "labels": []}
                self.assertTrue(cb.requires_plan(issue, branch_type="chore"))

    def test_does_not_require_plan_for_routine_chore_or_fix(self):
        issue = {"body": "Simple bug fix in parser.", "labels": [{"name": "type:fix"}]}
        self.assertFalse(cb.requires_plan(issue, branch_type="fix"))

    def test_has_implementation_plan_in_issue_body(self):
        issue = {"body": "## Implementation Plan\n\n### Proposed Changes\n- Update parser\n\n### Files to Touch\n- scripts/foo.py\n\n### Verification\n- Run tests"}
        self.assertTrue(cb.has_implementation_plan(104, issue=issue, comments=[]))

    def test_has_implementation_plan_in_issue_comments(self):
        issue = {"body": "Feature request."}
        comments = [{"body": "### Implementation Plan\n\n### Proposed Changes\n- Update api\n\n### Files to Touch\n- scripts/api.py\n\n### Verification\n- Run pytest"}]
        self.assertTrue(cb.has_implementation_plan(104, issue=issue, comments=comments))

    def test_minimal_low_risk_plan_rejected_for_high_risk_issue(self):
        low_risk_plan = (
            "## Implementation Plan\n\n"
            "### Proposed Changes\n- Update security auth checks\n\n"
            "### Files to Touch\n- auth.py\n\n"
            "### Verification\n- Run auth tests"
        )
        high_risk_issue = {
            "title": "feat: update token security and pii handling",
            "body": "Ensure security and pii encryption.",
            "labels": [{"name": "type:feat"}],
        }
        # Low risk plan passes for low risk
        self.assertTrue(cb.is_substantive_plan(low_risk_plan, is_risk=False))
        # But fails for high risk issue because schema/api and rejected alternatives are missing
        self.assertFalse(cb.is_substantive_plan(low_risk_plan, is_risk=True))
        self.assertFalse(cb.has_implementation_plan(104, issue=high_risk_issue, comments=[{"body": low_risk_plan}]))

    def test_full_high_risk_plan_accepted_for_high_risk_issue(self):
        high_risk_plan = (
            "## Implementation Plan\n\n"
            "### Proposed Changes\n- Update migration schema and pii encryption\n\n"
            "### Files to Touch\n- db/schema.py, security/auth.py\n\n"
            "### Schema / API Deltas\n- Adds encrypted_token column, preserves existing token API\n\n"
            "### Verification & Test Strategy\n- Run pytest tests/test_security.py\n\n"
            "### Rejected Alternatives\n- Considered storing raw tokens; rejected due to security risk"
        )
        high_risk_issue = {
            "title": "feat: schema migration for pii encryption",
            "body": "Migration and schema updates for pii.",
            "labels": [{"name": "type:feat"}],
        }
        self.assertTrue(cb.is_substantive_plan(high_risk_plan, is_risk=True))
        self.assertTrue(cb.has_implementation_plan(104, issue=high_risk_issue, comments=[{"body": high_risk_plan}]))

    def test_unsubstantive_or_placeholder_plan_is_rejected(self):
        issue = {"body": "Feature request."}
        # Mere mention of "implementation plan required" or "TBD"
        comments = [{"body": "implementation plan required before starting"}]
        self.assertFalse(cb.has_implementation_plan(104, issue=issue, comments=comments))
        comments = [{"body": "## Implementation Plan: TBD"}]
        self.assertFalse(cb.has_implementation_plan(104, issue=issue, comments=comments))

    def test_has_implementation_plan_returns_false_when_missing(self):
        issue = {"body": "Feature request without plan."}
        comments = [{"body": "General comment."}]
        self.assertFalse(cb.has_implementation_plan(104, issue=issue, comments=comments))

    def test_fetch_issue_comments_handles_paginated_json_streams(self):
        from common import fetch_issue_comments
        with patch("common.run_cmd", return_value=(0, '[{"id": 1}][{"id": 2}]', "")):
            comments = fetch_issue_comments(104)
            self.assertEqual(len(comments), 2)
            self.assertEqual(comments[0]["id"], 1)
            self.assertEqual(comments[1]["id"], 2)

    @patch("create_branch.require_worktree_admission")
    @patch("create_branch.has_implementation_plan")
    def test_create_branch_refuses_unplanned_feature(self, mock_has_plan, admission):
        admission.return_value = {"title": "feat: new feature", "labels": [{"name": "type:feat"}]}
        mock_has_plan.return_value = False

        with patch("sys.stderr.write") as mock_stderr:
            with self.assertRaises(SystemExit) as ctx:
                cb.create_branch(999, branch_type="feat", agent="agent-1")
            self.assertEqual(ctx.exception.code, 1)
            written = "".join(call.args[0] for call in mock_stderr.call_args_list)
            self.assertIn("[BLOCKED] Plan gate", written)
            self.assertIn("gh issue comment 999", written)

    @patch("create_branch.create_worktree")
    @patch("create_branch.require_worktree_admission")
    @patch("create_branch.has_implementation_plan")
    def test_create_branch_succeeds_for_planned_feature(self, mock_has_plan, admission, mock_worktree):
        admission.return_value = {"title": "feat: planned feature", "labels": [{"name": "type:feat"}]}
        mock_has_plan.return_value = True
        mock_worktree.return_value = ".worktrees/feat-issue-999-planned-feature"

        path = cb.create_branch(999, branch_type="feat", use_worktree=True, agent="agent-1")
        self.assertEqual(path, ".worktrees/feat-issue-999-planned-feature")
        mock_worktree.assert_called_once_with("feat/issue-999-planned-feature", agent="agent-1")

    def test_inline_touches_placeholder_rejected(self):
        plan_with_tbd_touches = (
            "## Implementation Plan\n\n"
            "### Approach\n"
            "We will implement token extraction by decoding JWT payloads.\n\n"
            "Touches: TBD\n\n"
            "### Verification & Test Strategy\n"
            "Run python3 -m unittest tests/test_auth.py."
        )
        gaps = cb.validate_plan_depth(plan_with_tbd_touches, is_risk=False)
        self.assertTrue(any("Files to Touch" in g and "placeholder" in g for g in gaps), f"Expected placeholder error, got: {gaps}")

    def test_untouched_template_rejected_for_placeholder_content(self):
        low_risk_template = cb.format_plan_template(is_risk=False)
        low_gaps = cb.validate_plan_depth(low_risk_template, is_risk=False)
        self.assertTrue(any("placeholder" in g.lower() for g in low_gaps), f"Expected placeholder errors, got: {low_gaps}")

        high_risk_template = cb.format_plan_template(is_risk=True)
        high_gaps = cb.validate_plan_depth(high_risk_template, is_risk=True)
        self.assertTrue(any("placeholder" in g.lower() for g in high_gaps), f"Expected placeholder errors, got: {high_gaps}")

    def test_template_when_substantively_filled_passes_validation(self):
        filled_low_risk = (
            "## Implementation Plan\n\n"
            "### Approach\n"
            "We will implement token extraction by decoding JWT payloads and validating signatures.\n\n"
            "### Files to Touch\n"
            "- scripts/auth.py\n"
            "- tests/test_auth.py\n\n"
            "### Verification & Test Strategy\n"
            "Run python3 -m unittest tests/test_auth.py to verify token extraction and expiry handling."
        )
        self.assertEqual(cb.validate_plan_depth(filled_low_risk, is_risk=False), [])
        self.assertTrue(cb.is_substantive_plan(filled_low_risk, is_risk=False))


if __name__ == "__main__":
    unittest.main()
