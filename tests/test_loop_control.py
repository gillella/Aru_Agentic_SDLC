#!/usr/bin/env python3
# line-ceiling: 425
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import loop_control


class LoopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target_home = Path(self.temp_dir.name)
        self.aru_dir = self.target_home / ".aru"
        self.aru_dir.mkdir(parents=True, exist_ok=True)
        self.project_a = "/tmp/test-project-alpha"
        self.project_b = "/tmp/test-project-beta"

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *args):
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "loop_control.py"),
            "--target-home", str(self.target_home),
        ] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def run_doctor(self, *args):
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "doctor_local_agent_integrations.py"),
            "--aru-home", str(ROOT),
            "--target-home", str(self.target_home),
        ] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def create_adapter(self, payload: dict | str, exit_code: int = 0) -> str:
        bin_path = self.target_home / "fake_adapter.sh"
        body = payload if isinstance(payload, str) else json.dumps(payload)
        content = f"#!/bin/sh\ncat << 'EOF'\n{body}\nEOF\nexit {exit_code}\n"
        bin_path.write_text(content, encoding="utf-8")
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
        return str(bin_path)

    def test_status_payload_shape_and_clean_defaults(self):
        res = self.run_cli("status", "--project", self.project_a, "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["project"], self.project_a)
        self.assertFalse(data["desktop_stop_marker"]["present"])
        self.assertFalse(data["desktop_stop_marker"]["applies"])
        self.assertEqual(data["desktop_stop_marker"]["scope"], "none")
        self.assertFalse(data["orchestrator"]["configured"])
        self.assertEqual(data["orchestrator"]["state"], "unknown")
        self.assertEqual(data["contradictions"], [])
        self.assertEqual(len(data["pause_reasons"]), 6)

    def test_stop_marker_project_and_global_applicability(self):
        loop_control.execute_stop(self.target_home, project=self.project_a, reason="maintenance")
        status_a = loop_control.get_status(self.target_home, project=self.project_a)
        self.assertTrue(status_a["desktop_stop_marker"]["present"])
        self.assertTrue(status_a["desktop_stop_marker"]["applies"])
        self.assertEqual(status_a["desktop_stop_marker"]["scope"], "project")
        self.assertEqual(status_a["desktop_stop_marker"]["reason"], "maintenance")
        self.assertEqual(status_a["status"], "stopped")

        status_b = loop_control.get_status(self.target_home, project=self.project_b)
        self.assertTrue(status_b["desktop_stop_marker"]["present"])
        self.assertFalse(status_b["desktop_stop_marker"]["applies"])
        self.assertEqual(status_b["status"], "ok")

        loop_control.execute_stop(self.target_home, project=None, reason="operator-requested")
        status_global = loop_control.get_status(self.target_home, project=self.project_b)
        self.assertTrue(status_global["desktop_stop_marker"]["applies"])
        self.assertEqual(status_global["desktop_stop_marker"]["scope"], "global")
        self.assertEqual(status_global["status"], "stopped")

    def test_adapter_states_enabled_and_paused(self):
        adapter_enabled = self.create_adapter({"adapter": "test-orch", "state": "enabled", "reason": None, "detail": "active"})
        data_enabled = loop_control.get_status(self.target_home, project=self.project_a, adapter_cmd=adapter_enabled)
        self.assertTrue(data_enabled["orchestrator"]["configured"])
        self.assertEqual(data_enabled["orchestrator"]["state"], "enabled")
        self.assertEqual(data_enabled["status"], "ok")

        adapter_paused = self.create_adapter({"adapter": "test-orch", "state": "paused", "reason": "quota-exhausted", "detail": "cooling"})
        data_paused = loop_control.get_status(self.target_home, project=self.project_a, adapter_cmd=adapter_paused)
        self.assertEqual(data_paused["orchestrator"]["state"], "paused")
        self.assertEqual(data_paused["orchestrator"]["reason"], "quota-exhausted")

    def test_adapter_malformed_and_failing_yields_degraded(self):
        bad_json_adapter = self.create_adapter("NOT_JSON", exit_code=0)
        res1 = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", bad_json_adapter, "--json")
        self.assertEqual(res1.returncode, 2)
        payload1 = json.loads(res1.stdout)
        self.assertEqual(payload1["status"], "degraded")
        self.assertIsNotNone(payload1["orchestrator"]["error"])

        failing_adapter = self.create_adapter("{}", exit_code=1)
        res2 = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", failing_adapter, "--json")
        self.assertEqual(res2.returncode, 2)
        payload2 = json.loads(res2.stdout)
        self.assertEqual(payload2["status"], "degraded")

    def test_contradiction_orchestrator_paused_without_stop_marker(self):
        adapter = self.create_adapter({"adapter": "test-orch", "state": "paused", "reason": "maintenance", "detail": "paused remotely"})
        res = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", adapter, "--json")
        self.assertEqual(res.returncode, 2)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "contradictory")
        ids = [c["id"] for c in data["contradictions"]]
        self.assertIn("orchestrator_paused_without_stop_marker", ids)

    def test_contradiction_stop_marker_without_orchestrator_pause(self):
        loop_control.execute_stop(self.target_home, project=self.project_a)
        adapter = self.create_adapter({"adapter": "test-orch", "state": "enabled", "reason": None, "detail": "active"})
        res = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", adapter, "--json")
        self.assertEqual(res.returncode, 2)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "contradictory")
        ids = [c["id"] for c in data["contradictions"]]
        self.assertIn("stop_marker_without_orchestrator_pause", ids)

    def test_no_contradiction_when_states_agree(self):
        adapter_en = self.create_adapter({"adapter": "test-orch", "state": "enabled", "reason": None, "detail": "ok"})
        res_en = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", adapter_en, "--json")
        self.assertEqual(res_en.returncode, 0)
        self.assertEqual(json.loads(res_en.stdout)["status"], "ok")

        loop_control.execute_stop(self.target_home, project=self.project_a)
        adapter_pz = self.create_adapter({"adapter": "test-orch", "state": "paused", "reason": "operator-requested", "detail": "stopped"})
        res_pz = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", adapter_pz, "--json")
        self.assertEqual(res_pz.returncode, 0)
        self.assertEqual(json.loads(res_pz.stdout)["status"], "stopped")
        self.assertEqual(json.loads(res_pz.stdout)["contradictions"], [])

    def test_desktop_stop_marker_vs_orchestrator_paused_not_conflated(self):
        adapter = self.create_adapter({"adapter": "test-orch", "state": "paused", "reason": "quota-exhausted", "detail": "cooling"})
        data = loop_control.get_status(self.target_home, project=self.project_a, adapter_cmd=adapter)
        self.assertFalse(data["desktop_stop_marker"]["applies"])
        self.assertFalse(data["desktop_stop_marker"]["present"])
        self.assertEqual(data["orchestrator"]["state"], "paused")
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())

    def test_bounded_pause_reason_vocabulary(self):
        for reason in loop_control.PAUSE_REASONS:
            res = loop_control.execute_stop(self.target_home, project=self.project_a, reason=reason)
            self.assertEqual(res["reason"], reason)
            marker = loop_control.resolve_desktop_stop_marker(self.target_home, project=self.project_a)
            self.assertEqual(marker["reason"], reason)

        with self.assertRaises(ValueError):
            loop_control.execute_stop(self.target_home, project=self.project_a, reason="unsupported-reason")

    def test_malformed_json_fails_closed(self):
        stop_file = self.aru_dir / "factory-loop.stop"
        stop_file.write_text("{CORRUPTED JSON")
        status = loop_control.get_status(self.target_home, project=self.project_a)
        self.assertEqual(status["status"], "invalid")
        self.assertIn("error", status)

        res_stop = self.run_cli("stop", "--project", self.project_a)
        self.assertEqual(res_stop.returncode, 1)

        res_resume = self.run_cli("resume", "--project", self.project_a)
        self.assertEqual(res_resume.returncode, 1)

    def test_relative_project_fails_closed(self):
        res = self.run_cli("status", "--project", "relative/path")
        self.assertEqual(res.returncode, 1)
        self.assertIn("absolute", res.stderr)

    def test_no_hardcoded_identifiers_in_loop_control(self):
        text = (ROOT / "scripts" / "loop_control.py").read_text(encoding="utf-8")
        forbidden = ["hermes", "telegram", "gillella", "anguliyam", "cron"]
        lower = text.lower()
        for term in forbidden:
            self.assertNotIn(term, lower, f"Hard-coded identifier {term} found in loop_control.py")

    def test_global_status_without_project_only_matches_global_star_marker(self):
        loop_control.execute_stop(self.target_home, project=self.project_a, reason="maintenance")
        status_global = loop_control.get_status(self.target_home, project=None)
        self.assertTrue(status_global["desktop_stop_marker"]["present"])
        self.assertFalse(status_global["desktop_stop_marker"]["applies"])
        self.assertEqual(status_global["desktop_stop_marker"]["scope"], "project")
        self.assertEqual(status_global["status"], "ok")

        res_cli = self.run_cli("status", "--json")
        self.assertEqual(res_cli.returncode, 0)
        cli_data = json.loads(res_cli.stdout)
        self.assertEqual(cli_data["status"], "ok")
        self.assertFalse(cli_data["desktop_stop_marker"]["applies"])

        loop_control.execute_stop(self.target_home, project=None, reason="operator-requested")
        status_star = loop_control.get_status(self.target_home, project=None)
        self.assertTrue(status_star["desktop_stop_marker"]["present"])
        self.assertTrue(status_star["desktop_stop_marker"]["applies"])
        self.assertEqual(status_star["desktop_stop_marker"]["scope"], "global")
        self.assertEqual(status_star["status"], "stopped")

        res_star_cli = self.run_cli("status", "--json")
        self.assertEqual(res_star_cli.returncode, 0)
        star_cli_data = json.loads(res_star_cli.stdout)
        self.assertEqual(star_cli_data["status"], "stopped")
        self.assertTrue(star_cli_data["desktop_stop_marker"]["applies"])

    def test_doctor_and_loop_control_malformed_stop_marker_yields_invalid_json_without_traceback(self):
        malformed_cases = [
            "{CORRUPTED JSON",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"projects": "not-a-list"}),
            json.dumps({"projects": 12345}),
        ]
        stop_file = self.aru_dir / "factory-loop.stop"
        for payload in malformed_cases:
            stop_file.write_text(payload, encoding="utf-8")
            status = loop_control.get_status(self.target_home, project=self.project_a)
            self.assertEqual(status["status"], "invalid")
            self.assertIn("error", status)
            self.assertTrue(status["desktop_stop_marker"]["present"])
            self.assertFalse(status["desktop_stop_marker"]["applies"])

            res_doc = self.run_doctor("--json", "--project", self.project_a)
            self.assertEqual(res_doc.returncode, 1)
            self.assertNotIn("Traceback", res_doc.stderr)
            doc_data = json.loads(res_doc.stdout)
            self.assertEqual(doc_data["status"], "invalid")
            self.assertTrue(doc_data["desktop_stop_marker"]["present"])
            stop_checks = [c for c in doc_data["checks"] if c["id"] == "stop_marker"]
            self.assertTrue(len(stop_checks) >= 1)
            self.assertFalse(stop_checks[0]["ok"])
            self.assertEqual(stop_checks[0]["severity"], "invalid")

            res_doc_global = self.run_doctor("--json")
            self.assertEqual(res_doc_global.returncode, 1)
            self.assertNotIn("Traceback", res_doc_global.stderr)
            doc_global_data = json.loads(res_doc_global.stdout)
            self.assertEqual(doc_global_data["status"], "invalid")
            self.assertTrue(doc_global_data["desktop_stop_marker"]["present"])

    def test_adapter_unhashable_and_non_string_state_handled_safely(self):
        unusual_states = [
            ["enabled", "active"],
            {"nested": "state"},
            12345,
            12.34,
            True,
            None,
            "unrecognized_custom_state",
        ]
        for bad_state in unusual_states:
            adapter = self.create_adapter({"adapter": "test-orch", "state": bad_state, "detail": "custom"})
            res = self.run_cli("status", "--project", self.project_a, "--orchestrator-adapter", adapter, "--json")
            self.assertEqual(res.returncode, 0, f"Failed for state: {bad_state!r}")
            data = json.loads(res.stdout)
            self.assertEqual(data["orchestrator"]["state"], "unknown")
            self.assertEqual(data["status"], "ok")
            self.assertIsNone(data["orchestrator"]["error"])

    def test_cli_argument_validation_and_errors(self):
        res_stop_reason = self.run_cli("stop", "--reason", "invalid-reason")
        self.assertEqual(res_stop_reason.returncode, 1)
        self.assertIn("invalid pause reason", res_stop_reason.stderr)

        res_resume_reason = self.run_cli("resume", "--reason", "invalid-reason")
        self.assertEqual(res_resume_reason.returncode, 1)
        self.assertIn("invalid pause reason", res_resume_reason.stderr)

        res_stop_rel = self.run_cli("stop", "--project", "relative/path")
        self.assertEqual(res_stop_rel.returncode, 1)
        self.assertIn("absolute", res_stop_rel.stderr)

        res_resume_rel = self.run_cli("resume", "--project", "relative/path")
        self.assertEqual(res_resume_rel.returncode, 1)
        self.assertIn("absolute", res_resume_rel.stderr)

        stop_file = self.aru_dir / "factory-loop.stop"
        stop_file.write_text("INVALID JSON", encoding="utf-8")
        res_status_json = self.run_cli("status", "--json", "--project", self.project_a)
        self.assertEqual(res_status_json.returncode, 1)
        data = json.loads(res_status_json.stdout)
        self.assertEqual(data["status"], "invalid")
        self.assertTrue(data["desktop_stop_marker"]["present"])
        self.assertIn("error", data)

    def test_corrupt_stop_marker_is_distinguishable_from_an_absent_one(self):
        """A present-but-unreadable marker must never look like 'no stop requested'."""
        stop_file = self.aru_dir / "factory-loop.stop"
        absent = loop_control.get_status(self.target_home, project=self.project_a)
        self.assertFalse(absent["desktop_stop_marker"]["present"])
        self.assertTrue(absent["desktop_stop_marker"]["valid"])
        self.assertIsNone(absent["desktop_stop_marker"]["error"])
        self.assertEqual(absent["status"], "ok")

        # `parses_to_object` marks the payloads `load_json` can still return, so
        # the legacy `stop` field keeps the real file contents for those.
        cases = [("{CORRUPTED JSON", False), (json.dumps(["not", "a", "dict"]), False),
                 ("", False), (json.dumps({"projects": "not-a-list"}), True)]
        for payload, parses_to_object in cases:
            stop_file.write_text(payload, encoding="utf-8")
            marker = loop_control.get_status(self.target_home, project=self.project_a)["desktop_stop_marker"]
            self.assertTrue(marker["present"], payload)
            self.assertFalse(marker["valid"], payload)
            self.assertFalse(marker["applies"], payload)
            self.assertIn("malformed stop marker", marker["error"] or "")
            self.assertEqual(marker["path"], str(stop_file))

            res = self.run_cli("status", "--project", self.project_a)
            self.assertEqual(res.returncode, 1, payload)
            self.assertNotIn("Traceback", res.stderr)
            self.assertIn("valid=False", res.stdout)

            doc = json.loads(self.run_doctor("--json", "--project", self.project_a).stdout)
            doc_marker = doc["desktop_stop_marker"]
            self.assertTrue(doc_marker["present"], payload)
            self.assertFalse(doc_marker["valid"], payload)
            self.assertIn("malformed stop marker", doc_marker["error"] or "")
            # The legacy `stop` field must not collapse a corrupt marker to null,
            # which is exactly what an absent marker reports.
            self.assertIsNotNone(doc["stop"], payload)
            if not parses_to_object:
                self.assertFalse(doc["stop"]["valid"], payload)
                self.assertEqual(doc["stop"]["path"], str(stop_file))

        stop_file.unlink()
        doc_absent = json.loads(self.run_doctor("--json", "--project", self.project_a).stdout)
        self.assertIsNone(doc_absent["stop"])
        self.assertTrue(doc_absent["desktop_stop_marker"]["valid"])

    def test_valid_stop_marker_reports_itself_as_valid(self):
        self.run_cli("stop", "--project", self.project_a, "--reason", "maintenance")
        marker = loop_control.get_status(self.target_home, project=self.project_a)["desktop_stop_marker"]
        self.assertTrue(marker["present"])
        self.assertTrue(marker["valid"])
        self.assertIsNone(marker["error"])
        self.assertTrue(marker["applies"])

    def test_non_string_adapter_state_never_crashes_status_or_contradictions(self):
        """Unhashable/non-string adapter state degrades to 'unknown', it does not raise."""
        hostile_states = [
            ["paused"], {"paused": True}, {"a": ["b", {"c": 1}]}, [[1, 2], [3, 4]],
            0, 1, -1, 0.0, True, False, None, "", "PAUSED", "paused ", "enabled\n",
        ]
        self.run_cli("stop", "--project", self.project_a)
        for bad_state in hostile_states:
            adapter = self.create_adapter({"adapter": "orch", "state": bad_state})
            orch = loop_control.query_orchestrator(adapter, self.project_a)
            self.assertEqual(orch["state"], "unknown", repr(bad_state))
            self.assertIsNone(orch["error"], repr(bad_state))
            # detect_contradictions must also tolerate the normalised value.
            marker = loop_control.resolve_desktop_stop_marker(self.target_home, self.project_a)
            self.assertEqual(loop_control.detect_contradictions(marker, orch), [])

            res = self.run_cli("status", "--project", self.project_a,
                               "--orchestrator-adapter", adapter, "--json")
            self.assertEqual(res.returncode, 0, repr(bad_state))
            self.assertNotIn("Traceback", res.stderr)
            self.assertEqual(json.loads(res.stdout)["orchestrator"]["state"], "unknown")

    def test_non_string_adapter_keys_and_non_dict_wake_entries_do_not_crash(self):
        """Adapter/reason/detail and persisted wake entries of the wrong type stay inert."""
        adapter = self.create_adapter(
            {"adapter": ["a", "b"], "state": {"x": 1}, "reason": {"r": 1}, "detail": ["d"]}
        )
        res = self.run_cli("status", "--project", self.project_a,
                           "--orchestrator-adapter", adapter, "--json")
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(json.loads(res.stdout)["orchestrator"]["state"], "unknown")

        res_human = self.run_cli("status", "--project", self.project_a,
                                 "--orchestrator-adapter", adapter)
        self.assertEqual(res_human.returncode, 0)
        self.assertNotIn("Traceback", res_human.stderr)
        self.assertIn("Aru loop-control status:", res_human.stdout)

        wake_file = self.aru_dir / "native-wake.json"
        for doc in ({"projects": {self.project_a: ["enabled"]}},
                    {"projects": {self.project_a: "enabled"}},
                    {"projects": {self.project_a: 1}},
                    {"projects": [self.project_a]},
                    {"projects": "nope"},
                    ["not", "a", "dict"]):
            wake_file.write_text(json.dumps(doc), encoding="utf-8")
            wake = loop_control.resolve_native_wake(self.target_home, project=self.project_a)
            self.assertFalse(wake["enabled"], doc)
            self.assertIsNone(wake["automation_id"], doc)
            res_w = self.run_cli("status", "--project", self.project_a, "--json")
            self.assertEqual(res_w.returncode, 0, doc)
            self.assertNotIn("Traceback", res_w.stderr)


if __name__ == "__main__":
    unittest.main()
