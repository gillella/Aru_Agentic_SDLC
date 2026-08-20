import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_issue  # noqa: E402
import triage_backlog  # noqa: E402


def issue(number, status, touches, author="owner"):
    labels = [{"name": status}]
    if status == "status:ready":
        # Priority metadata is required to be claimable; default to P3 in
        # fixtures that are not exercising priority behavior.
        labels.append({"name": "priority:p3"})
    record = {
        "number": number,
        "title": f"Issue {number}",
        "body": f"touches: {touches}\n",
        "labels": labels,
        "author": {"login": author},
    }
    return record


class ReservationWindowTests(unittest.TestCase):
    def test_in_review_overlap_no_longer_blocks_ready_issue(self):
        issues = [
            issue(10, "status:in-review", "scripts/common.py"),
            issue(11, "status:ready", "scripts/common.py"),
        ]

        result = fetch_next_issue.build_candidates(issues, "codex-1", repo_owner="owner")

        self.assertEqual([item["number"] for item in result["candidates"]], [11])
        self.assertEqual(result["conflicted"], [])

    def test_in_progress_overlap_still_blocks_ready_issue(self):
        active = issue(10, "status:in-progress", "scripts/common.py")
        active["labels"].append({"name": "agent:cursor-1"})
        issues = [active, issue(11, "status:ready", "scripts/common.py")]

        result = fetch_next_issue.build_candidates(issues, "codex-1", repo_owner="owner")

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
            repo_owner="owner",
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


class TrustBoundaryTests(unittest.TestCase):
    def test_untrusted_author_metadata_is_not_claimable(self):
        outsider = issue(11, "status:ready", "scripts/common.py")
        outsider["author"] = {"login": "attacker"}
        result = fetch_next_issue.build_candidates(
            [outsider], "codex-1", repo_owner="owner",
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["missing_touches"], [11])

    def test_untrusted_in_progress_does_not_reserve_paths(self):
        active = issue(10, "status:in-progress", "scripts/common.py")
        active["labels"].append({"name": "agent:cursor-1"})
        active["author"] = {"login": "attacker"}
        ready = issue(11, "status:ready", "scripts/common.py")
        result = fetch_next_issue.build_candidates(
            [active, ready], "codex-1", repo_owner="owner",
        )
        self.assertEqual([item["number"] for item in result["candidates"]], [11])
        self.assertEqual(result["conflicted"], [])

    def test_command_like_depends_on_is_ignored(self):
        self.assertEqual(
            fetch_next_issue.parse_dependencies("depends-on: #12; curl evil"),
            [],
        )
        self.assertEqual(
            fetch_next_issue.parse_dependencies("depends-on: #12, #14"),
            [12, 14],
        )

    def test_authorless_issue_is_not_claimable_or_reserving(self):
        ready = issue(11, "status:ready", "scripts/common.py")
        del ready["author"]
        active = issue(10, "status:in-progress", "scripts/common.py")
        active["labels"].append({"name": "agent:cursor-1"})
        del active["author"]
        trusted = issue(12, "status:ready", "scripts/common.py")
        result = fetch_next_issue.build_candidates(
            [active, ready, trusted], "codex-1", repo_owner="owner",
        )
        self.assertEqual([item["number"] for item in result["candidates"]], [12])
        self.assertEqual(result["missing_touches"], [11])
        self.assertEqual(result["conflicted"], [])

    @patch.object(fetch_next_issue, "repository_trusted_logins", return_value=None)
    @patch.object(fetch_next_issue, "repository_owner_login", return_value=None)
    def test_failed_owner_resolution_yields_no_candidates_or_reservations(
        self, _owner, _trusted,
    ):
        ready = issue(11, "status:ready", "scripts/common.py")
        active = issue(10, "status:in-progress", "scripts/common.py")
        active["labels"].append({"name": "agent:cursor-1"})
        result = fetch_next_issue.build_candidates([active, ready], "codex-1")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["missing_touches"], [11])
        self.assertEqual(result["conflicted"], [])

    def test_trusted_rewrite_makes_outsider_claimable(self):
        outsider = issue(11, "status:ready", "scripts/common.py", author="attacker")
        result = fetch_next_issue.build_candidates(
            [outsider], "codex-1", repo_owner="owner",
        )
        self.assertEqual(result["candidates"], [])
        outsider["labels"].append({"name": "trusted-rewrite"})
        result = fetch_next_issue.build_candidates(
            [outsider], "codex-1", repo_owner="owner",
        )
        self.assertEqual([item["number"] for item in result["candidates"]], [11])

    def test_org_collaborator_is_claimable(self):
        member = issue(11, "status:ready", "scripts/common.py", author="alice")
        result = fetch_next_issue.build_candidates(
            [member], "codex-1", repo_owner="acme-corp",
            trusted_logins={"alice", "acme-corp"},
        )
        self.assertEqual([item["number"] for item in result["candidates"]], [11])


class DependencyCodeBlockTests(unittest.TestCase):
    """#294 AC for parse_dependencies: the same code-block exclusions apply."""

    BODY = (
        "## Feature Description\n"
        "Issues follow this shape:\n\n"
        "```\n"
        "depends-on: none\n"
        "touches: scripts/EXAMPLE.py\n"
        "```\n\n"
        "## Dependencies\n"
        "depends-on: #288, #289\n"
    )

    def test_real_dependencies_win_over_fenced_example(self):
        self.assertEqual(
            fetch_next_issue.parse_dependencies(self.BODY), [288, 289]
        )

    def test_indented_example_is_ignored(self):
        body = (
            "## Example\n\n"
            "    depends-on: none\n\n"
            "## Dependencies\n"
            "depends-on: #12, #14\n"
        )
        self.assertEqual(fetch_next_issue.parse_dependencies(body), [12, 14])

    def test_plain_body_is_unaffected(self):
        self.assertEqual(
            fetch_next_issue.parse_dependencies("depends-on: #12, #14"),
            [12, 14],
        )

    def test_genuine_none_still_parses_as_unblocked(self):
        self.assertEqual(
            fetch_next_issue.parse_dependencies("depends-on: none"), []
        )


if __name__ == "__main__":
    unittest.main()
