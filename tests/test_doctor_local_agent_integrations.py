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
        merged["PATH"] = "/usr/bin:/bin"
        if env:
            merged.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, env=merged)

    def test_json_reports_full_install_diagnosis(self):
        payload = json.loads(self.run_doctor("--json").stdout)
        self.assertEqual(payload["install_diagnosis"], "complete")
        self.assertFalse(payload["issue_34"])
        self.assertIn(payload["status"], {"healthy", "degraded", "invalid"})
        self.assertIn("checks", payload)
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

    def test_default_output_reports_status_and_repairs(self):
        text = self.run_doctor().stdout.lower()
        self.assertIn("status=", text)
        self.assertNotIn("not yet diagnosable", text)
        self.assertIn("failed checks", text)

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

    def _skill_names(self):
        root = ROOT / "skills"
        return [
            child.name for child in sorted(root.iterdir())
            if child.is_dir() and (child / "SKILL.md").is_file()
        ]

    def _link_skills(self, rel_dir):
        dest = self.target_home / rel_dir
        dest.mkdir(parents=True, exist_ok=True)
        for name in self._skill_names():
            (dest / name).symlink_to(ROOT / "skills" / name)

    def _write_governance(self, rel):
        path = self.target_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n"
            "run-aru-factory\n"
            "$HOME/.aru/factory-loop.stop\n"
            "<!-- END ARU_SDLC_GOVERNANCE -->\n"
        )

    def _write_surface(self, rel):
        path = self.target_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("run-aru-factory loop\n")

    def _install_healthy_cursor(self):
        (self.target_home / ".cursor").mkdir()
        self._link_skills(".cursor/skills")
        self._link_skills(".agents/skills")
        self._write_governance(".cursor/user-rules-aru-agentic-sdlc.md")
        self._write_surface(".cursor/commands/run-aru-factory.md")
        rule = self.target_home / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text("alwaysApply: true\n")

    def _isolated_path(self, extra_bin=None):
        path = extra_bin or str(self.target_home / "bin")
        return path + os.pathsep + "/usr/bin:/bin"

    def _fake_bin(self, name, body):
        bin_dir = self.target_home / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / name
        fake.write_text("#!/bin/sh\n" + body)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return str(bin_dir)

    def test_missing_agent_is_degraded(self):
        res = self.run_doctor("--json", env={"PATH": "/usr/bin:/bin"})
        payload = json.loads(res.stdout)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(res.returncode, 2)
        ids = [item["id"] for item in payload["checks"] if not item["ok"]]
        self.assertIn("agents_detected", ids)

    def test_missing_link_is_degraded(self):
        (self.target_home / ".cursor").mkdir()
        self._link_skills(".agents/skills")
        payload = json.loads(self.run_doctor("--json").stdout)
        missing = [
            item for item in payload["checks"]
            if item["id"].startswith("skill_missing:")
        ]
        self.assertTrue(missing)
        self.assertEqual(payload["status"], "degraded")
        self.assertIn("install_local_agent_integrations.sh", missing[0]["repair"])

    def test_stale_link_is_degraded(self):
        (self.target_home / ".cursor").mkdir()
        dest = self.target_home / ".cursor" / "skills"
        dest.mkdir(parents=True)
        other = self.target_home / "other-skill"
        other.mkdir()
        (other / "SKILL.md").write_text("nope\n")
        (dest / "run-aru-factory").symlink_to(other)
        self._link_skills(".agents/skills")
        payload = json.loads(self.run_doctor("--json").stdout)
        stale = [
            item for item in payload["checks"]
            if item["id"].startswith("skill_stale:")
        ]
        self.assertTrue(stale)
        self.assertEqual(payload["status"], "degraded")

    def test_conflicting_path_is_invalid(self):
        (self.target_home / ".cursor").mkdir()
        dest = self.target_home / ".cursor" / "skills" / "run-aru-factory"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("copied\n")
        self._link_skills(".agents/skills")
        res = self.run_doctor("--json")
        payload = json.loads(res.stdout)
        conflicts = [
            item for item in payload["checks"]
            if item["id"].startswith("skill_conflict:")
        ]
        self.assertTrue(conflicts)
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(res.returncode, 1)

    def test_healthy_cursor_install_has_no_skill_failures(self):
        self._install_healthy_cursor()
        payload = json.loads(self.run_doctor("--json").stdout)
        skill_fails = [
            item for item in payload["checks"]
            if not item["ok"] and item["id"].startswith("skill_")
        ]
        self.assertEqual(skill_fails, [])
        self.assertTrue(payload["install"]["agents"]["cursor"]["detected"])

    def test_absent_board_is_degraded(self):
        repo = self.target_home / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/demo.git"],
            cwd=repo, check=True, capture_output=True,
        )
        (repo / "AGENTS.md").write_text("# Issue-First Law\n")
        (repo / ".worktrees").mkdir()
        git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True
        ).strip()
        hook_root = Path(git_dir) if Path(git_dir).is_absolute() else repo / git_dir
        (hook_root / "hooks").mkdir(parents=True, exist_ok=True)
        hook = hook_root / "hooks" / "pre-push"
        hook.write_text("Aru_Agentic_SDLC pre-push\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        self._fake_bin(
            "gh",
            'if [ "$1" = "auth" ]; then echo "Logged in to github.com"; exit 0; fi\n'
            'echo \'{"data":{"repository":{"projectsV2":{"nodes":[]}}}}\'\n',
        )
        self._install_healthy_cursor()
        res = self.run_doctor(
            "--json", "--project", str(repo),
            env={"PATH": self._isolated_path(), "GH_CONFIG_DIR": str(self.target_home / "gh")},
        )
        payload = json.loads(res.stdout)
        board = [item for item in payload["checks"] if item["id"] == "board"]
        self.assertTrue(board)
        self.assertFalse(board[0]["ok"])
        self.assertEqual(board[0]["severity"], "degraded")
        self.assertEqual(payload["repository"]["board"]["state"], "absent")

    def test_unauthenticated_repo_board_is_invalid(self):
        repo = self.target_home / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        (repo / "AGENTS.md").write_text("# Issue-First Law\n")
        self._fake_bin("gh", "echo unauthenticated >&2; exit 1\n")
        self._install_healthy_cursor()
        res = self.run_doctor(
            "--json", "--project", str(repo),
            env={"PATH": self._isolated_path(), "GH_CONFIG_DIR": str(self.target_home / "gh")},
        )
        payload = json.loads(res.stdout)
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(res.returncode, 1)
        board = [item for item in payload["checks"] if item["id"] == "board"]
        self.assertEqual(board[0]["severity"], "invalid")
        self.assertNotIn("TOKEN", res.stdout)
        self.assertNotIn("gho_", res.stdout)

    def test_origin_userinfo_is_stripped_from_remote(self):
        repo = self.target_home / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            [
                "git", "remote", "add", "origin",
                "https://alice:ghp_secretvalue@github.com/acme/demo.git",
            ],
            cwd=repo, check=True, capture_output=True,
        )
        (repo / "AGENTS.md").write_text("# Issue-First Law\n")
        self._fake_bin(
            "gh",
            'if [ "$1" = "auth" ]; then echo "Logged in to github.com"; exit 0; fi\n'
            'echo \'{"data":{"repository":{"projectsV2":{"nodes":[]}}}}\'\n',
        )
        self._install_healthy_cursor()
        res = self.run_doctor(
            "--json", "--project", str(repo),
            env={"PATH": self._isolated_path(), "GH_CONFIG_DIR": str(self.target_home / "gh")},
        )
        self.assertNotIn("ghp_secretvalue", res.stdout)
        self.assertNotIn("alice:ghp_", res.stdout)
        payload = json.loads(res.stdout)
        remote = payload["repository"]["remote"]
        self.assertIn("github.com/acme/demo", remote)
        self.assertNotIn("ghp_", remote)

    def test_relative_skill_link_is_ok(self):
        self._install_healthy_cursor()
        dest = self.target_home / ".cursor" / "skills" / "run-aru-factory"
        dest.unlink()
        expected = (ROOT / "skills" / "run-aru-factory").resolve()
        dest.symlink_to(os.path.relpath(expected, dest.parent.resolve()))
        payload = json.loads(self.run_doctor("--json").stdout)
        stale = [
            item for item in payload["checks"]
            if item["id"].startswith("skill_stale:")
        ]
        self.assertEqual(stale, [])

    def test_missing_cursor_rule_file_is_degraded(self):
        self._install_healthy_cursor()
        rule = self.target_home / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
        rule.unlink()
        payload = json.loads(self.run_doctor("--json").stdout)
        missing = [
            item for item in payload["checks"]
            if item["id"].startswith("required_file:")
        ]
        self.assertTrue(missing)
        self.assertEqual(payload["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
