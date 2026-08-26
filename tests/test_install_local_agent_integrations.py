# line-ceiling: 570
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_local_agent_integrations.sh"
CURSOR_INSTALLER = ROOT / "scripts" / "install_agent_integration.sh"


def codex_auto_id(project: str) -> str:
    return "aru-code-loop-" + hashlib.sha256(project.encode()).hexdigest()[:12]


class InstallLocalAgentIntegrationsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target_home = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_installer(self, *args):
        cmd = [
            str(INSTALLER),
            "--aru-home", str(ROOT),
            "--target-home", str(self.target_home),
        ] + list(args)
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res

    def test_detect_and_install_all_agents(self):
        (self.target_home / ".codex").mkdir(parents=True)
        (self.target_home / ".claude").mkdir(parents=True)
        (self.target_home / ".cursor").mkdir(parents=True)
        (self.target_home / ".gemini" / "antigravity").mkdir(parents=True)

        res = self.run_installer()
        self.assertEqual(res.returncode, 0, f"Installer failed: {res.stderr}")

        # Verify skill symlinks for Codex
        codex_skills = self.target_home / ".codex" / "skills"
        self.assertTrue(codex_skills.exists())
        self.assertTrue((codex_skills / "run-aru-factory").is_symlink())
        self.assertEqual(
            (codex_skills / "run-aru-factory").resolve(),
            (ROOT / "skills" / "run-aru-factory").resolve(),
        )

        # Verify Claude Code
        claude_skills = self.target_home / ".claude" / "skills"
        self.assertTrue((claude_skills / "run-aru-factory").is_symlink())
        self.assertTrue((self.target_home / ".claude" / "commands" / "continue.md").exists())
        self.assertIn("<!-- BEGIN ARU_SDLC_GOVERNANCE -->", (self.target_home / ".claude" / "CLAUDE.md").read_text())

        # Verify Cursor
        cursor_skills = self.target_home / ".cursor" / "skills"
        self.assertTrue((cursor_skills / "run-aru-factory").is_symlink())
        self.assertTrue((self.target_home / ".cursor" / "rules" / "aru-agentic-sdlc.mdc").exists())

        # Verify Antigravity
        ag_skills = self.target_home / ".gemini" / "antigravity" / "skills"
        self.assertTrue((ag_skills / "run-aru-factory").is_symlink())

        # Shared .agents/skills
        self.assertTrue((self.target_home / ".agents" / "skills" / "run-aru-factory").is_symlink())

    def test_dry_run_mode(self):
        (self.target_home / ".codex").mkdir(parents=True)
        res = self.run_installer("--dry-run")
        self.assertEqual(res.returncode, 0)
        self.assertIn("[DRY-RUN]", res.stdout)
        self.assertFalse((self.target_home / ".codex" / "skills").exists())

    def test_check_mode(self):
        (self.target_home / ".codex").mkdir(parents=True)
        # Check before install -> fails
        res1 = self.run_installer("--check")
        self.assertNotEqual(res1.returncode, 0)

        # Install
        self.run_installer()

        # Check after install -> succeeds
        res2 = self.run_installer("--check")
        self.assertEqual(res2.returncode, 0)

    def test_repair_mode(self):
        (self.target_home / ".codex" / "skills").mkdir(parents=True)
        broken_link = self.target_home / ".codex" / "skills" / "run-aru-factory"
        os.symlink("/nonexistent/old/path", broken_link)

        res = self.run_installer("--repair")
        self.assertEqual(res.returncode, 0)
        self.assertTrue(broken_link.is_symlink())
        self.assertEqual(
            broken_link.resolve(),
            (ROOT / "skills" / "run-aru-factory").resolve(),
        )

    def test_single_agent_only(self):
        res = self.run_installer("--codex-only")
        self.assertEqual(res.returncode, 0)
        self.assertTrue((self.target_home / ".codex" / "skills" / "run-aru-factory").is_symlink())
        self.assertFalse((self.target_home / ".claude" / "skills").exists())

    def test_re_run_idempotency(self):
        (self.target_home / ".claude").mkdir(parents=True)
        self.run_installer()
        claude_md = (self.target_home / ".claude" / "CLAUDE.md").read_text()
        count1 = claude_md.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->")
        self.assertEqual(count1, 1)

        # Re-run
        self.run_installer()
        claude_md2 = (self.target_home / ".claude" / "CLAUDE.md").read_text()
        count2 = claude_md2.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->")
        self.assertEqual(count2, 1)

    def test_preserve_user_content(self):
        claude_dir = self.target_home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("# My Personal User Settings\nDO NOT OVERWRITE THIS\n")

        self.run_installer("--claude-only")
        content = claude_md.read_text()
        self.assertIn("# My Personal User Settings", content)
        self.assertIn("DO NOT OVERWRITE THIS", content)
        self.assertIn("<!-- BEGIN ARU_SDLC_GOVERNANCE -->", content)

    def test_preserve_real_directory(self):
        codex_skills = self.target_home / ".codex" / "skills"
        real_dir = codex_skills / "run-aru-factory"
        real_dir.mkdir(parents=True)
        (real_dir / "custom.txt").write_text("user custom skill file")

        self.run_installer("--codex-only")
        self.assertTrue(real_dir.is_symlink())
        backups = list(codex_skills.glob("run-aru-factory.pre-aru.*"))
        self.assertTrue(len(backups) > 0, "Backup directory was not created")
        self.assertTrue((backups[0] / "custom.txt").exists())

    def test_detection_recognizes_cursor_agent_and_agy_binaries(self):
        fake_bin = self.target_home / "bin"
        fake_bin.mkdir()
        for binary in ("cursor-agent", "agy"):
            path = fake_bin / binary
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

        res = subprocess.run(
            [str(INSTALLER), "--aru-home", str(ROOT), "--target-home", str(self.target_home)],
            capture_output=True, text=True,
            env={"HOME": str(self.target_home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.target_home / ".cursor" / "skills" / "run-aru-factory").is_symlink())
        self.assertTrue(
            (self.target_home / ".gemini" / "antigravity" / "skills" / "run-aru-factory").is_symlink()
        )
        self.assertFalse((self.target_home / ".codex" / "skills").exists())
        self.assertFalse((self.target_home / ".claude" / "skills").exists())

    def test_malformed_managed_block_fails_closed(self):
        claude_dir = self.target_home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nKEEP_ME_AFTER_BROKEN_BLOCK\n")

        res = self.run_installer("--claude-only")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("KEEP_ME_AFTER_BROKEN_BLOCK", claude_md.read_text())

    def test_preserve_pre_existing_user_command(self):
        cmd_dir = self.target_home / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        user_cmd = cmd_dir / "continue.md"
        user_cmd.write_text("# User Custom Command\nmy custom code\n")

        res = self.run_installer("--claude-only")
        self.assertEqual(res.returncode, 0)
        self.assertIn("run-aru-factory", user_cmd.read_text())
        backups = list(cmd_dir.glob("continue.md.pre-aru.*"))
        self.assertTrue(len(backups) > 0, "Backup of pre-existing user command was not created")
        self.assertIn("# User Custom Command", backups[0].read_text())

    def test_check_mode_validates_cursor_rule(self):
        (self.target_home / ".cursor").mkdir(parents=True)
        self.run_installer("--cursor-only")
        # Remove cursor rule
        (self.target_home / ".cursor" / "rules" / "aru-agentic-sdlc.mdc").unlink()
        res = self.run_installer("--cursor-only", "--check")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("[CHECK FAILED]", res.stdout)

    def test_zero_agents_detected_without_all_flag(self):
        clean_path = self.target_home / "clean_path"
        clean_path.mkdir(parents=True)
        for tool in ["bash", "sh", "grep", "cat", "mkdir", "ln", "rm", "mktemp", "basename", "dirname", "readlink", "touch", "which", "cut", "awk", "head", "echo"]:
            tool_path = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
            if tool_path and Path(tool_path).exists():
                (clean_path / tool).symlink_to(tool_path)

        cmd = [
            str(INSTALLER),
            "--aru-home", str(ROOT),
            "--target-home", str(self.target_home / "user_home"),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, env={"PATH": str(clean_path), "HOME": str(self.target_home / "user_home")})
        self.assertEqual(res.returncode, 0, f"Installer failed: {res.stderr}\n{res.stdout}")
        user_home = self.target_home / "user_home"
        self.assertFalse((user_home / ".codex").exists())
        self.assertFalse((user_home / ".claude").exists())
        self.assertFalse((user_home / ".cursor").exists())

    def test_governance_block_includes_stop_file_contract(self):
        (self.target_home / ".codex").mkdir(parents=True)
        res = self.run_installer("--codex-only")
        self.assertEqual(res.returncode, 0, res.stderr)
        text = (self.target_home / ".codex" / "instructions.md").read_text()
        self.assertIn("factory-loop.stop", text)
        self.assertIn("thread", text.lower())

    def test_stop_loop_persists_and_survives_reinstall(self):
        project = "/tmp/aru-proj-a"
        (self.target_home / ".codex").mkdir(parents=True)
        self.run_installer("--codex-only")
        res = self.run_installer("--stop-loop", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertIn(project, stop["projects"])
        self.run_installer("--codex-only")
        stop2 = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertIn(project, stop2["projects"])
        res = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())

    def test_enable_native_wake_requires_absolute_project_and_scopes_prompt(self):
        (self.target_home / ".codex").mkdir(parents=True)
        res = self.run_installer("--enable-native-wake")
        self.assertNotEqual(res.returncode, 0)
        res = self.run_installer("--enable-native-wake", "--project", "relative/path")
        self.assertNotEqual(res.returncode, 0)
        project_a = "/tmp/aru-proj-a"
        project_b = "/tmp/aru-proj-b"
        res = self.run_installer("--codex-only", "--enable-native-wake", "--project", project_a)
        self.assertEqual(res.returncode, 0, res.stderr)
        prompt_a = (
            self.target_home / ".codex" / "automations" / codex_auto_id(project_a) / "PROMPT.md"
        ).read_text()
        self.assertIn(project_a, prompt_a)
        self.assertNotIn(project_b, prompt_a)
        res = self.run_installer("--enable-native-wake", "--project", project_b)
        self.assertEqual(res.returncode, 0, res.stderr)
        prompt_b = (
            self.target_home / ".codex" / "automations" / codex_auto_id(project_b) / "PROMPT.md"
        ).read_text()
        self.assertIn(project_b, prompt_b)
        self.assertNotIn(project_a, prompt_b)
        self.assertIn(project_a, prompt_a)
        wake = json.loads((self.target_home / ".aru" / "native-wake.json").read_text())
        self.assertTrue(wake["projects"][project_a]["enabled"])
        self.assertTrue(wake["projects"][project_b]["enabled"])
        self.assertNotEqual(wake["projects"][project_a]["automation_id"], wake["projects"][project_b]["automation_id"])

    def test_enable_native_wake_is_prepared_not_doctor_enabled(self):
        (self.target_home / ".codex").mkdir(parents=True)
        project = "/tmp/aru-proj-a"
        res = self.run_installer("--codex-only", "--enable-native-wake", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(
            (self.target_home / ".codex" / "automations" / codex_auto_id(project) / "automation.toml").exists()
        )
        doctor = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "doctor_local_agent_integrations.py"),
                "--aru-home",
                str(ROOT),
                "--target-home",
                str(self.target_home),
                "--json",
                "--project",
                project,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": "/usr/bin:/bin"},
        )
        payload = json.loads(doctor.stdout)
        self.assertTrue(payload["agents"]["codex"]["native_wake_prepared"])
        self.assertFalse(payload["agents"]["codex"]["native_wake_enabled"])
        self.assertEqual(payload["agents"]["codex"]["native_wake_evidence"], "prompt_only")
        self.assertFalse(payload["agents"]["antigravity"]["native_wake_enabled"])

    def test_stop_pauses_only_that_project_managed_heartbeat(self):
        project = "/tmp/aru-proj-a"
        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        other = self.target_home / ".codex" / "automations" / "india-jobs"
        sibling = self.target_home / ".codex" / "automations" / codex_auto_id("/tmp/aru-proj-b")
        for path in (managed, other, sibling):
            path.mkdir(parents=True)
        managed.joinpath("automation.toml").write_text(
            f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "ACTIVE"\n'
        )
        other.joinpath("automation.toml").write_text(
            'version = 1\nid = "india-jobs"\nstatus = "ACTIVE"\n'
        )
        sibling.joinpath("automation.toml").write_text(
            f'version = 1\nid = "{codex_auto_id("/tmp/aru-proj-b")}"\nstatus = "ACTIVE"\n'
        )
        res = self.run_installer("--stop-loop", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())
        self.assertIn('status = "ACTIVE"', other.joinpath("automation.toml").read_text())
        self.assertIn('status = "ACTIVE"', sibling.joinpath("automation.toml").read_text())

    def test_dry_run_stop_and_wake_do_not_write(self):
        (self.target_home / ".codex").mkdir(parents=True)
        res = self.run_installer("--dry-run", "--stop-loop", "--project", "/tmp/aru-proj-a")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[DRY-RUN]", res.stdout)
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())
        res = self.run_installer("--dry-run", "--enable-native-wake", "--project", "/tmp/aru-proj-a")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.target_home / ".aru" / "native-wake.json").exists())

    def test_disable_native_wake_pauses_matching_heartbeat(self):
        project = "/tmp/aru-proj-a"
        (self.target_home / ".codex").mkdir(parents=True)
        self.run_installer("--enable-native-wake", "--project", project)
        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        managed.joinpath("automation.toml").write_text(
            f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "ACTIVE"\n'
        )
        res = self.run_installer("--disable-native-wake", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())
        wake = json.loads((self.target_home / ".aru" / "native-wake.json").read_text())
        self.assertNotIn(project, wake.get("projects", {}))

    def test_project_resume_does_not_clear_global_stop(self):
        (self.target_home / ".codex").mkdir(parents=True)
        res = self.run_installer("--stop-loop")
        self.assertEqual(res.returncode, 0, res.stderr)
        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertIn("*", stop["projects"])
        res = self.run_installer("--resume-loop", "--project", "/tmp/aru-proj-a")
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("global stop", res.stderr)
        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertIn("*", stop["projects"])

    def test_resume_does_not_reactivate_disabled_wake(self):
        project = "/tmp/aru-proj-a"
        (self.target_home / ".codex").mkdir(parents=True)
        self.run_installer("--enable-native-wake", "--project", project)
        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        managed.joinpath("automation.toml").write_text(
            f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "ACTIVE"\n'
        )
        self.run_installer("--disable-native-wake", "--project", project)
        res = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())
        wake = json.loads((self.target_home / ".aru" / "native-wake.json").read_text())
        self.assertNotIn(project, wake.get("projects", {}))

    def test_antigravity_workflow_and_cursor_stop_command_install(self):
        (self.target_home / ".gemini" / "antigravity").mkdir(parents=True)
        (self.target_home / ".cursor").mkdir(parents=True)
        res = self.run_installer()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(
            (self.target_home / ".gemini" / "antigravity" / "workflows" / "aru-code-loop.md").is_file()
        )
        self.assertTrue((self.target_home / ".cursor" / "commands" / "stop-aru-loop.md").is_file())
        self.assertTrue((self.target_home / ".cursor" / "commands" / "resume-aru-loop.md").is_file())
        self.assertIn(
            "factory-loop.stop",
            (self.target_home / ".cursor" / "commands" / "continue.md").read_text(),
        )

    def test_resume_without_stop_marker_is_silent_success(self):
        res = self.run_installer("--resume-loop", "--project", "/tmp/aru-proj-a")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("No stop requested; continuing", res.stdout)
        self.assertNotIn("no stop marker at", res.stdout)

        # Global resume without stop marker
        res_global = self.run_installer("--resume-loop")
        self.assertEqual(res_global.returncode, 0, res_global.stderr)
        self.assertIn("No stop requested; continuing", res_global.stdout)
        self.assertNotIn("no stop marker at", res_global.stdout)

    def test_stop_loop_supports_bounded_reasons_and_rejects_invalid(self):
        project = "/tmp/aru-proj-a"
        res = self.run_installer("--stop-loop", "--project", project, "--reason", "quota-exhausted")
        self.assertEqual(res.returncode, 0, res.stderr)
        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertEqual(stop.get("reason"), "quota-exhausted")

        # Invalid reason token fails closed
        res_bad = self.run_installer("--stop-loop", "--project", project, "--reason", "unbounded-custom-reason")
        self.assertNotEqual(res_bad.returncode, 0)
        self.assertIn("invalid pause reason", res_bad.stderr)

    def test_cross_project_stop_isolation(self):
        proj_a = "/tmp/aru-proj-a"
        proj_b = "/tmp/aru-proj-b"
        self.run_installer("--stop-loop", "--project", proj_a, "--reason", "maintenance")
        self.run_installer("--stop-loop", "--project", proj_b, "--reason", "error-threshold")

        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertIn(proj_a, stop["projects"])
        self.assertIn(proj_b, stop["projects"])

        # Resume proj_a leaves proj_b stopped
        res = self.run_installer("--resume-loop", "--project", proj_a)
        self.assertEqual(res.returncode, 0, res.stderr)
        stop_after_a = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertNotIn(proj_a, stop_after_a["projects"])
        self.assertIn(proj_b, stop_after_a["projects"])

        # Resume proj_b clears marker completely
        res_b = self.run_installer("--resume-loop", "--project", proj_b)
        self.assertEqual(res_b.returncode, 0, res_b.stderr)
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())

    def test_stop_and_resume_are_idempotent(self):
        project = "/tmp/aru-proj-a"
        self.run_installer("--stop-loop", "--project", project)
        self.run_installer("--stop-loop", "--project", project)
        stop = json.loads((self.target_home / ".aru" / "factory-loop.stop").read_text())
        self.assertEqual(stop["projects"].count(project), 1)

        self.run_installer("--resume-loop", "--project", project)
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())
        res_repeat = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res_repeat.returncode, 0)
        self.assertIn("No stop requested; continuing", res_repeat.stdout)

    def test_simultaneous_stop_and_resume_rejected(self):
        res = self.run_installer("--stop-loop", "--resume-loop")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cannot specify both --stop-loop and --resume-loop", res.stderr)
        self.assertFalse((self.target_home / ".aru" / "factory-loop.stop").exists())

        res_rev = self.run_installer("--resume-loop", "--stop-loop")
        self.assertNotEqual(res_rev.returncode, 0)
        self.assertIn("cannot specify both --stop-loop and --resume-loop", res_rev.stderr)

        res_proj = self.run_installer("--stop-loop", "--resume-loop", "--project", "/tmp/aru-proj-a")
        self.assertNotEqual(res_proj.returncode, 0)
        self.assertIn("cannot specify both --stop-loop and --resume-loop", res_proj.stderr)

        res_wake = self.run_installer("--enable-native-wake", "--disable-native-wake")
        self.assertNotEqual(res_wake.returncode, 0)
        self.assertIn("cannot specify both --enable-native-wake and --disable-native-wake", res_wake.stderr)

        res_wake_rev = self.run_installer("--disable-native-wake", "--enable-native-wake")
        self.assertNotEqual(res_wake_rev.returncode, 0)
        self.assertIn("cannot specify both --enable-native-wake and --disable-native-wake", res_wake_rev.stderr)

    def test_mutually_exclusive_loop_flags_reject_before_any_side_effect(self):
        """Rejection happens in argument parsing, so no marker or heartbeat is touched."""
        project = "/tmp/aru-proj-a"
        self.run_installer("--stop-loop", "--project", project, "--reason", "maintenance")
        stop_file = self.target_home / ".aru" / "factory-loop.stop"
        before = stop_file.read_text()

        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        managed.mkdir(parents=True, exist_ok=True)
        toml = managed / "automation.toml"
        toml.write_text(f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "PAUSED"\n')

        for extra in ([], ["--dry-run"], ["--check"], ["--reason", "operator-requested"],
                      ["--project", project], ["--codex-only"]):
            for order in (["--stop-loop", "--resume-loop"], ["--resume-loop", "--stop-loop"]):
                res = self.run_installer(*(order + extra))
                self.assertEqual(res.returncode, 1, f"{order} {extra}: {res.stdout}")
                self.assertIn("cannot specify both --stop-loop and --resume-loop", res.stderr)
                # A rejected invocation must leave the persisted stop exactly as it was.
                self.assertEqual(stop_file.read_text(), before, f"{order} {extra}")
                self.assertIn('status = "PAUSED"', toml.read_text(), f"{order} {extra}")

        res_bad_reason = self.run_installer("--stop-loop", "--reason", "invalid-reason")
        self.assertNotEqual(res_bad_reason.returncode, 0)
        self.assertIn("invalid pause reason", res_bad_reason.stderr)

    def test_resume_loop_fails_closed_on_corrupt_stop_marker(self):
        """A marker the wrapper cannot parse must not report a successful resume."""
        project = "/tmp/aru-proj-a"
        (self.target_home / ".codex").mkdir(parents=True)
        self.run_installer("--codex-only")
        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        managed.mkdir(parents=True, exist_ok=True)
        toml = managed / "automation.toml"
        stop_file = self.target_home / ".aru" / "factory-loop.stop"
        stop_file.parent.mkdir(parents=True, exist_ok=True)

        # `"abc"` was coerced to per-character tokens and `5` raised a bare
        # TypeError, so a corrupt stop silently un-paused the managed heartbeat.
        for payload in ('{"projects": "abc"}', '{"projects": 5}', '{"projects": {"a": 1}}',
                        '{NOT JSON', '["not", "a", "dict"]'):
            stop_file.write_text(payload)
            toml.write_text(f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "PAUSED"\n')
            res = self.run_installer("--resume-loop", "--project", project)
            self.assertNotEqual(res.returncode, 0, f"{payload}: {res.stdout}")
            self.assertIn("malformed stop marker", res.stderr, payload)
            self.assertNotIn("Traceback", res.stderr, payload)
            self.assertNotIn("No stop requested", res.stdout, payload)
            self.assertEqual(stop_file.read_text(), payload, payload)
            self.assertIn('status = "PAUSED"', toml.read_text(), payload)

    def test_resume_without_active_stop_does_not_reactivate_paused_heartbeat(self):
        project = "/tmp/aru-proj-a"
        other_project = "/tmp/aru-proj-b"
        (self.target_home / ".codex").mkdir(parents=True)
        self.run_installer("--enable-native-wake", "--project", project)
        managed = self.target_home / ".codex" / "automations" / codex_auto_id(project)
        managed.joinpath("automation.toml").write_text(
            f'version = 1\nid = "{codex_auto_id(project)}"\nstatus = "PAUSED"\n'
        )

        # 1. No stop marker exists: project-scoped resume must not reactivate paused heartbeat.
        res = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("No stop requested; continuing", res.stdout)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())

        # 2. No stop marker exists: global resume must not reactivate paused heartbeat.
        res_global = self.run_installer("--resume-loop")
        self.assertEqual(res_global.returncode, 0, res_global.stderr)
        self.assertIn("No stop requested; continuing", res_global.stdout)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())

        # 3. Stop marker exists for a different project: resume for this project must not reactivate heartbeat.
        self.run_installer("--stop-loop", "--project", other_project)
        res_other = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res_other.returncode, 0, res_other.stderr)
        self.assertIn("No stop requested; continuing", res_other.stdout)
        self.assertIn('status = "PAUSED"', managed.joinpath("automation.toml").read_text())

        # 4. When stop marker actually applied to this project, resume DOES reactivate heartbeat.
        self.run_installer("--stop-loop", "--project", project)
        res_active = self.run_installer("--resume-loop", "--project", project)
        self.assertEqual(res_active.returncode, 0, res_active.stderr)
        self.assertIn('status = "ACTIVE"', managed.joinpath("automation.toml").read_text())


if __name__ == "__main__":
    unittest.main()
