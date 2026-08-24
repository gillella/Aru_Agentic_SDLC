import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_review_service_capacity as audit  # noqa: E402


AS_OF = "2026-08-24T12:00:00Z"


def service_record(name, *, plan="trial", quota="metered"):
    plan_record = {"kind": plan}
    if plan == "trial":
        plan_record["expires_at"] = "2026-08-31T12:00:00Z"
    quota_record = {"kind": quota}
    if quota == "metered":
        quota_record.update({
            "limit": 100,
            "used": 40,
            "remaining": 60,
            "resets_at": "2026-08-25T12:00:00Z",
        })
    return {
        "service": name,
        "account_state": "active",
        "plan": plan_record,
        "quota": quota_record,
    }


def snapshot():
    return {
        "schema": audit.SNAPSHOT_SCHEMA,
        "observed_at": "2026-08-24T11:30:00Z",
        "configured_services": ["coderabbit", "sourcery", "codeant"],
        "services": [
            service_record("codeant", plan="free", quota="unlimited"),
            service_record("coderabbit", plan="paid", quota="unlimited"),
            service_record("sourcery"),
        ],
    }


def mismatch_codes(report):
    return {item["code"] for item in report["mismatches"]}


class AuditCapacityTests(unittest.TestCase):
    def test_healthy_snapshot_classifies_capacity_in_canonical_order(self):
        report = audit.audit_capacity(snapshot(), as_of=AS_OF, max_age_seconds=3600)

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], audit.AUDIT_SCHEMA)
        self.assertEqual(report["snapshot"]["age_seconds"], 1800)
        self.assertEqual(
            [item["service"] for item in report["services"]],
            ["coderabbit", "sourcery", "codeant"],
        )
        sourcery = report["services"][1]
        self.assertTrue(sourcery["available"])
        self.assertEqual(sourcery["trial_state"], "active")
        self.assertEqual(sourcery["expiry_state"], "future")
        self.assertEqual(sourcery["quota_state"], "available")
        self.assertEqual(sourcery["remaining"], 60)
        self.assertEqual(report["mismatches"], [])

    def test_explicit_unlimited_non_trial_capacity_is_usable(self):
        report = audit.audit_capacity(snapshot(), as_of=AS_OF)

        coderabbit = report["services"][0]
        self.assertTrue(coderabbit["available"])
        self.assertEqual(coderabbit["trial_state"], "not_applicable")
        self.assertEqual(coderabbit["expiry_state"], "not_applicable")
        self.assertEqual(coderabbit["quota_state"], "unlimited")

    def test_missing_duplicate_unknown_or_unconfigured_services_fail_closed(self):
        cases = []
        missing = snapshot()
        missing["services"] = missing["services"][:-1]
        cases.append((missing, "service_missing"))
        duplicate = snapshot()
        duplicate["configured_services"].append("sourcery")
        cases.append((duplicate, "configured_services_invalid"))
        unknown = snapshot()
        unknown["configured_services"][1] = "other"
        cases.append((unknown, "configured_services_invalid"))
        unconfigured = snapshot()
        unconfigured["configured_services"].remove("codeant")
        cases.append((unconfigured, "service_unconfigured"))

        for payload, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_capacity(payload, as_of=AS_OF)
            self.assertFalse(report["ok"])
            self.assertIn(expected, mismatch_codes(report))

    def test_stale_future_and_elapsed_quota_windows_fail_closed(self):
        cases = []
        stale = snapshot()
        stale["observed_at"] = "2026-08-24T10:59:59Z"
        cases.append((stale, "snapshot_stale"))
        future = snapshot()
        future["observed_at"] = "2026-08-24T12:00:01Z"
        cases.append((future, "snapshot_from_future"))
        reset = snapshot()
        reset["services"][2]["quota"]["resets_at"] = AS_OF
        cases.append((reset, "quota_window_elapsed"))

        for payload, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_capacity(
                    payload, as_of=AS_OF, max_age_seconds=3600,
                )
            self.assertFalse(report["ok"])
            self.assertIn(expected, mismatch_codes(report))

    def test_expired_trial_suspended_account_and_exhausted_quota_fail_closed(self):
        cases = []
        expired = snapshot()
        expired["services"][2]["plan"]["expires_at"] = AS_OF
        cases.append((expired, "trial_expired", "expired", "expired"))
        suspended = snapshot()
        suspended["services"][2]["account_state"] = "suspended"
        cases.append((suspended, "account_unavailable", "active", "future"))
        exhausted = snapshot()
        exhausted["services"][2]["quota"].update({"used": 100, "remaining": 0})
        cases.append((exhausted, "quota_exhausted", "active", "future"))

        for payload, expected, trial_state, _expiry in cases:
            with self.subTest(expected=expected):
                report = audit.audit_capacity(payload, as_of=AS_OF)
            self.assertFalse(report["ok"])
            self.assertIn(expected, mismatch_codes(report))
            self.assertEqual(report["services"][1]["trial_state"], trial_state)
            self.assertEqual(report["services"][1]["expiry_state"], _expiry)

    def test_inconsistent_or_malformed_service_fields_fail_closed(self):
        cases = []
        inconsistent = snapshot()
        inconsistent["services"][2]["quota"]["remaining"] = 61
        cases.append((inconsistent, "quota_invalid"))
        boolean_count = snapshot()
        boolean_count["services"][2]["quota"]["used"] = True
        cases.append((boolean_count, "quota_invalid"))
        missing_expiry = snapshot()
        del missing_expiry["services"][2]["plan"]["expires_at"]
        cases.append((missing_expiry, "plan_invalid"))
        unexpected = snapshot()
        unexpected["services"][2]["billing_enabled"] = True
        cases.append((unexpected, "service_record_invalid"))

        for payload, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_capacity(payload, as_of=AS_OF)
            self.assertFalse(report["ok"])
            self.assertIn(expected, mismatch_codes(report))
            self.assertTrue(all(not item["available"] for item in report["services"]))

    def test_top_level_missing_or_malformed_data_fails_closed(self):
        cases = []
        wrong_schema = snapshot()
        wrong_schema["schema"] = "unknown"
        cases.append((wrong_schema, "snapshot_schema_invalid"))
        missing_timestamp = snapshot()
        del missing_timestamp["observed_at"]
        cases.append((missing_timestamp, "snapshot_shape_invalid"))
        naive_timestamp = snapshot()
        naive_timestamp["observed_at"] = "2026-08-24T11:30:00"
        cases.append((naive_timestamp, "observed_at_invalid"))

        for payload, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_capacity(payload, as_of=AS_OF)
            self.assertFalse(report["ok"])
            self.assertIn(expected, mismatch_codes(report))
            self.assertTrue(all(not item["available"] for item in report["services"]))

    def test_non_string_configuration_and_invalid_max_age_return_reports(self):
        malformed = snapshot()
        malformed["configured_services"] = [{"service": "sourcery"}]

        malformed_report = audit.audit_capacity(malformed, as_of=AS_OF)
        age_report = audit.audit_capacity(
            snapshot(), as_of=AS_OF, max_age_seconds="3600",
        )

        self.assertFalse(malformed_report["ok"])
        self.assertIn("configured_services_invalid", mismatch_codes(malformed_report))
        self.assertFalse(age_report["ok"])
        self.assertIn("max_age_invalid", mismatch_codes(age_report))

    def test_strict_json_rejects_duplicates_non_finite_and_non_objects(self):
        invalid = (
            '{"schema":"one","schema":"two"}',
            '{"value":NaN}',
            '[]',
        )

        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    audit.parse_snapshot(raw)

    def test_input_order_does_not_change_report(self):
        first = snapshot()
        second = deepcopy(first)
        second["configured_services"].reverse()
        second["services"].reverse()

        self.assertEqual(
            audit.audit_capacity(first, as_of=AS_OF),
            audit.audit_capacity(second, as_of=AS_OF),
        )


class CapacityCliTests(unittest.TestCase):
    def run_main(self, raw, *, max_age="3600"):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(raw)), redirect_stdout(output):
            code = audit.main([
                "--input", "-",
                "--as-of", AS_OF,
                "--max-age-seconds", max_age,
            ])
        return code, json.loads(output.getvalue())

    def test_stdin_cli_emits_json_and_success_for_usable_capacity(self):
        code, report = self.run_main(json.dumps(snapshot()))

        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["as_of"], AS_OF)

    def test_cli_emits_fail_closed_json_for_invalid_input(self):
        code, report = self.run_main('{"duplicate":1,"duplicate":2}')

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["max_age_seconds"], 3600)
        self.assertIn("input_invalid", mismatch_codes(report))

    def test_cli_emits_fail_closed_json_for_non_integer_max_age(self):
        code, report = self.run_main(json.dumps(snapshot()), max_age="not-a-number")

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("input_invalid", mismatch_codes(report))

    def test_cli_emits_fail_closed_json_for_deeply_nested_input(self):
        raw = "[" * 2000 + "]" * 2000

        code, report = self.run_main(raw)

        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("input_invalid", mismatch_codes(report))

    def test_file_cli_reads_snapshot_without_changing_it(self):
        raw = json.dumps(snapshot())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capacity.json"
            path.write_text(raw, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = audit.main([
                    "--input", str(path),
                    "--as-of", AS_OF,
                    "--max-age-seconds", "3600",
                ])

            self.assertEqual(path.read_text(encoding="utf-8"), raw)

        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
