# line-ceiling: 680
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
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


def unavailability_entry(service="coderabbit", state="cooldown", *,
                          observed_at="2026-08-24T11:30:00Z",
                          retry_at="2026-08-24T13:00:00Z",
                          reason="rate limited", source="review-status-webhook"):
    return {
        "service": service, "state": state, "reason": reason,
        "observed_at": observed_at, "retry_at": retry_at, "source": source,
    }


def ledger(*entries):
    return {"schema": audit.UNAVAILABILITY_SCHEMA, "entries": list(entries)}


class UnavailabilityAuditTests(unittest.TestCase):
    """audit_unavailability() is the fail-open twin of audit_capacity(): only
    fresh, well-formed, unexpired evidence excludes a service; everything
    else is ignored, never treated as proof of unavailability."""

    def test_each_unavailable_state_excludes_the_service(self):
        for state in audit.UNAVAILABLE_STATES:
            with self.subTest(state=state):
                report = audit.audit_unavailability(
                    ledger(unavailability_entry(state=state)), as_of=AS_OF,
                )
                self.assertEqual(set(report["unavailable"]), {"coderabbit"})
                self.assertEqual(report["unavailable"]["coderabbit"]["state"], state)
                self.assertEqual(report["ignored"], [])

    def test_stale_entry_beyond_max_age_is_ignored_not_excluded(self):
        old = unavailability_entry(observed_at="2026-08-24T10:00:00Z", retry_at="2026-08-24T15:00:00Z")
        report = audit.audit_unavailability(ledger(old), as_of=AS_OF, max_age_seconds=3600)
        self.assertEqual(report["unavailable"], {})
        self.assertIn("entry_stale", {item["code"] for item in report["ignored"]})

    def test_expired_retry_at_is_ignored_not_excluded(self):
        expired = unavailability_entry(observed_at="2026-08-24T11:00:00Z", retry_at="2026-08-24T11:30:00Z")
        report = audit.audit_unavailability(ledger(expired), as_of=AS_OF)
        self.assertEqual(report["unavailable"], {})
        self.assertIn("entry_expired", {item["code"] for item in report["ignored"]})

    def test_future_observed_at_is_ignored(self):
        future = unavailability_entry(observed_at="2026-08-24T12:00:01Z", retry_at="2026-08-24T13:00:00Z")
        report = audit.audit_unavailability(ledger(future), as_of=AS_OF)
        self.assertEqual(report["unavailable"], {})
        self.assertIn("entry_from_future", {item["code"] for item in report["ignored"]})

    def test_retry_at_not_after_observed_at_is_ignored(self):
        bad = unavailability_entry(observed_at="2026-08-24T11:30:00Z", retry_at="2026-08-24T11:30:00Z")
        report = audit.audit_unavailability(ledger(bad), as_of=AS_OF)
        self.assertEqual(report["unavailable"], {})
        self.assertIn("entry_bounds_invalid", {item["code"] for item in report["ignored"]})

    def test_malformed_and_spoofed_entries_are_ignored_not_excluded(self):
        cases = [
            ({"service": "coderabbit"}, "entry_invalid"),
            ({**unavailability_entry(), "extra": "field"}, "entry_invalid"),
            (unavailability_entry(service="not-a-real-service"), "entry_service_invalid"),
            (unavailability_entry(state="banned"), "entry_state_invalid"),
            (unavailability_entry(reason=""), "entry_reason_invalid"),
            (unavailability_entry(reason=123), "entry_reason_invalid"),
            (unavailability_entry(source=""), "entry_source_invalid"),
            (unavailability_entry(observed_at="not-a-date"), "entry_observed_at_invalid"),
            (unavailability_entry(retry_at="not-a-date"), "entry_retry_at_invalid"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_unavailability(ledger(raw), as_of=AS_OF)
                self.assertEqual(report["unavailable"], {})
                self.assertIn(expected, {item["code"] for item in report["ignored"]})

    def test_non_list_entries_and_malformed_top_level_are_ignored_not_excluded(self):
        cases = [
            ({"schema": audit.UNAVAILABILITY_SCHEMA, "entries": "not-a-list"}, "entries_invalid"),
            ({"schema": "wrong-schema", "entries": []}, "snapshot_shape_invalid"),
            ({}, "snapshot_shape_invalid"),
            ([], "snapshot_shape_invalid"),
            (None, "snapshot_shape_invalid"),
            ("not-a-dict", "snapshot_shape_invalid"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                report = audit.audit_unavailability(payload, as_of=AS_OF)
                self.assertEqual(report["unavailable"], {})
                self.assertIn(expected, {item["code"] for item in report["ignored"]})

    def test_invalid_max_age_is_ignored_fail_open(self):
        report = audit.audit_unavailability(
            ledger(unavailability_entry()), as_of=AS_OF, max_age_seconds=-1,
        )
        self.assertEqual(report["unavailable"], {})
        self.assertIn("max_age_invalid", {item["code"] for item in report["ignored"]})

    def test_duplicate_entries_for_one_service_keep_most_recently_observed(self):
        older = unavailability_entry(
            state="cooldown", observed_at="2026-08-24T11:00:00Z", retry_at="2026-08-24T13:00:00Z",
        )
        newer = unavailability_entry(
            state="outage", observed_at="2026-08-24T11:45:00Z", retry_at="2026-08-24T13:00:00Z",
        )
        report = audit.audit_unavailability(ledger(older, newer), as_of=AS_OF)
        self.assertEqual(report["unavailable"]["coderabbit"]["state"], "outage")

        report_reversed = audit.audit_unavailability(ledger(newer, older), as_of=AS_OF)
        self.assertEqual(report_reversed["unavailable"]["coderabbit"]["state"], "outage")

    def test_empty_ledger_excludes_nothing(self):
        report = audit.audit_unavailability(ledger(), as_of=AS_OF)
        self.assertEqual(report["unavailable"], {})
        self.assertEqual(report["ignored"], [])


class LedgerConcurrencyTests(unittest.TestCase):
    """The ledger is shared by every governed checkout on the machine.

    Concurrent writers are therefore the normal case. An unlocked
    read-modify-write loses whichever append finishes first, and the loss is
    silent: the next routing decision simply stops excluding a service that
    really is unavailable.
    """

    def record(self, path, service, minute):
        audit.record_unavailability(
            service, "cooldown", f"cooldown at {minute}", "2026-08-24T14:00:00Z",
            source="test", observed_at=f"2026-08-24T12:{minute:02d}:00Z", path=path,
        )

    def test_concurrent_appends_from_separate_processes_all_survive(self):
        # Real processes, not threads: the GIL would hide the race this guards
        # against, and the writers are genuinely separate repository checkouts.
        writers = 8
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            program = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(ROOT / "scripts")!r})
                import audit_review_service_capacity as audit
                index = int(sys.argv[1])
                audit.record_unavailability(
                    "sourcery", "cooldown", "writer %d" % index,
                    "2026-08-24T14:00:00Z", source="test",
                    observed_at="2026-08-24T12:%02d:00Z" % index,
                    path={str(path)!r},
                )
            """)
            script = Path(directory) / "writer.py"
            script.write_text(program, encoding="utf-8")
            procs = [
                subprocess.Popen([sys.executable, str(script), str(i)])
                for i in range(writers)
            ]
            codes = [proc.wait(timeout=60) for proc in procs]
            snapshot = audit.load_unavailability_snapshot(path)

        self.assertEqual(codes, [0] * writers)
        self.assertEqual(len(snapshot["entries"]), writers)
        self.assertEqual(
            {entry["reason"] for entry in snapshot["entries"]},
            {f"writer {i}" for i in range(writers)},
        )

    def test_append_holds_the_lock_across_the_whole_read_modify_write(self):
        """Re-reading inside the lock is the actual fix; a lock taken only
        around the write still loses the append it raced with."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            self.record(path, "sourcery", 1)
            seen = []
            real_load = audit.load_unavailability_snapshot

            def observing_load(target=None):
                seen.append(Path(str(target)).exists())
                return real_load(target)

            with patch.object(audit, "load_unavailability_snapshot", observing_load):
                self.record(path, "codeant", 2)
            snapshot = real_load(path)
        self.assertEqual(seen, [True])  # read once, inside the lock
        self.assertEqual(len(snapshot["entries"]), 2)

    def test_reader_never_observes_a_partially_written_ledger(self):
        """load_unavailability_snapshot() treats corruption as 'no evidence',
        so a torn write would silently stop excluding every service rather
        than failing loudly. os.replace() makes the swap all-or-nothing."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            self.record(path, "sourcery", 1)
            before = path.read_text(encoding="utf-8")

            real_replace = os.replace
            observed = {}

            def capturing_replace(src, dst):
                # Mid-write: the live ledger must still be the previous
                # complete file, and the new bytes must be somewhere else.
                observed["live"] = Path(dst).read_text(encoding="utf-8")
                observed["staged"] = Path(src).read_text(encoding="utf-8")
                return real_replace(src, dst)

            with patch.object(audit.os, "replace", capturing_replace):
                self.record(path, "codeant", 2)
            after = path.read_text(encoding="utf-8")

        self.assertEqual(observed["live"], before)
        self.assertNotEqual(observed["staged"], before)
        self.assertEqual(after, observed["staged"])
        self.assertEqual(len(json.loads(after)["entries"]), 2)

    def test_failed_write_leaves_the_previous_ledger_and_no_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            self.record(path, "sourcery", 1)
            before = path.read_text(encoding="utf-8")

            with patch.object(audit.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.record(path, "codeant", 2)

            self.assertEqual(path.read_text(encoding="utf-8"), before)
            leftovers = [item.name for item in Path(directory).iterdir()
                         if item.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_lock_file_is_not_mistaken_for_ledger_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            self.record(path, "sourcery", 1)
            snapshot = audit.load_unavailability_snapshot(path)
            names = sorted(item.name for item in Path(directory).iterdir())
        self.assertEqual(len(snapshot["entries"]), 1)
        self.assertEqual(names, ["ledger.json", "ledger.json.lock"])


class UnavailabilityLedgerIoTests(unittest.TestCase):
    def test_missing_file_loads_as_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            snapshot = audit.load_unavailability_snapshot(path)
        self.assertEqual(snapshot, {"schema": audit.UNAVAILABILITY_SCHEMA, "entries": []})

    def test_corrupt_file_loads_as_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.json"
            path.write_text("{not json", encoding="utf-8")
            snapshot = audit.load_unavailability_snapshot(path)
        self.assertEqual(snapshot, {"schema": audit.UNAVAILABILITY_SCHEMA, "entries": []})

    def test_record_unavailability_appends_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            audit.record_unavailability(
                "sourcery", "quota_exhausted", "trial quota exhausted",
                "2026-08-24T13:00:00Z", source="billing-webhook",
                observed_at="2026-08-24T12:00:00Z", path=path,
            )
            audit.record_unavailability(
                "codeant", "outage", "5xx from provider status page",
                "2026-08-24T13:30:00Z", source="status-page-poll",
                observed_at="2026-08-24T12:05:00Z", path=path,
            )
            snapshot = audit.load_unavailability_snapshot(path)
        self.assertEqual(len(snapshot["entries"]), 2)
        self.assertEqual({e["service"] for e in snapshot["entries"]}, {"sourcery", "codeant"})

        report = audit.audit_unavailability(snapshot, as_of="2026-08-24T12:10:00Z")
        self.assertEqual(set(report["unavailable"]), {"sourcery", "codeant"})

    def test_record_unavailability_rejects_unknown_service_state_or_bad_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            with self.assertRaises(ValueError):
                audit.record_unavailability(
                    "not-a-service", "cooldown", "r", "2026-08-24T13:00:00Z",
                    source="s", path=path,
                )
            with self.assertRaises(ValueError):
                audit.record_unavailability(
                    "coderabbit", "not-a-state", "r", "2026-08-24T13:00:00Z",
                    source="s", path=path,
                )
            with self.assertRaises(ValueError):
                audit.record_unavailability(
                    "coderabbit", "cooldown", "r", "2026-08-24T11:00:00Z",
                    source="s", observed_at="2026-08-24T12:00:00Z", path=path,
                )
            with self.assertRaises(ValueError):
                audit.record_unavailability(
                    "coderabbit", "cooldown", "  ", "2026-08-24T13:00:00Z",
                    source="s", path=path,
                )
        self.assertFalse(path.exists())

    def test_default_ledger_path_honors_env_override(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shared.json"
            with patch.dict("os.environ", {audit.CAPACITY_LEDGER_ENV: str(target)}):
                self.assertEqual(audit.unavailability_ledger_path(), target)

    def test_cross_repository_shared_ledger_is_visible_to_a_second_reader(self):
        """Two working directories (simulated repos) pointing
        ARU_REVIEW_CAPACITY_LEDGER at the same path see the same evidence --
        the ledger is a shared cross-repository resource, not per-repo state."""
        with tempfile.TemporaryDirectory() as directory:
            shared_path = Path(directory) / "shared-ledger.json"
            with patch.dict("os.environ", {audit.CAPACITY_LEDGER_ENV: str(shared_path)}):
                # "repo A" records an outage using only the shared env var.
                audit.record_unavailability(
                    "coderabbit", "outage", "provider 5xx", "2026-08-24T13:00:00Z",
                    source="status-page-poll", observed_at="2026-08-24T11:45:00Z",
                )
            with patch.dict("os.environ", {audit.CAPACITY_LEDGER_ENV: str(shared_path)}):
                # "repo B" reads through the same env var, independently of repo A.
                snapshot = audit.load_unavailability_snapshot()
            self.assertEqual(len(snapshot["entries"]), 1)
            self.assertEqual(snapshot["entries"][0]["service"], "coderabbit")
            report = audit.audit_unavailability(snapshot, as_of=AS_OF)
            self.assertEqual(set(report["unavailable"]), {"coderabbit"})


if __name__ == "__main__":
    unittest.main()
