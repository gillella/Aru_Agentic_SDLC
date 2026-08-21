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
AUTHORITATIVE_IDS = {
    "ARU-PROJECT-BOOTSTRAP",
    "ARU-INTAKE-PRD",
    "ARU-INCIDENT-INTAKE",
    "ARU-READY-ADMISSION",
    "ARU-BOARD-STATE",
    "ARU-ISSUE-CLAIM",
    "ARU-REVIEW-CLAIM",
    "ARU-MERGE-CLAIM",
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
    "ARU-WORKTREE-JANITOR",
}
ADVISORY_IDS = {
    "ARU-WORK-DISPATCH",
    "ARU-TOUCHES-HOOK",
    "ARU-PRE-PUSH-HOOK",
    "ARU-COMMIT-ATTRIBUTION",
    "ARU-FRAMEWORK-COMPAT",
    "ARU-CI-OBSERVER",
    "ARU-FEEDBACK-OBSERVER",
    "ARU-PRESENCE-STATUS",
    "ARU-FLEET-STATUS",
    "ARU-SLACK-NOTIFICATION",
}
EXPECTED_CLASSES = {
    **{stable_id: "Authoritative" for stable_id in AUTHORITATIVE_IDS},
    **{stable_id: "Advisory" for stable_id in ADVISORY_IDS},
}
EXPECTED_OWNERS = {
    "ARU-PROJECT-BOOTSTRAP": "`scripts/init_project.py`",
    "ARU-INTAKE-PRD": "`scripts/prd_to_issues.py`",
    "ARU-INCIDENT-INTAKE": "`scripts/incident_intake.py`",
    "ARU-READY-ADMISSION": "`scripts/triage_backlog.py`",
    "ARU-BOARD-STATE": "`scripts/update_issue_status.py --require-board`",
    "ARU-ISSUE-CLAIM": "`scripts/claim_issue.py --issue`",
    "ARU-REVIEW-CLAIM": "`scripts/claim_issue.py --pr`",
    "ARU-MERGE-CLAIM": "`scripts/claim_issue.py --merge`",
    "ARU-WORK-DISPATCH": "`scripts/fetch_next_work.py`, `scripts/fetch_next_issue.py`, `scripts/common.py` candidate policy",
    "ARU-WORKTREE-ADMISSION": "`scripts/create_branch.py`, `scripts/common.py` worktree helpers",
    "ARU-PR-ADMISSION": "`scripts/create_pr.py`",
    "ARU-CI-PROTECTION": "`.github/workflows/ci.yml`, `scripts/enable_main_ruleset.py`, GitHub ruleset `aru-protect-main`",
    "ARU-ACCEPTANCE-EXECUTION": "`scripts/acceptance_runner.py` as invoked by `scripts/merge_pr.py`",
    "ARU-REVIEW-ATTESTATION": "`scripts/claim_issue.py --complete-review`, GitHub review, `scripts/merge_pr.py` review evidence",
    "ARU-DOD": "`scripts/merge_pr.py` gate evaluation",
    "ARU-MERGE-EXECUTION": "`scripts/merge_pr.py --expected-head <SHA>` and GitHub merge API",
    "ARU-CLOSEOUT": "`scripts/merge_pr.py` close-out and checkpoint path",
    "ARU-PREVIEW-DEPLOYMENT": "`scripts/deploy_preview.py`, `.github/workflows/deploy-preview.yml`",
    "ARU-PROMOTION": "`scripts/promote.py`, `.github/workflows/promote.yml`",
    "ARU-REVERT": "`scripts/revert_merge.py` plus ordinary PR, review, and merge gates",
    "ARU-INCREMENT-DECISION": "`scripts/slack_control_room.py`, `scripts/slack_projects.py`, `scripts/delivery_increments.py`",
    "ARU-LOOP-CONTROL": "`skills/run-aru-factory/SKILL.md`, `scripts/run_fleet.py`, `scripts/slack_control_room.py` stop store",
    "ARU-INCREMENT-RELEASE": "`scripts/increment_release.py`",
    "ARU-FRAMEWORK-RELEASE": "`scripts/release.py`",
    "ARU-SPEC-SYNC": "`scripts/sync_spec.py` as a DoD gate",
    "ARU-TOUCHES-HOOK": "`hooks/enforce_touches.py`",
    "ARU-PRE-PUSH-HOOK": "`hooks/pre-push`",
    "ARU-COMMIT-ATTRIBUTION": "`hooks/prepare_commit_msg.py`",
    "ARU-FRAMEWORK-COMPAT": "`scripts/common.py` `check_version_compatibility`",
    "ARU-CI-OBSERVER": "`scripts/check_ci.py`",
    "ARU-FEEDBACK-OBSERVER": "`scripts/fetch_pr_feedback.py`",
    "ARU-PRESENCE-STATUS": "`scripts/agent_presence.py`",
    "ARU-FLEET-STATUS": "`scripts/fleet_status.py`",
    "ARU-WORKTREE-JANITOR": "`scripts/cleanup_worktrees.py` deletion predicates",
    "ARU-SLACK-NOTIFICATION": "`scripts/slack_notify.py` and status messages from `scripts/slack_control_room.py`",
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
        self.assertEqual(set(ids), set(EXPECTED_CLASSES))
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

    def test_authority_class_is_fixed_for_every_stable_id(self):
        self.assertEqual(set(AUTHORITATIVE_IDS), set(EXPECTED_OWNERS) - ADVISORY_IDS)
        self.assertEqual(set(ADVISORY_IDS), set(EXPECTED_OWNERS) - AUTHORITATIVE_IDS)
        for stable_id, expected in EXPECTED_CLASSES.items():
            with self.subTest(stable_id=stable_id):
                self.assertEqual(self.by_id[stable_id]["Authority class"], expected)

    def test_authoritative_controls_fail_closed(self):
        for stable_id in AUTHORITATIVE_IDS:
            failure = self.by_id[stable_id]["Failure behavior"].lower()
            with self.subTest(stable_id=stable_id):
                self.assertTrue(
                    failure.startswith("fail closed"),
                    f"authoritative failure contract is not fail-closed: {failure}",
                )

    def test_fail_open_advice_names_its_audit_diagnostic(self):
        normalized = re.sub(r"\s+", " ", self.text)
        self.assertIn("schema `aru.advisory.v1`", normalized)
        self.assertIn("persist that exact event in a durable run or session audit", normalized)
        self.assertIn("retain a locator", normalized)
        for row in self.rows:
            if "fail open" not in row["Failure behavior"].lower():
                continue
            with self.subTest(stable_id=row["Stable ID"]):
                self.assertEqual(row["Authority class"], "Advisory")
                evidence = row["Audit evidence"]
                compliant = evidence.startswith(
                    "COMPLIANT aru.advisory.v1 durable sink:"
                )
                noncompliant = evidence.startswith("NONCOMPLIANT:")
                self.assertNotEqual(compliant, noncompliant)
                self.assertIn("aru.advisory.v1", evidence)
                self.assertRegex(evidence.lower(), r"durable.*(?:audit|locator)|audit locator")

    def test_advisory_controls_cannot_authorize_or_enter_tcb(self):
        authority_rules = re.sub(r"\s+", " ", self.text.lower())
        self.assertIn("cannot authorize a transition", authority_rules)
        self.assertIn("outside the tcb", authority_rules)
        for row in self.rows:
            if row["Authority class"] != "Advisory":
                continue
            combined = " ".join(
                value for key, value in row.items()
                if key not in {"Stable ID", "Authority class"}
            ).lower()
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

    def test_partial_queue_and_worktree_gaps_remain_advisory(self):
        dispatch = self.by_id["ARU-WORK-DISPATCH"]
        self.assertEqual(dispatch["Authority class"], "Advisory")
        self.assertIn("issue-query failure", dispatch["Failure behavior"])
        self.assertIn("empty list", dispatch["Failure behavior"])
        self.assertIn("cannot enter the TCB", dispatch["Recovery path"])

        fleet = self.by_id["ARU-FLEET-STATUS"]
        self.assertEqual(fleet["Authority class"], "Advisory")
        self.assertIn("worktree enumeration failure", fleet["Failure behavior"])
        self.assertIn("incorrectly report complete", fleet["Failure behavior"])
        self.assertIn("cannot enter the TCB", fleet["Recovery path"])

    def test_every_stable_id_has_its_exact_owner_binding(self):
        self.assertEqual(set(EXPECTED_OWNERS), set(EXPECTED_CLASSES))
        for stable_id, expected_owner in EXPECTED_OWNERS.items():
            with self.subTest(stable_id=stable_id):
                self.assertEqual(self.by_id[stable_id]["Owner"], expected_owner)

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
