#!/usr/bin/env python3
# line-ceiling: 710
"""Unit tests for factory_loop_ledger.py and fleet_status integration (#471).

Verifies:
- aru.factory_loop_ledger.v1 schema validation and field allowlisting.
- Distinct outcome taxonomy: single-flight skips, missed fires, and stale recoveries.
- Secret/raw prompt/body rejection and lane-utilization latency tracking.
- Bounded explicit pause reasons with optional linked issue/PR.
- Crash consistency, torn-tail recovery, file permissions, and bounded rotation.
- Observational summary calculations (median, P90, idle breakdowns, material progress).
- Read-only integration with fleet_status.py.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import factory_loop_ledger as fll
import fleet_status as fs


def sample_tick_dict(
    *,
    run_id: str = "run_test_001",
    project_slug: str = "gillella/Aru_Agentic_SDLC",
    scheduled_at: str = "2026-08-26T20:00:00Z",
    started_at: str = "2026-08-26T20:00:01Z",
    finished_at: str = "2026-08-26T20:00:05Z",
    stage_durations: dict[str, int] | None = None,
    outcome: str = "success",
    snapshot_hash: str = "abc123def456",
    actions_selected: list[str] | None = None,
    workers_launched: int = 1,
    workers_adopted: int = 0,
    prs_progressed: list[int] | None = None,
    merges_completed: list[int] | None = None,
    delivery_status: str = "progressing",
    assignment_latency_ms: int | float = 120,
    idle_reason: str = "none",
    pause_reason: str | None = None,
    linked_issue: int | None = None,
    linked_pr: int | None = None,
) -> dict:
    return {
        "schema_version": fll.SCHEMA_VERSION,
        "run_id": run_id,
        "project_slug": project_slug,
        "scheduled_at": scheduled_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "stage_durations": stage_durations
        or {
            "snapshot_ms": 500,
            "decision_ms": 200,
            "dispatch_ms": 300,
            "total_ms": 4000,
        },
        "outcome": outcome,
        "snapshot_hash": snapshot_hash,
        "actions_selected": actions_selected
        if actions_selected is not None
        else ["dispatch_worker"],
        "workers_launched": workers_launched,
        "workers_adopted": workers_adopted,
        "prs_progressed": prs_progressed if prs_progressed is not None else [471],
        "merges_completed": merges_completed if merges_completed is not None else [],
        "delivery_status": delivery_status,
        "assignment_latency_ms": assignment_latency_ms,
        "idle_reason": idle_reason,
        "pause_reason": pause_reason,
        "linked_issue": linked_issue,
        "linked_pr": linked_pr,
    }


class TestLedgerSchemaValidation(unittest.TestCase):
    """Verifies schema validation, taxonomy, bounds, and secret rejection."""

    def test_valid_record_passes_validation(self):
        data = sample_tick_dict()
        record = fll.validate_tick_record(data)
        self.assertEqual(record.schema_version, fll.SCHEMA_VERSION)
        self.assertEqual(record.run_id, "run_test_001")
        self.assertEqual(record.outcome, "success")
        self.assertEqual(record.idle_reason, "none")
        self.assertIsNone(record.pause_reason)

    def test_distinct_outcomes_accepted(self):
        expected_outcomes = {
            "success",
            "failure",
            "skipped-single-flight",
            "missed-fire",
            "stale-recovery",
            "waiting",
            "paused",
            "error",
        }
        self.assertEqual(set(fll.OUTCOMES), expected_outcomes)

        for out in expected_outcomes:
            pause = "operator-requested" if out == "paused" else None
            data = sample_tick_dict(outcome=out, pause_reason=pause)
            record = fll.validate_tick_record(data)
            self.assertEqual(record.outcome, out)

    def test_invalid_outcome_rejected(self):
        data = sample_tick_dict(outcome="ok")
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data)

    def test_idle_reasons_taxonomy(self):
        expected_idle_reasons = {
            "no-ready-work",
            "dependency-blocked",
            "touches-contention",
            "review-wait",
            "ci-wait",
            "needs-human",
            "needs-design",
            "quota-limited",
            "none",
        }
        self.assertEqual(set(fll.IDLE_REASONS), expected_idle_reasons)

        for reason in expected_idle_reasons:
            data = sample_tick_dict(idle_reason=reason)
            record = fll.validate_tick_record(data)
            self.assertEqual(record.idle_reason, reason)

    def test_invalid_idle_reason_rejected(self):
        data = sample_tick_dict(idle_reason="sleeping")
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data)

    def test_pause_reason_required_for_paused_outcome(self):
        for reason in fll.PAUSE_REASONS:
            data = sample_tick_dict(
                outcome="paused",
                pause_reason=reason,
                linked_issue=471,
                linked_pr=472,
            )
            record = fll.validate_tick_record(data)
            self.assertEqual(record.outcome, "paused")
            self.assertEqual(record.pause_reason, reason)
            self.assertEqual(record.linked_issue, 471)
            self.assertEqual(record.linked_pr, 472)

        # Missing pause reason when paused
        data_missing = sample_tick_dict(outcome="paused", pause_reason=None)
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data_missing)

        # Invalid pause reason
        data_invalid = sample_tick_dict(outcome="paused", pause_reason="coffee-break")
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data_invalid)

        # Pause reason present when outcome is not paused
        data_mismatched = sample_tick_dict(
            outcome="success", pause_reason="operator-requested"
        )
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data_mismatched)

    def test_secret_and_unallowlisted_fields_rejected(self):
        sensitive_keys = [
            "raw_prompt",
            "prompt",
            "raw_issue_body",
            "issue_body",
            "token",
            "secret",
            "password",
            "argv",
            "env",
            "custom_payload",
        ]
        for key in sensitive_keys:
            data = sample_tick_dict()
            data[key] = "sensitive-content-123"
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(data)

    def test_numeric_bounds_and_timestamps(self):
        # Negative assignment latency rejected
        data = sample_tick_dict(assignment_latency_ms=-5)
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data)

        # Negative worker count rejected
        data = sample_tick_dict(workers_launched=-1)
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data)

        # Invalid timestamp format rejected
        data = sample_tick_dict(scheduled_at="yesterday")
        with self.assertRaises(fll.LedgerValidationError):
            fll.validate_tick_record(data)

    def test_booleans_and_non_finite_numbers_rejected(self):
        # Booleans rejected as integers or numbers
        for val in (True, False):
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(assignment_latency_ms=val))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(
                    sample_tick_dict(stage_durations={"total_ms": val})
                )
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(workers_launched=val))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(workers_adopted=val))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(prs_progressed=[val]))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(merges_completed=[val]))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(linked_issue=val))
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(sample_tick_dict(linked_pr=val))

        # Non-finite numbers rejected
        for non_finite in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(
                    sample_tick_dict(assignment_latency_ms=non_finite)
                )
            with self.assertRaises(fll.LedgerValidationError):
                fll.validate_tick_record(
                    sample_tick_dict(stage_durations={"total_ms": non_finite})
                )

    def test_slug_validation_and_containment(self):
        # Valid slugs
        self.assertEqual(
            fll.sanitize_slug("gillella/Aru_Agentic_SDLC"), "gillella__Aru_Agentic_SDLC"
        )
        self.assertEqual(fll.sanitize_slug("my-project.1"), "my-project.1")

        # Invalid slugs
        for bad_slug in (
            r"..\outside",
            r"owner\repo",
            "../traversal",
            "owner/../repo",
            "owner/./repo",
            "/absolute/path",
            "trailing/slash/",
            "empty//segment",
            "invalid char*",
            "",
        ):
            with self.subTest(bad_slug=bad_slug):
                with self.assertRaises(fll.LedgerValidationError):
                    fll.sanitize_slug(bad_slug)
                with self.assertRaises(fll.LedgerValidationError):
                    fll.validate_tick_record(sample_tick_dict(project_slug=bad_slug))


class TestLedgerStorageAndPersistence(unittest.TestCase):
    """Verifies crash-consistent storage, permissions, locking, and recovery."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ledger_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_append_creates_secure_file_and_reads_back(self):
        slug = "gillella/Aru_Agentic_SDLC"
        record_data = sample_tick_dict(run_id="run_001")

        fll.append_tick_record(record_data, base_dir=self.ledger_dir)

        ledger_file = fll.ledger_file_for_slug(slug, base_dir=self.ledger_dir)
        self.assertTrue(ledger_file.exists())

        # Check permissions: 0600 on file, 0700 on dir
        file_mode = stat.S_IMODE(os.stat(ledger_file).st_mode)
        self.assertEqual(file_mode, 0o600)
        dir_mode = stat.S_IMODE(os.stat(self.ledger_dir).st_mode)
        self.assertEqual(dir_mode, 0o700)

        # Read back
        records = fll.read_tick_records(slug, base_dir=self.ledger_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].run_id, "run_001")

    def test_torn_tail_recovery_ignores_incomplete_final_line(self):
        slug = "gillella/Aru_Agentic_SDLC"
        fll.append_tick_record(
            sample_tick_dict(run_id="run_complete_1"), base_dir=self.ledger_dir
        )
        fll.append_tick_record(
            sample_tick_dict(run_id="run_complete_2"), base_dir=self.ledger_dir
        )

        ledger_file = fll.ledger_file_for_slug(slug, base_dir=self.ledger_dir)
        with open(ledger_file, "a", encoding="utf-8") as fh:
            fh.write('{"schema_version": "aru.factory_loop_ledger.v1", "run_id": "ru')

        # Reading back should recover the 2 complete records and safely ignore the torn tail
        records = fll.read_tick_records(slug, base_dir=self.ledger_dir)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].run_id, "run_complete_1")
        self.assertEqual(records[1].run_id, "run_complete_2")

    def test_interior_corruption_fails_closed(self):
        slug = "gillella/Aru_Agentic_SDLC"
        fll.append_tick_record(
            sample_tick_dict(run_id="run_complete_1"), base_dir=self.ledger_dir
        )

        ledger_file = fll.ledger_file_for_slug(slug, base_dir=self.ledger_dir)
        with open(ledger_file, "a", encoding="utf-8") as fh:
            fh.write('{"corrupt": true}\n')

        fll.append_tick_record(
            sample_tick_dict(run_id="run_complete_2"), base_dir=self.ledger_dir
        )

        # Interior corruption fails closed
        with self.assertRaises(fll.LedgerCorruptionError):
            fll.read_tick_records(slug, base_dir=self.ledger_dir)

    def test_bounded_rotation(self):
        slug = "gillella/Aru_Agentic_SDLC"
        for i in range(10):
            fll.append_tick_record(
                sample_tick_dict(run_id=f"run_{i}"), base_dir=self.ledger_dir
            )

        ledger_file = fll.ledger_file_for_slug(slug, base_dir=self.ledger_dir)
        rotated_file = fll.rotated_file_for_slug(slug, base_dir=self.ledger_dir)

        self.assertTrue(ledger_file.exists())
        self.assertFalse(rotated_file.exists())

        # Rotate
        fll.rotate_ledger(slug, base_dir=self.ledger_dir)

        self.assertTrue(rotated_file.exists())
        self.assertFalse(ledger_file.exists())

        # Append new record creates fresh ledger
        fll.append_tick_record(
            sample_tick_dict(run_id="run_new"), base_dir=self.ledger_dir
        )
        self.assertTrue(ledger_file.exists())
        records = fll.read_tick_records(slug, base_dir=self.ledger_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].run_id, "run_new")

    def test_append_tick_record_handles_write_failure_without_double_close(self):
        record_data = sample_tick_dict(run_id="run_err_test")
        original_fdopen = fll.os.fdopen

        class FailingWriter:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self._fh.__exit__(exc_type, exc_val, exc_tb)

            def fileno(self):
                return self._fh.fileno()

            def write(self, _):
                raise OSError("simulated disk full")

            def flush(self):
                self._fh.flush()

        def mock_fdopen(fd, mode="r", encoding="utf-8"):
            real_fh = original_fdopen(fd, mode, encoding=encoding)
            return FailingWriter(real_fh)

        with patch("factory_loop_ledger.os.fdopen", side_effect=mock_fdopen):
            with self.assertRaises(OSError) as cm:
                fll.append_tick_record(record_data, base_dir=self.ledger_dir)
            self.assertEqual(str(cm.exception), "simulated disk full")


class TestLedgerSummarization(unittest.TestCase):
    """Verifies summary math, percentiles, lane utilization, and progress tracking."""

    def test_empty_ledger_summary(self):
        summary = fll.summarize_ledger([])
        self.assertEqual(summary["total_ticks"], 0)
        self.assertEqual(summary["median_tick_duration_ms"], 0.0)
        self.assertEqual(summary["p90_tick_duration_ms"], 0.0)
        self.assertEqual(summary["skipped_fires"], 0)
        self.assertEqual(summary["avoidable_idle_count"], 0)
        self.assertEqual(summary["active_lanes"], 0)
        self.assertIsNone(summary["last_material_progress_at"])

    def test_comprehensive_summary_metrics(self):
        ticks = [
            sample_tick_dict(
                run_id="run_1",
                started_at="2026-08-26T20:00:00Z",
                finished_at="2026-08-26T20:00:02Z",
                stage_durations={
                    "snapshot_ms": 100,
                    "decision_ms": 100,
                    "dispatch_ms": 100,
                    "total_ms": 1000,
                },
                outcome="success",
                workers_launched=2,
                workers_adopted=0,
                prs_progressed=[471],
                merges_completed=[],
                assignment_latency_ms=100,
                idle_reason="none",
            ),
            sample_tick_dict(
                run_id="run_2",
                started_at="2026-08-26T20:01:00Z",
                finished_at="2026-08-26T20:01:03Z",
                stage_durations={
                    "snapshot_ms": 100,
                    "decision_ms": 100,
                    "dispatch_ms": 100,
                    "total_ms": 2000,
                },
                outcome="skipped-single-flight",
                workers_launched=0,
                workers_adopted=0,
                prs_progressed=[],
                merges_completed=[],
                assignment_latency_ms=0,
                idle_reason="touches-contention",
            ),
            sample_tick_dict(
                run_id="run_3",
                started_at="2026-08-26T20:02:00Z",
                finished_at="2026-08-26T20:02:04Z",
                stage_durations={
                    "snapshot_ms": 100,
                    "decision_ms": 100,
                    "dispatch_ms": 100,
                    "total_ms": 3000,
                },
                outcome="missed-fire",
                workers_launched=0,
                workers_adopted=0,
                prs_progressed=[],
                merges_completed=[],
                assignment_latency_ms=0,
                idle_reason="dependency-blocked",
            ),
            sample_tick_dict(
                run_id="run_4",
                started_at="2026-08-26T20:03:00Z",
                finished_at="2026-08-26T20:03:05Z",
                stage_durations={
                    "snapshot_ms": 100,
                    "decision_ms": 100,
                    "dispatch_ms": 100,
                    "total_ms": 4000,
                },
                outcome="paused",
                pause_reason="operator-requested",
                workers_launched=0,
                workers_adopted=0,
                prs_progressed=[],
                merges_completed=[],
                assignment_latency_ms=0,
                idle_reason="none",
            ),
            sample_tick_dict(
                run_id="run_5",
                started_at="2026-08-26T20:04:00Z",
                finished_at="2026-08-26T20:04:10Z",
                stage_durations={
                    "snapshot_ms": 100,
                    "decision_ms": 100,
                    "dispatch_ms": 100,
                    "total_ms": 10000,
                },
                outcome="success",
                workers_launched=0,
                workers_adopted=1,
                prs_progressed=[],
                merges_completed=[470],
                assignment_latency_ms=300,
                idle_reason="none",
            ),
        ]

        records = [fll.validate_tick_record(t) for t in ticks]
        summary = fll.summarize_ledger(records)

        self.assertEqual(summary["total_ticks"], 5)
        # Durations: [1000, 2000, 3000, 4000, 10000] -> median 3000, p90 10000
        self.assertEqual(summary["median_tick_duration_ms"], 3000.0)
        self.assertEqual(summary["p90_tick_duration_ms"], 10000.0)

        # Outcomes
        self.assertEqual(summary["outcomes"]["success"], 2)
        self.assertEqual(summary["outcomes"]["skipped-single-flight"], 1)
        self.assertEqual(summary["outcomes"]["missed-fire"], 1)
        self.assertEqual(summary["outcomes"]["paused"], 1)
        self.assertEqual(summary["skipped_fires"], 2)

        # Idle causes
        self.assertEqual(summary["idle_reasons"]["touches-contention"], 1)
        self.assertEqual(summary["idle_reasons"]["dependency-blocked"], 1)
        self.assertEqual(summary["avoidable_idle_count"], 1)  # touches-contention
        self.assertEqual(summary["dependency_contention_count"], 2)  # touches + dep

        # Material progress tracking
        self.assertEqual(summary["last_material_progress_at"], "2026-08-26T20:04:10Z")
        self.assertEqual(summary["total_merges_completed"], 1)
        self.assertEqual(summary["total_workers_launched"], 2)
        self.assertEqual(summary["total_workers_adopted"], 1)


class TestFleetStatusIntegration(unittest.TestCase):
    """Verifies read-only integration of ledger summaries into fleet_status."""

    def test_evaluate_fleet_status_includes_ledger_summary(self):
        slug = "gillella/Aru_Agentic_SDLC"
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp)
            fll.append_tick_record(
                sample_tick_dict(
                    run_id="run_fs_01",
                    stage_durations={
                        "snapshot_ms": 50,
                        "decision_ms": 50,
                        "dispatch_ms": 50,
                        "total_ms": 1500,
                    },
                    outcome="success",
                ),
                base_dir=ledger_dir,
            )

            with patch("fleet_status.query_open_issues", return_value=[]), patch(
                "fleet_status.governed_board_inventory", return_value=({}, 0)
            ), patch("fleet_status.list_open_prs", return_value=[]), patch(
                "fleet_status.list_worktrees",
                return_value=[{"path": "/repo", "branch": "main"}],
            ), patch("fleet_status.get_repo_slug", return_value=slug), patch(
                "factory_loop_ledger.DEFAULT_LEDGER_DIR", ledger_dir
            ):
                status = fs.evaluate_fleet_status(".")
                self.assertIn("ledger_summary", status)
                self.assertEqual(status["ledger_summary"]["total_ticks"], 1)
                self.assertEqual(
                    status["ledger_summary"]["median_tick_duration_ms"], 1500.0
                )

                formatted = fs.format_status(status)
                self.assertIn("Run Health & Lane Utilization", formatted)
                self.assertIn("Ticks: 1", formatted)

    def test_fleet_status_reports_ledger_corruption_without_error_state(self):
        slug = "gillella/Aru_Agentic_SDLC"
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp)
            ledger_file = ledger_dir / f"{fll.sanitize_slug(slug)}.jsonl"
            ledger_file.write_text('{"corrupt": true}\n', encoding="utf-8")

            with patch("fleet_status.query_open_issues", return_value=[]), patch(
                "fleet_status.governed_board_inventory", return_value=({}, 0)
            ), patch("fleet_status.list_open_prs", return_value=[]), patch(
                "fleet_status.list_worktrees",
                return_value=[{"path": "/repo", "branch": "main"}],
            ), patch("fleet_status.get_repo_slug", return_value=slug), patch(
                "factory_loop_ledger.DEFAULT_LEDGER_DIR", ledger_dir
            ):
                status = fs.evaluate_fleet_status(".")
                self.assertIsNone(status["ledger_summary"])
                self.assertEqual(status["state"], "complete")
                self.assertEqual(status["exit_code"], fs.EXIT_COMPLETE)
                self.assertTrue(
                    any(
                        "Local factory loop ledger is unreadable:" in r
                        for r in status["reasons"]
                    )
                )

                formatted = fs.format_status(status)
                self.assertIn("Local factory loop ledger is unreadable:", formatted)


class TestLedgerCLI(unittest.TestCase):
    """Verifies factory_loop_ledger CLI commands and formats."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ledger_dir = Path(self.tmp_dir.name)
        self.slug = "gillella/Aru_Agentic_SDLC"
        fll.append_tick_record(
            sample_tick_dict(run_id="run_cli_1", outcome="success"),
            base_dir=self.ledger_dir,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cli_summary_text_and_json(self):
        with patch(
            "sys.argv",
            [
                "factory_loop_ledger.py",
                "summary",
                "--project-slug",
                self.slug,
                "--dir",
                str(self.ledger_dir),
                "--json",
            ],
        ), patch("builtins.print") as mock_print:
            fll.main()
            mock_print.assert_called_once()
            output = json.loads(mock_print.call_args[0][0])
            self.assertEqual(output["total_ticks"], 1)

    def test_cli_list_text_and_json(self):
        with patch(
            "sys.argv",
            [
                "factory_loop_ledger.py",
                "list",
                "--project-slug",
                self.slug,
                "--dir",
                str(self.ledger_dir),
                "--json",
            ],
        ), patch("builtins.print") as mock_print:
            fll.main()
            mock_print.assert_called_once()
            output = json.loads(mock_print.call_args[0][0])
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0]["run_id"], "run_cli_1")

    def test_cli_rotate(self):
        with patch(
            "sys.argv",
            [
                "factory_loop_ledger.py",
                "rotate",
                "--project-slug",
                self.slug,
                "--dir",
                str(self.ledger_dir),
            ],
        ), patch("builtins.print") as mock_print:
            fll.main()
            mock_print.assert_called_with("Rotated")


class TestLedgerReadOnlyIsolation(unittest.TestCase):
    """Verifies that reading and summary operations are strictly non-mutating."""

    def test_read_on_missing_ledger_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp)
            records = fll.read_tick_records("owner/missing", base_dir=ledger_dir)
            self.assertEqual(records, [])
            # Directory should be clean, no files created
            self.assertEqual(list(ledger_dir.iterdir()), [])

    def test_summarize_ledger_is_pure_function(self):
        records = [fll.validate_tick_record(sample_tick_dict())]
        summary1 = fll.summarize_ledger(records)
        summary2 = fll.summarize_ledger(records)
        self.assertEqual(summary1, summary2)


if __name__ == "__main__":
    unittest.main()
