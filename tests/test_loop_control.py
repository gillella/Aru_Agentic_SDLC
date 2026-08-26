#!/usr/bin/env python3
import json
import os
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

            res_doc = self.run_doctor("--json", "--project", self.project_a)
            self.assertEqual(res_doc.returncode, 1)
            self.assertNotIn("Traceback", res_doc.stderr)
            doc_data = json.loads(res_doc.stdout)
            self.assertEqual(doc_data["status"], "invalid")
            stop_checks = [c for c in doc_data["checks"] if c["id"] == "stop_marker"]
            self.assertTrue(len(stop_checks) >= 1)
            self.assertFalse(stop_checks[0]["ok"])
            self.assertEqual(stop_checks[0]["severity"], "invalid")

            res_doc_global = self.run_doctor("--json")
            self.assertEqual(res_doc_global.returncode, 1)
            self.assertNotIn("Traceback", res_doc_global.stderr)
            doc_global_data = json.loads(res_doc_global.stdout)
            self.assertEqual(doc_global_data["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
