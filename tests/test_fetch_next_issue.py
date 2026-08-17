import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue  # noqa: E402
import triage_backlog  # noqa: E402


def issue(number, status, touches):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": f"touches: {touches}\n",
        "labels": [{"name": status}],
    }


class ReservationWindowTests(unittest.TestCase):
    def test_in_review_overlap_no_longer_blocks_ready_issue(self):
        issues = [
            issue(10, "status:in-review", "scripts/common.py"),
            issue(11, "status:ready", "scripts/common.py"),
        ]

        result = fetch_next_issue.build_candidates(issues, "codex-1")

        self.assertEqual([item["number"] for item in result["candidates"]], [11])
        self.assertEqual(result["conflicted"], [])

    def test_in_progress_overlap_still_blocks_ready_issue(self):
        active = issue(10, "status:in-progress", "scripts/common.py")
        active["labels"].append({"name": "agent:cursor-1"})
        issues = [active, issue(11, "status:ready", "scripts/common.py")]

        result = fetch_next_issue.build_candidates(issues, "codex-1")

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["conflicted"][0]["number"], 11)

    def test_in_review_pr_file_snapshot_does_not_reacquire_lock(self):
        issues = [
            issue(10, "status:in-review", "scripts/common.py"),
            issue(11, "status:ready", "scripts/common.py"),
        ]

        result = fetch_next_issue.build_candidates(
            issues,
            "codex-1",
            pr_files_by_issue={10: ["scripts/common.py"]},
        )

        self.assertEqual([item["number"] for item in result["candidates"]], [11])
        self.assertEqual(result["conflicted"], [])

    def test_live_stall_shape_releases_all_seven_ready_issues(self):
        held = [
            issue(115, "status:in-review", ".github/workflows/ci.yml"),
            issue(126, "status:in-review", "scripts/merge_pr.py, scripts/common.py"),
            issue(243, "status:in-review", "scripts/doctor_local_agent_integrations.py"),
        ]
        ready = [
            issue(129, "status:ready", "scripts/common.py"),
            issue(146, "status:ready", ".github/workflows/ci.yml"),
            issue(149, "status:ready", ".github/workflows/ci.yml"),
            issue(194, "status:ready", "scripts/doctor_local_agent_integrations.py"),
            issue(242, "status:ready", "scripts/merge_pr.py"),
            issue(262, "status:ready", "scripts/merge_pr.py"),
            issue(263, "status:ready", "prompts/fleet-worker.md"),
        ]

        capacity = triage_backlog.capacity(ready, held)

        self.assertGreater(len(capacity["concurrent"]), 1)
        self.assertEqual(capacity["concurrent"], [129, 146, 194, 242, 263])
        self.assertEqual(
            [number for number, _reason in capacity["deferred"]],
            [149, 262],
        )


if __name__ == "__main__":
    unittest.main()
