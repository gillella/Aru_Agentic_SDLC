import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import triage_backlog as tb


def issue(number, *labels, body="", title="t"):
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
    }


READY_BODY = """## Summary

Do the thing.

## Acceptance Criteria

- [ ] it works
- [ ] it is documented

## Verification

`pytest -q` exits 0.

## Dependencies

depends-on:
touches: src/thing.py, tests/test_thing.py
parallel-eligible: true
"""


class SectionParsingTests(unittest.TestCase):
    def test_reads_criteria_under_the_heading(self):
        self.assertEqual(len(tb.acceptance_criteria(READY_BODY)), 2)

    def test_checkboxes_without_the_heading_do_not_count(self):
        # A stray task list is not acceptance criteria; counting it would
        # promote under-specified issues.
        self.assertEqual(tb.acceptance_criteria("## Notes\n\n- [ ] something\n"), [])

    def test_verification_section_detected(self):
        self.assertTrue(tb.has_verification(READY_BODY))

    def test_empty_verification_section_is_not_verification(self):
        self.assertFalse(tb.has_verification("## Verification\n\n## Dependencies\n\nx\n"))


class ReadyContractTests(unittest.TestCase):
    def test_complete_issue_has_no_gaps(self):
        self.assertEqual(tb.ready_gaps(issue(10, "type:chore", body=READY_BODY), set()), [])

    def test_missing_touches_is_reported(self):
        body = READY_BODY.replace("touches: src/thing.py, tests/test_thing.py", "touches:")
        gaps = tb.ready_gaps(issue(10, "type:chore", body=body), set())
        self.assertIn("no touches: declaration", gaps)

    def test_missing_criteria_and_verification_reported_together(self):
        gaps = tb.ready_gaps(issue(10, "type:chore", body="## Summary\n\nx\n"), set())
        self.assertIn("no acceptance criteria checkboxes", gaps)
        self.assertIn("no verification section", gaps)

    def test_open_dependency_blocks(self):
        body = READY_BODY.replace("depends-on:", "depends-on: #5, #6")
        gaps = tb.ready_gaps(issue(10, "type:chore", body=body), {5})
        self.assertTrue(any("depends-on still open" in g and "#5" in g for g in gaps))

    def test_closed_dependency_does_not_block(self):
        body = READY_BODY.replace("depends-on:", "depends-on: #5")
        self.assertEqual(tb.ready_gaps(issue(10, "type:chore", body=body), set()), [])

    def test_epic_is_never_ready(self):
        gaps = tb.ready_gaps(issue(2, "type:epic", body=READY_BODY), set())
        self.assertEqual(len(gaps), 1)
        self.assertIn("epic", gaps[0])


class PartitionTests(unittest.TestCase):
    def test_splits_by_status_and_claim(self):
        issues = [
            issue(1, "status:backlog"),
            issue(2, "status:ready"),
            issue(3, "status:in-progress", "agent:agent-1"),
        ]
        backlog, ready, held = tb.partition(issues)
        self.assertEqual([i["number"] for i in backlog], [1])
        self.assertEqual([i["number"] for i in ready], [2])
        self.assertEqual([i["number"] for i in held], [3])

    def test_a_claimed_ready_issue_counts_as_held_not_ready(self):
        issues = [issue(4, "status:ready", "agent:agent-2")]
        _backlog, ready, held = tb.partition(issues)
        self.assertEqual(ready, [])
        self.assertEqual([i["number"] for i in held], [4])


class CapacityTests(unittest.TestCase):
    def test_non_overlapping_issues_are_all_concurrent(self):
        ready = [
            issue(1, "status:ready", body="touches: src/a.py"),
            issue(2, "status:ready", body="touches: src/b.py"),
        ]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [1, 2])

    def test_overlapping_issues_are_deferred(self):
        ready = [
            issue(1, "status:ready", body="touches: src/a.py"),
            issue(2, "status:ready", body="touches: src/a.py"),
        ]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [1])
        self.assertEqual([n for n, _ in cap["deferred"]], [2])

    def test_conflict_with_in_flight_work_defers(self):
        held = [issue(9, "status:in-progress", "agent:a", body="touches: src/a.py")]
        ready = [issue(1, "status:ready", body="touches: src/a.py")]
        cap = tb.capacity(ready, held)
        self.assertEqual(cap["concurrent"], [])

    def test_issue_without_touches_cannot_be_counted(self):
        ready = [issue(1, "status:ready", body="no metadata")]
        cap = tb.capacity(ready, [])
        self.assertEqual(cap["concurrent"], [])
        self.assertIn("no touches", cap["deferred"][0][1])


if __name__ == "__main__":
    unittest.main()
