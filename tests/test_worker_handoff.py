# line-ceiling: 440
"""Focused worker-handoff and multi-lane recovery contract for issue #479."""

import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_next_work as fnw  # noqa: E402


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
PROJECT = "proj_repo_1234567890abcdef"


def start_fields(number=479, *, project=PROJECT, state="running", detail=""):
    return {
        "project_id": project,
        "checkout_path": "/tmp/aru-checkout",
        "agent_id": f"codex-{number}",
        "family": "openai",
        "unit_kind": "issue",
        "unit_number": number,
        "lane": f"codex-{number}",
        "branch": f"feat/issue-{number}-worker",
        "worktree_path": f"/tmp/aru-checkout/.worktrees/issue-{number}",
        "session_handle": "remote-host|123",
        "start_state": "a" * 40,
        "expected_evidence": "pull-request-open",
        "state": state,
        "detail": detail,
    }


class WorkerRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "worker-handoff.json"
        self.store = fnw.WorkerHandoffStore(self.path, clock=lambda: NOW)

    def test_start_round_trips_every_required_field_in_private_project_store(self):
        record = self.store.record_start(**start_fields())
        reread = self.store.inventory(project_id=PROJECT)

        self.assertEqual(reread, [record])
        self.assertEqual(record.worker_id, f"{PROJECT}:issue-479")
        self.assertEqual(record.lane, "codex-479")
        self.assertEqual(record.start_state, "a" * 40)
        self.assertEqual(record.expected_evidence, "pull-request-open")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "aru.worker-handoff/v1")
        self.assertEqual(list(document["workers"]), [record.worker_id])

    def test_duplicate_unit_is_refused_and_later_tick_adopts_same_worker(self):
        original = self.store.record_start(**start_fields())
        with self.assertRaisesRegex(fnw.PresenceError, "already exists"):
            self.store.record_start(**start_fields())

        plan = fnw.plan_worker_dispatch(
            self.store, project_id=PROJECT, unit_kind="issue", unit_number=479, now=NOW,
        )
        self.assertEqual(plan["action"], "adopt")
        self.assertEqual(plan["record"].worker_id, original.worker_id)
        self.assertEqual(
            fnw.plan_worker_dispatch(
                self.store, project_id=PROJECT, unit_kind="issue", unit_number=480, now=NOW,
            )["action"],
            "dispatch",
        )

    def test_terminal_routes_require_independent_matching_evidence(self):
        record = self.store.record_start(**start_fields())
        completed = self.store.observe(record.worker_id, state="completed")
        good = {
            "kind": "pull-request-open", "unit_number": 479,
            "source": "github", "settled": False,
        }
        self.assertEqual(
            fnw.route_worker(completed, now=NOW, evidence=good)["action"], "close-out",
        )
        self.assertEqual(
            fnw.route_worker(completed, now=NOW, evidence={**good, "settled": True})["action"],
            "next-unit",
        )
        for evidence in (
            None,
            {**good, "unit_number": 480},
            {**good, "source": "worker"},
            {**good, "source": "ci"},
        ):
            with self.subTest(evidence=evidence):
                self.assertEqual(
                    fnw.route_worker(completed, now=NOW, evidence=evidence)["action"], "hold",
                )

    def test_failure_quota_and_dead_running_workers_route_independently(self):
        failed = self.store.record_start(**start_fields(480))
        failed = self.store.observe(failed.worker_id, state="failed", detail="child-crash")
        self.assertEqual(
            fnw.route_worker(failed, now=NOW, live_process=False)["action"], "remediation",
        )
        self.assertEqual(
            fnw.route_worker(failed, now=NOW, live_process=True)["action"], "hold",
        )

        quota = self.store.record_start(**start_fields(481))
        quota = self.store.observe(quota.worker_id, state="quota-limited", detail="rate-limited")
        self.assertEqual(fnw.route_worker(quota, now=NOW)["action"], "hold")
        routed = fnw.route_worker(
            quota, now=NOW,
            evidence={"kind": "quota-limited", "source": "provider", "reason": "rate-limited"},
        )
        self.assertEqual((routed["action"], routed["reason"]), ("cooldown", "rate-limited"))

        running = self.store.record_start(**start_fields(482))
        self.assertEqual(
            fnw.route_worker(running, now=NOW, live_process=False)["action"], "adoption",
        )

    def test_terminal_state_cannot_resurrect(self):
        record = self.store.record_start(**start_fields())
        self.store.observe(record.worker_id, state="failed", detail="child-crash")
        with self.assertRaisesRegex(fnw.PresenceError, "terminal"):
            self.store.observe(record.worker_id, state="running")

    def test_store_is_project_scoped_bounded_and_evicts_only_terminal_records(self):
        store = fnw.WorkerHandoffStore(self.path, clock=lambda: NOW, max_per_project=2)
        first = store.record_start(**start_fields(470))
        store.observe(first.worker_id, state="completed")
        store.record_start(**start_fields(471))
        store.record_start(**start_fields(472))
        self.assertEqual([r.unit_number for r in store.inventory(project_id=PROJECT)], [471, 472])

        other = "proj_repo_fedcba0987654321"
        store.record_start(**start_fields(1, project=other))
        self.assertEqual([r.unit_number for r in store.inventory(project_id=other)], [1])
        with self.assertRaisesRegex(fnw.PresenceError, "live worker limit"):
            store.record_start(**start_fields(473))

    def test_unknown_queue_fields_credentials_controls_and_overlong_text_fail_closed(self):
        base = start_fields()
        record = fnw.WorkerRecord.from_dict({
            **base,
            "worker_id": f"{PROJECT}:issue-479",
            "started_at": "2026-08-27T12:00:00Z",
            "last_observed_at": "2026-08-27T12:00:00Z",
            "updated_at": "2026-08-27T12:00:00Z",
        })
        self.assertEqual(record.unit_number, 479)
        for name, value in (
            ("queue", []),
            ("detail", "token=secret"),
            ("branch", "bad\nbranch"),
            ("detail", "x" * 513),
            ("checkout_path", "/tmp/token=secret"),
            ("unit_number", "479"),
        ):
            with self.subTest(name=name):
                candidate = record.public_dict()
                candidate[name] = value
                with self.assertRaises(fnw.PresenceError):
                    fnw.WorkerRecord.from_dict(candidate)

    def test_mid_mutation_failure_preserves_previous_document(self):
        self.store.record_start(**start_fields())
        before = self.path.read_bytes()
        with patch.object(fnw.agent_presence, "_write_unlocked", side_effect=fnw.PresenceError("boom")):
            with self.assertRaisesRegex(fnw.PresenceError, "boom"):
                self.store.observe(f"{PROJECT}:issue-479", state="completed")
        self.assertEqual(self.path.read_bytes(), before)


class LaneRecoveryTests(unittest.TestCase):
    def test_explicit_lanes_must_be_registered_available_and_project_matched(self):
        records = [
            SimpleNamespace(agent_id="codex-2", availability="returned", project_id=PROJECT),
            SimpleNamespace(agent_id="codex-1", availability="available", project_id=PROJECT),
        ]
        with patch.object(fnw, "PresenceStore") as presence:
            presence.return_value.query_project.return_value = records
            agents, project = fnw.verified_lane_agents(
                ["codex-2", "codex-1"], Path("/tmp/aru-checkout"),
            )
        self.assertEqual((agents, project), (["codex-1", "codex-2"], PROJECT))
        presence.return_value.query_project.assert_called_once_with(
            checkout_path=str(Path("/tmp/aru-checkout").resolve()), expire=True,
        )

        records[0].availability = "busy"
        with patch.object(fnw, "PresenceStore") as presence:
            presence.return_value.query_project.return_value = records
            with self.assertRaisesRegex(fnw.PresenceError, "not available"):
                fnw.verified_lane_agents(
                    ["codex-2", "codex-1"], Path("/tmp/aru-checkout"),
                )

    def test_worker_cli_records_launched_lane_for_later_tick(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "workers.json"
            worktree = Path(temp) / "checkout" / ".worktrees" / "issue-479"
            argv = [
                "--worker-path", str(path), "worker-start",
                "--project-id", PROJECT, "--checkout", str(Path(temp) / "checkout"),
                "--agent", "codex-1", "--family", "openai",
                "--unit-kind", "issue", "--unit-number", "479",
                "--lane", "codex-1", "--branch", "feat/issue-479-worker",
                "--worktree", str(worktree), "--session-handle", "remote|123",
                "--start-state", "a" * 40,
                "--expected-evidence", "pull-request-open",
            ]
            with redirect_stdout(StringIO()):
                self.assertEqual(fnw.agent_presence.main(argv), 0)
            records = fnw.WorkerHandoffStore(path).inventory(project_id=PROJECT)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].unit_number, 479)

    def test_live_handoff_suppresses_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            store = fnw.WorkerHandoffStore(Path(temp) / "workers.json", clock=lambda: NOW)
            store.record_start(**start_fields(479))
            result = fnw.apply_worker_handoffs(
                [{"agent": "codex-479", "family": "openai", "work": {
                    "type": "issue", "issue": 479, "resuming": True,
                }}],
                store=store, project_id=PROJECT, now=NOW,
            )
        self.assertFalse(result[0]["dispatchable"])
        self.assertEqual(result[0]["reason"], "already-running")

    def test_terminal_handoff_is_returned_for_governed_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            store = fnw.WorkerHandoffStore(Path(temp) / "workers.json", clock=lambda: NOW)
            record = store.record_start(**start_fields(479))
            store.observe(record.worker_id, state="failed", detail="child-crash")
            result = fnw.apply_worker_handoffs(
                [{"agent": "codex-479", "family": "openai", "work": {
                    "type": "issue", "issue": 479, "resuming": True,
                }}],
                store=store, project_id=PROJECT, now=NOW,
            )
        self.assertFalse(result[0]["dispatchable"])
        self.assertEqual(result[0]["handoff_action"], "route")


if __name__ == "__main__":
    unittest.main()
