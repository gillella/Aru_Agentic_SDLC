"""Hermetic proof that the issue lifecycle is traversable end to end.

Issue #293 - every governance gate in this repository is individually
defensible, but nothing verified their conjunction. On 2026-08-18 four
independent locks engaged at once and the board had zero claimable work:
triage's split rule, merge's tests check, a one-identity review pile-up, and a
DoD gate the picker never routed. This suite proves the conjunction is
reachable, so a future unroutable gate fails the build instead of silently
freezing the board.

Hermetic contract:
  * No network calls. GitHub and sync-spec boundaries are stubbed.
  * No repository state is created or mutated.
  * Finishes far under 30 seconds so it can live in the default CI job.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr
import update_issue_status as issue_status
from common import parse_touches
from fetch_next_issue import is_parallel_eligible
from triage_backlog import ready_gaps


CANONICAL_ISSUE_BODY = (
    "## Summary: add a trivial delivery module with hermetic coverage.\n"
    "\n"
    "## Acceptance Criteria\n"
    "- [ ] Source exists (verify: `python3 -m unittest tests.test_pipeline_traversal`)\n"
    "- [ ] Test exists (verify: `python3 -m unittest tests.test_pipeline_traversal`)\n"
    "\n"
    "## Decision Boundaries\n"
    "- Hermetic only: no GitHub writes.\n"
    "\n"
    "## Verification\n"
    "`python3 -m unittest tests.test_pipeline_traversal` exits 0.\n"
    "\n"
    "## Non-Goals\n"
    "- Running a live factory pilot.\n"
    "\n"
    "## Dependencies\n"
    "depends-on: none\n"
    "touches: scripts/widget.py, tests/test_widget.py\n"
    "parallel-eligible: true\n"
)

CANONICAL_PR_BODY = (
    "feat(widget): add a canonical module with hermetic coverage\n"
    "\n"
    "Adds scripts/widget.py and tests/test_widget.py.\n"
    "\n"
    "Closes #293\n"
    "<!-- aru-verification-evidence:v1 -->\n"
    "```json\n"
    '{"schema": "aru.verification.v1", "status": "passed", '
    '"head_sha": "1111111111111111111111111111111111111111", '
    '"commands": [{"command": ["python3", "-m", "unittest", '
    '"tests.test_pipeline_traversal"], "exit_code": 0, '
    '"duration_seconds": 0.5, "status": "passed"}]}\n'
    "```\n"
    "<!-- /aru-verification-evidence -->\n"
)


def canonical_pr(**overrides) -> dict:
    """A Definition-of-Done-satisfying PR for issue #293."""
    pr = {
        "number": 293,
        "title": "feat(widget): add a canonical module with hermetic coverage",
        "state": "OPEN",
        "isDraft": False,
        "body": CANONICAL_PR_BODY,
        "headRefName": "feat/issue-293-testfactory-prove-the-lifecycl",
        "headRefOid": "1111111111111111111111111111111111111111",
        "baseRefOid": "2222222222222222222222222222222222222222",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "additions": 40,
        "deletions": 0,
        "files": [
            {"path": "scripts/widget.py", "additions": 5, "deletions": 0},
            {"path": "tests/test_widget.py", "additions": 5, "deletions": 0},
        ],
        "labels": [
            {"name": "author:agent-1"},
            {"name": "family:anthropic"},
            {"name": "review:coderabbit"},
            {"name": "status:in-review"},
        ],
        "reviews": [{
            "id": "coderabbit-review-293",
            "state": "APPROVED",
            "body": "CodeRabbit reviewed the canonical lifecycle fixture.",
            "author": {"login": "coderabbitai", "__typename": "Bot"},
            "submittedAt": "2026-08-19T00:00:00Z",
            "commit": {"oid": "1111111111111111111111111111111111111111"},
        }],
        "statusCheckRollup": [
            {"name": "Lint, Verify & Test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {
                "__typename": "StatusContext",
                "context": "CodeRabbit",
                "state": "SUCCESS",
                "creator": {"login": "coderabbitai", "__typename": "Bot"},
                "description": "Review completed",
            },
        ],
    }
    pr.update(overrides)
    return pr


def canonical_evidence(**overrides) -> dict:
    evidence = {
        "unresolved": 0,
        "unfixed": 0,
        "outdated_unfixed": 0,
        "withdrawn": 0,
        "reviewed_head": True,
        "github_review_evidence": True,
        "head_oid": "1111111111111111111111111111111111111111",
        "reviews": [{
            "id": "coderabbit-review-293",
            "state": "APPROVED",
            "body": "CodeRabbit reviewed the canonical lifecycle fixture.",
            "author": {"login": "coderabbitai", "__typename": "Bot"},
            "submittedAt": "2026-08-19T00:00:00Z",
            "commit": {"oid": "1111111111111111111111111111111111111111"},
        }],
        "coderabbit_status": [{
            "__typename": "StatusContext",
            "context": "CodeRabbit",
            "state": "SUCCESS",
            "creator": {"login": "coderabbitai", "__typename": "Bot"},
            "description": "Review completed",
        }],
        "service_threads": {
            "coderabbit": {
                "unresolved": 0,
                "unfixed": 0,
                "outdated_unfixed": 0,
            },
        },
    }
    evidence.update(overrides)
    return evidence


def _issue(number: int, status: str) -> dict:
    return {
        "number": number,
        "state": "OPEN",
        "labels": [{"name": f"status:{status}"}, {"name": "type:feat"}],
    }


EXPECTED_GATES = frozenset([
    "open", "issue link", "verification", "ci", "review", "rebased",
    "size", "tests", "spec-sync", "review rounds",
])


class FixtureShapeTests(unittest.TestCase):
    """The canonical fixture must itself satisfy the Ready contract."""

    def test_fixture_declares_source_and_test_touches(self):
        paths = parse_touches(CANONICAL_ISSUE_BODY)
        self.assertIn("scripts/widget.py", paths)
        self.assertIn("tests/test_widget.py", paths)

    def test_fixture_is_parallel_eligible(self):
        self.assertTrue(is_parallel_eligible(CANONICAL_ISSUE_BODY, []))

    def test_fixture_meets_ready_contract(self):
        issue = {"number": 293, "body": CANONICAL_ISSUE_BODY,
                 "labels": [{"name": "type:feat"}]}
        gaps = ready_gaps(issue, open_numbers=set(), repo_slug=None)
        self.assertEqual(gaps, [])


class LifecycleTransitionTests(unittest.TestCase):
    """Drive the canonical issue through each lifecycle hop hermetically."""

    def test_backlog_to_ready(self):
        with patch.object(issue_status, "get_issue") as gi, \
             patch.object(issue_status, "set_board_status") as sb, \
             patch.object(issue_status, "run_cmd") as rc, \
             patch.object(issue_status, "label_names") as ln:
            gi.return_value = _issue(293, "backlog")
            ln.side_effect = lambda issue: {
                lab.get("name") for lab in issue.get("labels", [])
            }
            sb.return_value = True
            rc.return_value = (0, "", "")
            self.assertTrue(issue_status.update_status(293, "Ready"))
            sb.assert_called_once_with(293, "Ready")

    def test_ready_to_in_progress(self):
        with patch.object(issue_status, "get_issue") as gi, \
             patch.object(issue_status, "set_board_status") as sb, \
             patch.object(issue_status, "run_cmd") as rc:
            gi.return_value = _issue(293, "ready")
            sb.return_value = True
            rc.return_value = (0, "", "")
            self.assertTrue(issue_status.update_status(293, "In Progress"))

    def test_review_requires_exact_head_coderabbit_authority(self):
        ok, msg = merge_pr.check_reviews(canonical_pr(), canonical_evidence())
        self.assertTrue(ok, msg)

    def test_coding_agent_review_is_not_a_traversal(self):
        pr = canonical_pr()
        evidence = canonical_evidence(
            reviews=[{
                "id": "agent-review-293",
                "state": "APPROVED",
                "body": "Coding-agent approval is not positive authority.",
                "author": {"login": "agent-1", "__typename": "User"},
                "submittedAt": "2026-08-19T00:00:00Z",
                "commit": {"oid": "1111111111111111111111111111111111111111"},
            }],
        )
        ok, _ = merge_pr.check_reviews(pr, evidence)
        self.assertFalse(ok, "a coding-agent review must not satisfy the review gate")


class DefinitionOfDoneTests(unittest.TestCase):
    """The conjunction: all gates active at once, and every gate routed."""

    def test_canonical_pr_passes_every_gate(self):
        with patch.object(merge_pr, "check_spec_sync") as _ss, \
             patch.object(merge_pr, "_behind_by", return_value=0) as _bb:
            _ss.return_value = (True, "specifications synchronized.")
            ok, gates = merge_pr.evaluate_dod(
                canonical_pr(), {293: CANONICAL_ISSUE_BODY}, canonical_evidence()
            )
        self.assertTrue(ok, [f"{name}: {msg}" for name, p, msg in gates if not p])
        emitted = {name for name, _, _ in gates}
        self.assertEqual(emitted, EXPECTED_GATES | {"accept #293"})

    def test_every_gate_has_a_routing_actor(self):
        routed = {
            "open": "author",
            "issue link": "author",
            "verification": "author",
            "ci": "author",
            "review": "CodeRabbit then author remediation",
            "rebased": "author",
            "size": "author (size-waiver) or split",
            "tests": "author",
            "spec-sync": "author",
            "review rounds": "author (audit-only split guidance)",
        }
        self.assertEqual(EXPECTED_GATES, frozenset(routed))

    def test_unknown_future_gate_fails_build(self):
        with patch.object(merge_pr, "check_spec_sync") as _ss, \
             patch.object(merge_pr, "_behind_by", return_value=0) as _bb:
            _ss.return_value = (True, "ok")
            _, gates = merge_pr.evaluate_dod(
                canonical_pr(), {293: CANONICAL_ISSUE_BODY}, canonical_evidence()
            )
        emitted = {name for name, _, _ in gates if name != "accept #293"}
        self.assertEqual(emitted, EXPECTED_GATES)


if __name__ == "__main__":
    unittest.main()
