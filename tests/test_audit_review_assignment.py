import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_review_assignment as audit  # noqa: E402


HEAD = "a" * 40


def pr_snapshot(*, body="Closes #384", labels=None, head=HEAD, checks=None):
    return {
        "number": 77,
        "state": "OPEN",
        "isDraft": False,
        "body": body,
        "headRefOid": head,
        "labels": labels if labels is not None else [{"name": "review:codeant"}],
        "statusCheckRollup": checks if checks is not None else [{
            "__typename": "CheckRun",
            "name": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }],
    }


def review_evidence(*, head=HEAD):
    return {
        "head_oid": head,
        "reviews": [{
            "id": "review-1",
            "state": "COMMENTED",
            "submittedAt": "2026-08-24T12:00:00Z",
            "body": "One finding.",
            "author": {"login": "codeant-ai", "__typename": "Bot"},
            "commit": {"oid": head},
        }],
        "unresolved": 1,
        "unfixed": 0,
        "outdated_unfixed": 0,
        "outdated_addressed": 0,
        "body_addressed": 0,
        "withdrawn": 0,
        "service_threads": {
            "coderabbit": {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
            "sourcery": {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
            "codeant": {"unresolved": 1, "unfixed": 0, "outdated_unfixed": 0},
        },
    }


class AuditReviewAssignmentTests(unittest.TestCase):
    def test_read_only_audit_script_exists(self):
        self.assertTrue((ROOT / "scripts" / "audit_review_assignment.py").is_file())

    @patch.object(audit, "review_evidence", side_effect=lambda _pr: review_evidence())
    @patch.object(audit, "fetch_pr", side_effect=lambda _pr: pr_snapshot())
    def test_matching_assignment_reports_exact_head_counts(self, fetch, evidence):
        report = audit.audit_review_assignment(77, expected_head=HEAD)

        self.assertTrue(report["ok"])
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["assignment"], {
            "linked_issues": [{"number": 384, "service": "codeant"}],
            "expected_service": "codeant",
            "review_labels": ["review:codeant"],
            "label_service": "codeant",
            "validated_service": "codeant",
        })
        self.assertEqual(report["heads"], {
            "expected": HEAD,
            "pr": HEAD,
            "review_evidence": HEAD,
        })
        self.assertEqual(report["checks"]["total"], 1)
        self.assertEqual(report["reviews"]["total"], 1)
        self.assertEqual(report["reviews"]["exact_head"], 1)
        self.assertEqual(report["reviews"]["assigned_service_exact_head"], 1)
        self.assertEqual(report["threads"]["assigned_service"]["unresolved"], 1)
        fetch.assert_called_once_with(77)
        evidence.assert_called_once_with(77)

    def test_wrong_or_multiple_review_labels_fail_closed(self):
        cases = (
            ([{"name": "review:sourcery"}], "review_label_mismatch"),
            (
                [{"name": "review:codeant"}, {"name": "review:coderabbit"}],
                "review_label_count",
            ),
        )
        for labels, code in cases:
            with self.subTest(code=code), patch.object(
                audit, "fetch_pr", return_value=pr_snapshot(labels=labels),
            ), patch.object(audit, "review_evidence", return_value=review_evidence()):
                report = audit.audit_review_assignment(77)
            self.assertFalse(report["ok"])
            self.assertIn(code, {item["code"] for item in report["mismatches"]})

    @patch.object(audit, "review_evidence", return_value=review_evidence())
    @patch.object(
        audit,
        "fetch_pr",
        return_value=pr_snapshot(body="Closes #384\nCloses #385"),
    )
    def test_linked_issues_with_different_assignments_fail_closed(self, _fetch, _evidence):
        report = audit.audit_review_assignment(77)

        self.assertFalse(report["ok"])
        self.assertIsNone(report["assignment"]["expected_service"])
        self.assertIn(
            "deterministic_assignment_ambiguous",
            {item["code"] for item in report["mismatches"]},
        )

    def test_missing_or_stale_heads_fail_closed(self):
        cases = (
            (pr_snapshot(head=""), review_evidence(), None, "pr_head_missing"),
            (pr_snapshot(), review_evidence(head="b" * 40), None, "evidence_head_mismatch"),
            (pr_snapshot(), review_evidence(), "b" * 40, "expected_head_mismatch"),
            (pr_snapshot(), review_evidence(), "not-a-sha", "expected_head_invalid"),
        )
        for pr, evidence, expected, code in cases:
            with self.subTest(code=code), patch.object(
                audit, "fetch_pr", return_value=pr,
            ), patch.object(audit, "review_evidence", return_value=evidence):
                report = audit.audit_review_assignment(77, expected_head=expected)
            self.assertFalse(report["ok"])
            self.assertIn(code, {item["code"] for item in report["mismatches"]})

    def test_unavailable_pr_or_review_evidence_fails_closed(self):
        with patch.object(audit, "fetch_pr", return_value=None), patch.object(
            audit, "review_evidence",
        ) as evidence:
            missing_pr = audit.audit_review_assignment(77)
        self.assertEqual(missing_pr["mismatches"][0]["code"], "pr_unavailable")
        evidence.assert_not_called()

        with patch.object(audit, "fetch_pr", return_value=pr_snapshot()), patch.object(
            audit, "review_evidence", return_value=None,
        ):
            missing_evidence = audit.audit_review_assignment(77)
        self.assertIn(
            "review_evidence_unavailable",
            {item["code"] for item in missing_evidence["mismatches"]},
        )

    def test_malformed_checks_reviews_or_thread_counts_fail_closed(self):
        cases = (
            (pr_snapshot(checks=[{"status": "COMPLETED"}]), review_evidence(), "checks_malformed"),
            (pr_snapshot(), dict(review_evidence(), reviews=None), "reviews_malformed"),
            (
                pr_snapshot(),
                dict(review_evidence(), service_threads={"codeant": {"unresolved": -1}}),
                "threads_malformed",
            ),
        )
        for pr, evidence, code in cases:
            with self.subTest(code=code), patch.object(
                audit, "fetch_pr", return_value=pr,
            ), patch.object(audit, "review_evidence", return_value=evidence):
                report = audit.audit_review_assignment(77)
            self.assertFalse(report["ok"])
            self.assertIn(code, {item["code"] for item in report["mismatches"]})

    def test_unassigned_review_service_activity_fails_closed(self):
        evidence = review_evidence()
        evidence["reviews"].append({
            "id": "review-2",
            "state": "COMMENTED",
            "submittedAt": "2026-08-24T12:01:00Z",
            "body": "Unassigned service ran.",
            "author": {"login": "coderabbitai", "__typename": "Bot"},
            "commit": {"oid": HEAD},
        })
        evidence["service_threads"]["sourcery"]["unresolved"] = 1
        checks = [
            {
                "__typename": "CheckRun",
                "name": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "CheckRun",
                "name": "Sourcery review",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
        ]
        with patch.object(
            audit, "fetch_pr", return_value=pr_snapshot(checks=checks),
        ), patch.object(audit, "review_evidence", return_value=evidence):
            report = audit.audit_review_assignment(77)

        codes = {item["code"] for item in report["mismatches"]}
        self.assertFalse(report["ok"])
        self.assertNotIn("unexpected_review_service_check", codes)
        self.assertIn("unexpected_review_service_review", codes)
        self.assertIn("unexpected_review_service_thread", codes)
        self.assertEqual(report["checks"]["by_review_service"]["sourcery"], 1)
        self.assertEqual(report["reviews"]["by_service_total"]["coderabbit"], 1)

    def test_unassigned_service_skip_status_is_reported_without_mismatch(self):
        checks = [{
            "__typename": "StatusContext",
            "context": "CodeRabbit",
            "state": "SUCCESS",
        }]
        with patch.object(
            audit, "fetch_pr", return_value=pr_snapshot(checks=checks),
        ), patch.object(audit, "review_evidence", return_value=review_evidence()):
            report = audit.audit_review_assignment(77)

        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["by_review_service"]["coderabbit"], 1)

    @patch.object(audit, "audit_review_assignment")
    def test_cli_emits_json_and_returns_fail_closed_status(self, run_audit):
        for ok, expected_exit in ((True, 0), (False, 1)):
            with self.subTest(ok=ok):
                run_audit.return_value = {"schema": audit.SCHEMA, "ok": ok}
                output = io.StringIO()
                with redirect_stdout(output):
                    result = audit.main(["--pr", "77", "--expected-head", HEAD])
                self.assertEqual(result, expected_exit)
                self.assertEqual(json.loads(output.getvalue())["ok"], ok)
                run_audit.assert_called_with(77, expected_head=HEAD)
                run_audit.reset_mock()


if __name__ == "__main__":
    unittest.main()
