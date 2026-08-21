import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = ROOT / "docs" / "kernel-enforcement-register.md"
START = "<!-- BEGIN ARU KERNEL ENFORCEMENT REGISTER -->"
END = "<!-- END ARU KERNEL ENFORCEMENT REGISTER -->"
EXPECTED_HEADERS = [
    "Stable ID",
    "Authority class",
    "Owner",
    "Protected transition or invariant",
    "Trusted inputs",
    "Failure behavior",
    "Audit evidence",
    "Operator remedy",
    "Recovery path",
]
REQUIRED_IDS = {
    "ARU-PROJECT-BOOTSTRAP",
    "ARU-INTAKE-PRD",
    "ARU-INCIDENT-INTAKE",
    "ARU-READY-ADMISSION",
    "ARU-BOARD-STATE",
    "ARU-ISSUE-CLAIM",
    "ARU-REVIEW-CLAIM",
    "ARU-MERGE-CLAIM",
    "ARU-WORK-DISPATCH",
    "ARU-WORKTREE-ADMISSION",
    "ARU-PR-ADMISSION",
    "ARU-CI-PROTECTION",
    "ARU-ACCEPTANCE-EXECUTION",
    "ARU-REVIEW-ATTESTATION",
    "ARU-DOD",
    "ARU-MERGE-EXECUTION",
    "ARU-CLOSEOUT",
    "ARU-PREVIEW-DEPLOYMENT",
    "ARU-PROMOTION",
    "ARU-REVERT",
    "ARU-INCREMENT-DECISION",
    "ARU-LOOP-CONTROL",
    "ARU-INCREMENT-RELEASE",
    "ARU-FRAMEWORK-RELEASE",
    "ARU-SPEC-SYNC",
    "ARU-TOUCHES-HOOK",
    "ARU-PRE-PUSH-HOOK",
    "ARU-COMMIT-ATTRIBUTION",
    "ARU-FRAMEWORK-COMPAT",
    "ARU-CI-OBSERVER",
    "ARU-FEEDBACK-OBSERVER",
    "ARU-PRESENCE-STATUS",
    "ARU-FLEET-STATUS",
    "ARU-WORKTREE-JANITOR",
    "ARU-SLACK-NOTIFICATION",
}
PLACEHOLDERS = re.compile(r"\b(?:tbd|todo|placeholder|unknown owner)\b", re.I)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_register() -> tuple[str, list[dict[str, str]]]:
    text = REGISTER_PATH.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        raise AssertionError("register markers must appear exactly once")
    table = text.split(START, 1)[1].split(END, 1)[0]
    lines = [line for line in table.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        raise AssertionError("register table is missing")
    headers = _cells(lines[0])
    if headers != EXPECTED_HEADERS:
        raise AssertionError(f"unexpected register headers: {headers}")
    if not all(set(cell) <= {"-", ":"} for cell in _cells(lines[1])):
        raise AssertionError("register separator row is malformed")
    rows = []
    for line in lines[2:]:
        values = _cells(line)
        if len(values) != len(headers):
            raise AssertionError(f"register row has {len(values)} cells: {line}")
        rows.append(dict(zip(headers, values)))
    return text, rows


class KernelEnforcementRegisterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text, cls.rows = load_register()
        cls.by_id = {row["Stable ID"]: row for row in cls.rows}

    def test_register_has_complete_unique_stable_id_inventory(self):
        ids = [row["Stable ID"] for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "stable IDs must be unique")
        self.assertEqual(set(ids), REQUIRED_IDS)
        for stable_id in ids:
            self.assertRegex(stable_id, r"^ARU-[A-Z0-9]+(?:-[A-Z0-9]+)*$")

    def test_every_row_has_the_durable_contract_fields(self):
        for row in self.rows:
            with self.subTest(stable_id=row["Stable ID"]):
                for header in EXPECTED_HEADERS:
                    self.assertTrue(row[header], f"{header} is empty")
                    self.assertIsNone(PLACEHOLDERS.search(row[header]))
                self.assertIn(
                    row["Authority class"], {"Authoritative", "Advisory"}
                )

    def test_authoritative_controls_fail_closed(self):
        authoritative = [
            row for row in self.rows
            if row["Authority class"] == "Authoritative"
        ]
        self.assertGreater(len(authoritative), 15)
        for row in authoritative:
            with self.subTest(stable_id=row["Stable ID"]):
                self.assertIn("fail closed", row["Failure behavior"].lower())

    def test_fail_open_advice_names_its_audit_diagnostic(self):
        for row in self.rows:
            if "fail open" not in row["Failure behavior"].lower():
                continue
            with self.subTest(stable_id=row["Stable ID"]):
                self.assertEqual(row["Authority class"], "Advisory")
                self.assertRegex(
                    row["Audit evidence"].lower(),
                    r"diagnostic|report|result|record|stderr|exit|json|metadata",
                )

    def test_advisory_controls_cannot_authorize_or_enter_tcb(self):
        authority_rules = re.sub(r"\s+", " ", self.text.lower())
        self.assertIn("cannot authorize a transition", authority_rules)
        self.assertIn("outside the tcb", authority_rules)
        for row in self.rows:
            if row["Authority class"] != "Advisory":
                continue
            combined = " ".join(row.values()).lower()
            self.assertRegex(combined, r"cannot|never|outside|advisory")

    def test_touches_hook_is_explicitly_fail_open_and_advisory(self):
        row = self.by_id["ARU-TOUCHES-HOOK"]
        self.assertEqual(row["Authority class"], "Advisory")
        self.assertIn("hooks/enforce_touches.py", row["Owner"])
        self.assertIn("fail open", row["Failure behavior"].lower())
        self.assertIn("[aru]", row["Audit evidence"])
        self.assertIn("cannot enter the TCB", row["Recovery path"])

    def test_framework_compatibility_is_warn_only_and_advisory(self):
        row = self.by_id["ARU-FRAMEWORK-COMPAT"]
        self.assertEqual(row["Authority class"], "Advisory")
        self.assertIn("scripts/common.py", row["Owner"])
        self.assertIn("warn only", row["Failure behavior"].lower())
        self.assertIn("always returns success", row["Failure behavior"])
        self.assertIn("[WARN] Framework version mismatch", row["Audit evidence"])

    def test_required_lifecycle_surfaces_are_owned(self):
        required_owner_fragments = {
            "ARU-BOARD-STATE": "update_issue_status.py --require-board",
            "ARU-WORK-DISPATCH": "fetch_next_work.py",
            "ARU-REVIEW-ATTESTATION": "claim_issue.py --complete-review",
            "ARU-DOD": "merge_pr.py",
            "ARU-MERGE-EXECUTION": "merge_pr.py --expected-head",
            "ARU-PROMOTION": ".github/workflows/promote.yml",
            "ARU-CLOSEOUT": "merge_pr.py",
            "ARU-INCREMENT-DECISION": "slack_projects.py",
            "ARU-LOOP-CONTROL": "run_fleet.py",
        }
        for stable_id, fragment in required_owner_fragments.items():
            with self.subTest(stable_id=stable_id):
                self.assertIn(fragment, self.by_id[stable_id]["Owner"])

    def test_phase_one_adr_must_consume_registered_authority_only(self):
        lower = re.sub(r"\s+", " ", self.text.lower())
        self.assertIn("phase 1 kernel architecture decision record must cite", lower)
        self.assertIn("list the stable ids", lower)
        self.assertIn("only rows classified", lower)
        self.assertIn("an unregistered mechanism must not be described", lower)
        self.assertIn("tests/test_kernel_enforcement_register.py", self.text)
        self.assertIn("capability-admission validator", lower)

    def test_documented_source_paths_exist(self):
        paths = set(re.findall(r"`((?:scripts|hooks|\.github)/[^` ]+)`", self.text))
        self.assertGreater(len(paths), 15)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f"missing registered owner: {path}")


if __name__ == "__main__":
    unittest.main()
