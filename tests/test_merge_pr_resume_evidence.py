# line-ceiling: 400
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import merge_pr  # noqa: E402


def merged_pr():
    return {
        "number": 491,
        "title": "merged",
        "body": "Closes #7",
        "state": "MERGED",
        "mergedAt": "2026-08-26T00:00:00Z",
        "mergeCommit": {"oid": "merge-sha"},
        "headRefName": "fix/issue-7-example",
        "headRefOid": "gated-sha",
        "baseRefOid": "base-sha",
        "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
        "headRepositoryOwner": {"login": "owner"},
    }


def open_pr():
    pr = merged_pr()
    pr["state"] = "OPEN"
    pr["mergedAt"] = None
    pr["title"] = "open"
    pr.pop("mergeCommit")
    return pr


PASSING_GATES = [
    ("open", True, "PR is open."),
    ("ci", True, "CI green (1 job)."),
    ("review", True, "Exact-head review is present."),
]


class ResumeGateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        patcher = patch.object(merge_pr, "run_cmd", return_value=(0, self.tempdir.name, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, *, pr_num=491, gated_head="gated-sha", gates=PASSING_GATES):
        return merge_pr.save_gate_verdicts(
            self.tempdir.name, pr_num, gates, gated_head=gated_head
        )

    def resume(self):
        with patch.object(sys, "argv", ["merge_pr.py", "--pr", "491"]), \
             patch.object(merge_pr, "fetch_pr", return_value=merged_pr()), \
             patch.object(merge_pr, "repository_root", return_value=self.tempdir.name), \
             patch.object(merge_pr, "run_closeout_with_retries", return_value=(True, [])) as closeout, \
             patch.object(merge_pr, "write_checkpoint_tag",
                          return_value=(True, "written")) as tag, \
             patch.object(merge_pr, "ensure_ready_after_closeout",
                          return_value=(True, "ready")) as ready, \
             patch.object(merge_pr, "evaluate_dod") as evaluate, \
             patch.object(merge_pr, "execute_merge") as execute:
            code = merge_pr.main()
        return code, closeout, tag, ready, evaluate, execute

    def test_resume_after_server_merge_uses_saved_exact_head_evidence(self):
        self.assertTrue(self.save())
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_OK)
        closeout.assert_called_once()
        ready.assert_called_once()
        tag.assert_called_once()
        self.assertEqual(tag.call_args.args[3], PASSING_GATES)
        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_resume_fails_closed_when_evidence_is_absent(self):
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        closeout.assert_not_called()
        tag.assert_not_called()
        ready.assert_not_called()
        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_resume_fails_closed_when_record_targets_a_different_pr(self):
        self.assertTrue(self.save(pr_num=490))
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        closeout.assert_not_called()
        tag.assert_not_called()
        ready.assert_not_called()
        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_resume_fails_closed_when_record_targets_a_different_head(self):
        self.assertTrue(self.save(gated_head="other-head"))
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        closeout.assert_not_called()
        tag.assert_not_called()
        ready.assert_not_called()
        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_resume_fails_closed_when_any_saved_gate_failed(self):
        self.assertTrue(self.save(gates=[("ci", False, "CI is red.")]))
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        closeout.assert_not_called()
        tag.assert_not_called()
        ready.assert_not_called()
        evaluate.assert_not_called()
        execute.assert_not_called()

    def test_resume_fails_closed_when_record_is_malformed(self):
        path = Path(merge_pr.gate_verdict_path(self.tempdir.name, 491))
        path.write_text('{"schema_version": 0, "pr": 491}', encoding="utf-8")
        code, closeout, tag, ready, evaluate, execute = self.resume()
        self.assertEqual(code, merge_pr.EXIT_ERROR)
        closeout.assert_not_called()
        tag.assert_not_called()
        ready.assert_not_called()
        evaluate.assert_not_called()
        execute.assert_not_called()


class FinalGatePersistenceTests(unittest.TestCase):
    @patch.object(merge_pr, "_behind_by", new=lambda base, head: 0)
    def test_atomic_evidence_write_failure_blocks_before_execute_merge(self):
        with tempfile.TemporaryDirectory() as repo:
            with patch.object(sys, "argv", ["merge_pr.py", "--pr", "491"]), \
                 patch.object(merge_pr, "fetch_pr", side_effect=[open_pr(), open_pr()]), \
                 patch.object(merge_pr, "_gh_json", return_value={"body": ""}), \
                 patch.object(merge_pr, "review_evidence",
                              return_value={"head_oid": "gated-sha"}), \
                 patch.object(merge_pr, "evaluate_dod",
                              return_value=(True, list(PASSING_GATES))), \
                 patch.object(merge_pr, "_current_base_tip", return_value="base-sha"), \
                 patch.object(merge_pr, "check_rebased", return_value=(True, "fresh")), \
                 patch.object(merge_pr, "repository_merge_lock",
                              return_value=nullcontext((True, "serialized"))), \
                 patch.object(merge_pr, "repository_root", return_value=repo), \
                 patch.object(merge_pr, "save_gate_verdicts", return_value=False), \
                 patch.object(merge_pr, "execute_merge") as execute_merge:
                code = merge_pr.main()
        self.assertEqual(code, merge_pr.EXIT_BLOCKED)
        execute_merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
