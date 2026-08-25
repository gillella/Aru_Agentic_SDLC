# line-ceiling: 490
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

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr
import reassign_review
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
        "head_commit_committed_at": "2026-08-18T23:59:00Z",
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


def reassigned_pr(service: str, **overrides) -> dict:
    """The canonical PR after an operator moved it off CodeRabbit (#435).

    Only the authority label and the provider's own check surface change: every
    other field stays byte-identical to the canonical fixture, so a gate that
    starts passing or failing here did so because of the reassignment.
    """
    pr = canonical_pr()
    pr["labels"] = [lab for lab in pr["labels"]
                    if lab["name"] != "review:coderabbit"]
    pr["labels"].append({"name": f"review:{service}"})
    pr["statusCheckRollup"] = [pr["statusCheckRollup"][0]]
    if service == "sourcery":
        pr["statusCheckRollup"].append({
            "__typename": "CheckRun", "name": "Sourcery review",
            "status": "COMPLETED", "conclusion": "SUCCESS",
        })
    pr.update(overrides)
    return pr


def reassigned_evidence(service: str, **overrides) -> dict:
    """Exact-head evidence in the shape the named service actually produces.

    Neither fallback leaves a CodeRabbit review object or status context
    behind, so both are dropped: the gate has to pass on the incoming
    provider's own proof or not at all.
    """
    head = canonical_pr()["headRefOid"]
    base = canonical_pr()["baseRefOid"]
    evidence = canonical_evidence(reviews=[])
    evidence.pop("coderabbit_status")
    evidence["service_threads"] = {
        service: {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
    }
    if service == "sourcery":
        evidence["sourcery_check_runs"] = [{
            "name": "Sourcery review", "app": {"slug": "sourcery-ai"},
            "head_sha": head, "status": "completed", "conclusion": "success",
            "pull_requests": [{"number": 293, "head": {"sha": head},
                               "base": {"sha": base}}],
        }]
    else:
        record = {"label": "CodeAnt review", "commit": head,
                  "started": "2026-08-19T00:00:00Z",
                  "finished": "2026-08-19T00:02:00Z", "done": True}
        evidence["codeant_status_comments"] = [{
            "author": {"login": "codeant-ai", "__typename": "Bot"},
            "body": (merge_pr.CODEANT_STATUS_MARKER_PREFIX
                     + json.dumps([record]) + "-->"),
        }]
    evidence.update(overrides)
    return evidence


FALLBACK_SERVICES = ("sourcery", "codeant")


def agent_review_pr(**overrides) -> dict:
    pr = reassigned_pr("agent")
    pr["labels"].extend([
        {"name": "reviewed-by:agent-2"},
        {"name": "reviewer-family:agent-2:openai"},
    ])
    pr.update(overrides)
    return pr


def agent_review_evidence(**overrides) -> dict:
    head = canonical_pr()["headRefOid"]
    evidence = canonical_evidence(reviews=[{
        "id": "agent-review-293", "state": "COMMENTED",
        "body": "No findings after exact-head inspection and focused tests.",
        "author": {"login": "gillella", "__typename": "User"},
        "submittedAt": "2026-08-19T00:02:00Z", "commit": {"oid": head},
    }])
    evidence.pop("coderabbit_status")
    evidence["service_threads"] = {
        "agent": {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
    }
    evidence["agent_review_attestations"] = [{
        "agent": "agent-2", "completed_at": "2026-08-19T00:03:00Z",
        "disposition": "no-findings", "family": "openai", "head": head,
        "status": "completed", "github_login": "gillella",
    }]
    evidence["agent_review_marker_errors"] = 0
    evidence["agent_review_assignments"] = [{
        "family": "openai", "from": "review:codeant", "head": head,
        "reason": "External reviewers busy", "reviewer": "agent-2",
        "assigned_at": "2026-08-19T00:01:00Z", "github_login": "gillella",
    }]
    evidence["agent_review_assignment_errors"] = 0
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


class FallbackReviewTraversalTests(unittest.TestCase):
    """#435: a reassigned PR must still be traversable, and no easier.

    The reassignment path exists to unstick a PR CodeRabbit cannot review. It
    would be worth nothing if the fallback route reached merge by relaxing a
    gate, so each service is driven through the same full conjunction as the
    canonical fixture and then shown to still fail the review gate on its own
    missing evidence.
    """

    def _dod(self, pr, evidence):
        with patch.object(merge_pr, "check_spec_sync") as spec_sync, \
             patch.object(merge_pr, "_behind_by", return_value=0):
            spec_sync.return_value = (True, "specifications synchronized.")
            return merge_pr.evaluate_dod(
                pr, {293: CANONICAL_ISSUE_BODY}, evidence)

    def test_every_reassignment_target_is_traversable(self):
        for service in FALLBACK_SERVICES:
            with self.subTest(service=service):
                ok, gates = self._dod(reassigned_pr(service),
                                      reassigned_evidence(service))
                self.assertTrue(
                    ok, [f"{n}: {m}" for n, p, m in gates if not p])
                self.assertEqual({name for name, _, _ in gates},
                                 EXPECTED_GATES | {"accept #293"})

    def test_reassignment_helper_targets_exactly_the_traversed_services(self):
        """A target nobody proved traversable is a PR the operator can strand."""
        self.assertEqual(set(reassign_review.FALLBACK_LABELS),
                         set(FALLBACK_SERVICES) | {"agent"})

    def test_fallback_without_its_own_evidence_blocks_on_review_alone(self):
        """The reassignment moves authority; it never satisfies authority."""
        for service in FALLBACK_SERVICES:
            with self.subTest(service=service):
                evidence = reassigned_evidence(service)
                evidence.pop("sourcery_check_runs", None)
                evidence["codeant_status_comments"] = []
                ok, gates = self._dod(reassigned_pr(service), evidence)
                self.assertFalse(ok)
                self.assertEqual(
                    [name for name, passed, _ in gates if not passed],
                    ["review"])

    def test_coding_agent_review_is_not_a_traversal_for_a_fallback_either(self):
        for service in FALLBACK_SERVICES:
            with self.subTest(service=service):
                evidence = reassigned_evidence(service, reviews=[{
                    "id": "agent-review-293",
                    "state": "APPROVED",
                    "body": "Coding-agent approval is not positive authority.",
                    "author": {"login": "agent-1", "__typename": "User"},
                    "submittedAt": "2026-08-19T00:00:00Z",
                    "commit": {"oid": canonical_pr()["headRefOid"]},
                }])
                evidence.pop("sourcery_check_runs", None)
                evidence["codeant_status_comments"] = []
                self.assertFalse(
                    merge_pr.check_reviews(reassigned_pr(service), evidence)[0])

    def test_stale_fallback_evidence_does_not_survive_a_push(self):
        """Exact-head binding is the whole contract; a new head resets it."""
        pushed = "3" * 40
        for service in FALLBACK_SERVICES:
            with self.subTest(service=service):
                pr = reassigned_pr(service, headRefOid=pushed)
                evidence = reassigned_evidence(service, head_oid=pushed)
                self.assertFalse(merge_pr.check_reviews(pr, evidence)[0])

    def test_two_authority_labels_are_never_traversable(self):
        """Reassignment is a swap, not an accumulation."""
        pr = reassigned_pr("sourcery")
        pr["labels"].append({"name": "review:codeant"})
        self.assertIsNone(merge_pr.assigned_review_service(pr))
        self.assertFalse(
            merge_pr.check_reviews(pr, reassigned_evidence("sourcery"))[0])


class EmergencyAgentReviewTraversalTests(FallbackReviewTraversalTests):
    """The explicit last-resort agent path preserves the full DoD conjunction."""

    def test_exact_head_independent_agent_review_is_traversable(self):
        ok, gates = self._dod(agent_review_pr(), agent_review_evidence())
        self.assertTrue(ok, [f"{n}: {m}" for n, passed, m in gates if not passed])

    def test_agent_finding_blocks_until_new_head_and_fresh_review(self):
        evidence = agent_review_evidence()
        evidence["unresolved"] = 1
        evidence["service_threads"]["agent"]["unresolved"] = 1
        self.assertFalse(merge_pr.check_reviews(agent_review_pr(), evidence)[0])
        pushed = "3" * 40
        stale = agent_review_evidence(head_oid=pushed)
        self.assertFalse(
            merge_pr.check_reviews(agent_review_pr(headRefOid=pushed), stale)[0])

    def test_self_review_and_duplicate_completion_fail_closed(self):
        self_review = agent_review_pr()
        for label in self_review["labels"]:
            if label["name"] == "reviewed-by:agent-2":
                label["name"] = "reviewed-by:agent-1"
            if label["name"] == "reviewer-family:agent-2:openai":
                label["name"] = "reviewer-family:agent-1:openai"
        self.assertFalse(
            merge_pr.check_reviews(self_review, agent_review_evidence())[0])
        evidence = agent_review_evidence()
        evidence["agent_review_attestations"] *= 2
        self.assertFalse(merge_pr.check_reviews(agent_review_pr(), evidence)[0])


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
