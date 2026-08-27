import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_pr
import picker_continuation


class PickerContinuationTests(unittest.TestCase):
    def invoke(self, payload, code=0, error=""):
        output = json.dumps(payload) if not isinstance(payload, str) else payload
        with patch.object(picker_continuation, "run_cmd",
                          return_value=(code, output, error)) as run:
            result = picker_continuation.ensure_ready_after_closeout("/private/tmp")
        return result, run

    def test_promotes_without_claiming_and_reports_the_issue(self):
        (ok, message), run = self.invoke({
            "auto_promoted_issue": 408,
            "work": {"type": "issue", "issue": 408},
        })

        self.assertTrue(ok)
        self.assertIn("#408", message)
        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertIn("--promote-idle", command)
        self.assertIn("--reap-after", command)
        self.assertEqual(command[command.index("--agent") + 1], "post-merge-promoter")
        self.assertNotIn("--claim", command)
        self.assertEqual(run.call_args.kwargs["cwd"], "/private/tmp")

    def test_existing_ready_work_is_a_successful_handoff(self):
        (ok, message), _ = self.invoke({"work": {"type": "issue", "issue": 24}})
        self.assertTrue(ok)
        self.assertIn("#24", message)

    def test_higher_priority_work_does_not_promote_another_issue(self):
        (ok, message), _ = self.invoke({"work": {"type": "merge", "pr": 9}})
        self.assertTrue(ok)
        self.assertIn("merge", message)

    def test_no_qualified_backlog_is_a_clean_noop(self):
        (ok, message), _ = self.invoke({"work": {"type": "idle"}})
        self.assertTrue(ok)
        self.assertIn("No qualified", message)

    def test_picker_error_is_visible_and_retryable(self):
        (ok, message), _ = self.invoke({
            "work": {"type": "error", "reason": "Project inventory unavailable"},
        })
        self.assertFalse(ok)
        self.assertIn("Project inventory unavailable", message)

    def test_process_and_payload_failures_are_not_collapsed_to_idle(self):
        (process_ok, process_message), _ = self.invoke("", code=1, error="rate limited")
        (json_ok, json_message), _ = self.invoke("not-json")
        self.assertFalse(process_ok)
        self.assertIn("rate limited", process_message)
        self.assertFalse(json_ok)
        self.assertIn("malformed", json_message)


class MergeContinuationCallSiteTests(unittest.TestCase):
    @staticmethod
    def parked_gates():
        return {
            "schema_version": 1,
            "pr": 9,
            "gated_head": "gated-sha",
            "gates": [
                {"name": "open", "passed": True, "message": "PR was open at gating time."},
                {"name": "ci", "passed": True, "message": "All required checks passed."},
                {"name": "review", "passed": True, "message": "Exact-head review evidence passed."},
            ],
        }

    @staticmethod
    def merged_pr():
        return {
            "number": 9,
            "title": "merged",
            "body": "Closes #7",
            "state": "MERGED",
            "mergedAt": "2026-08-26T12:00:00Z",
            "headRefOid": "gated-sha",
            "headRefName": "fix/example",
            "mergeCommit": {"oid": "merge-sha"},
            "labels": [],
        }

    def drive(self, closeout=True, dry_run=False):
        argv = ["merge_pr.py", "--pr", "9"] + (["--dry-run"] if dry_run else [])
        output = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output), \
             patch.object(merge_pr, "fetch_pr", return_value=self.merged_pr()), \
             patch.object(merge_pr, "repository_root", return_value="/repo"), \
             patch.object(merge_pr, "load_gate_verdicts",
                          return_value=self.parked_gates()), \
             patch.object(merge_pr, "run_closeout", return_value=closeout), \
             patch.object(merge_pr.time, "sleep"), \
             patch.object(merge_pr, "post_human_intervention", return_value=True), \
             patch.object(merge_pr, "write_checkpoint_tag", return_value=(True, "written")), \
             patch.object(merge_pr, "ensure_ready_after_closeout",
                          return_value=(True, "promoted")) as continuation:
            code = merge_pr.main()
        return code, continuation, output.getvalue()

    def test_completed_closeout_immediately_ensures_ready_work(self):
        code, continuation, output = self.drive()
        self.assertEqual(code, merge_pr.EXIT_OK)
        continuation.assert_called_once_with("/repo", run_cmd_fn=merge_pr.run_cmd)
        self.assertIn("ready handoff", output)

    def test_failed_closeout_never_claims_a_successful_handoff(self):
        code, continuation, _ = self.drive(closeout=False)
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        continuation.assert_not_called()

    def test_dry_run_never_runs_the_mutating_handoff(self):
        code, continuation, _ = self.drive(dry_run=True)
        self.assertEqual(code, merge_pr.EXIT_OK)
        continuation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
