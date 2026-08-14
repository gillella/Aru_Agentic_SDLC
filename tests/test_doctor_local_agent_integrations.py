import hashlib
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "doctor_local_agent_integrations.py"


class DoctorLocalAgentIntegrationsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target_home = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_doctor(self, *args, env=None):
        cmd = [
            "python3",
            str(DOCTOR),
            "--aru-home",
            str(ROOT),
            "--target-home",
            str(self.target_home),
        ] + list(args)
        merged = dict(os.environ)
        if env:
            merged.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, env=merged)

    def test_json_reports_continuity_slice_not_full_install_doctor(self):
        payload = json.loads(self.run_doctor("--json").stdout)
        self.assertEqual(payload["install_diagnosis"], "not_yet")
        self.assertTrue(payload["issue_34"])
        self.assertIn("codex", payload["agents"])
        self.assertIn("claude", payload["agents"])
        self.assertIn("cursor", payload["agents"])
        self.assertIn("antigravity", payload["agents"])
        self.assertIn("app quit", payload["non_guarantees"])
        self.assertIn("AppleScript", payload["forbidden"])

    def test_claude_and_cursor_same_task_wake_are_session_only_gaps(self):
        (self.target_home / ".claude").mkdir()
        (self.target_home / ".cursor").mkdir()
        payload = json.loads(self.run_doctor("--json").stdout)
        self.assertEqual(payload["agents"]["claude"]["same_task_native_wake"], "session_loop_only")
        self.assertTrue(payload["agents"]["claude"]["capability_gap"])
        self.assertEqual(payload["agents"]["cursor"]["same_task_native_wake"], "session_loop_only")
        self.assertTrue(payload["agents"]["cursor"]["capability_gap"])

    def test_stop_file_is_reported_for_project(self):
        aru = self.target_home / ".aru"
        aru.mkdir()
        (aru / "factory-loop.stop").write_text(
            json.dumps({"projects": ["/tmp/aru-proj-a"], "stopped_at": "2026-08-14T00:00:00Z"})
        )
        res = self.run_doctor("--json", "--project", "/tmp/aru-proj-a")
        payload = json.loads(res.stdout)
        self.assertTrue(payload["loop_stopped"])
        self.assertEqual(res.returncode, 2)

    def test_relative_project_is_invalid(self):
        res = self.run_doctor("--project", "relative")
        self.assertEqual(res.returncode, 1)
        self.assertIn("absolute", res.stderr)

    def test_version_probe_redacts_secret_shaped_output(self):
        bin_dir = self.target_home / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text("#!/bin/sh\necho 'codex TOKEN=gho_secretvalue'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        (self.target_home / ".codex").mkdir()
        path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        payload = json.loads(self.run_doctor("--json", env={"PATH": path}).stdout)
        self.assertEqual(payload["agents"]["codex"]["version"], "redacted")
        self.assertNotIn("gho_secretvalue", json.dumps(payload))
        self.assertNotIn("gho_secretvalue", self.run_doctor(env={"PATH": path}).stdout)

    def test_app_only_install_is_detected_with_bundle_version(self):
        app = self.target_home / "Applications" / "Antigravity.app" / "Contents"
        app.mkdir(parents=True)
        with (app / "Info.plist").open("wb") as fh:
            plistlib.dump(
                {
                    "CFBundleIdentifier": "com.google.antigravity",
                    "CFBundleShortVersionString": "2.8.1",
                },
                fh,
            )
        payload = json.loads(
            self.run_doctor("--json", env={"PATH": "/usr/bin:/bin"}).stdout
        )
        agent = payload["agents"]["antigravity"]
        self.assertTrue(agent["detected"])
        self.assertEqual(agent["version"], "2.8.1")
        self.assertEqual(agent["app_version"], "2.8.1")
        self.assertTrue(agent["app_path"].endswith("Antigravity.app"))
        self.assertFalse(agent["config_detected"])
        self.assertIsNone(agent["cli_version"])

    def test_default_output_does_not_claim_full_install_diagnosis(self):
        text = self.run_doctor().stdout.lower()
        self.assertIn("not yet diagnosable", text)
        self.assertIn("#34", self.run_doctor().stdout)

    def _write_prompt_only_codex_opt_in(self, project: str) -> str:
        auto_id = "aru-code-loop-" + hashlib.sha256(project.encode()).hexdigest()[:12]
        aru = self.target_home / ".aru"
        aru.mkdir()
        (aru / "native-wake.json").write_text(
            json.dumps(
                {
                    "projects": {
                        project: {
                            "enabled": True,
                            "automation_id": auto_id,
                            "codex": "thread_heartbeat_template",
                            "antigravity": "goal_or_schedule_operator",
                        }
                    }
                }
            )
        )
        prompt_dir = self.target_home / ".codex" / "automations" / auto_id
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "PROMPT.md").write_text(f"wake {project}\n")
        return auto_id

    def test_prompt_only_opt_in_is_prepared_not_enabled(self):
        project = "/tmp/aru-proj-a"
        self._write_prompt_only_codex_opt_in(project)
        payload = json.loads(self.run_doctor("--json", "--project", project).stdout)
        codex = payload["agents"]["codex"]
        self.assertTrue(codex["native_wake_prepared"])
        self.assertFalse(codex["native_wake_configured"])
        self.assertFalse(codex["native_wake_enabled"])
        self.assertEqual(codex["native_wake_evidence"], "prompt_only")
        gravity = payload["agents"]["antigravity"]
        self.assertTrue(gravity["native_wake_prepared"])
        self.assertFalse(gravity["native_wake_configured"])
        self.assertFalse(gravity["native_wake_enabled"])
        self.assertEqual(gravity["native_wake_evidence"], "requested")
        for name in ("claude", "cursor"):
            agent = payload["agents"][name]
            self.assertFalse(agent["native_wake_prepared"])
            self.assertFalse(agent["native_wake_enabled"])

    def test_codex_active_toml_is_enabled_without_claiming_antigravity(self):
        project = "/tmp/aru-proj-a"
        auto_id = self._write_prompt_only_codex_opt_in(project)
        toml = self.target_home / ".codex" / "automations" / auto_id / "automation.toml"
        toml.write_text(f'version = 1\nid = "{auto_id}"\nstatus = "ACTIVE"\n')
        payload = json.loads(self.run_doctor("--json", "--project", project).stdout)
        self.assertTrue(payload["agents"]["codex"]["native_wake_enabled"])
        self.assertEqual(payload["agents"]["codex"]["native_wake_evidence"], "automation_active")
        self.assertFalse(payload["agents"]["antigravity"]["native_wake_enabled"])


if __name__ == "__main__":
    unittest.main()
