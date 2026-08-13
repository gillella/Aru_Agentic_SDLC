import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_local_agent_integrations.sh"
CURSOR_INSTALLER = ROOT / "scripts" / "install_cursor_integration.sh"


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

    def test_cursor_installer_compatibility(self):
        cmd = [
            str(CURSOR_INSTALLER),
            "--target-home", str(self.target_home),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, ARU_SDLC_HOME=str(ROOT)))
        self.assertEqual(res.returncode, 0, f"Cursor installer failed: {res.stderr}")
        self.assertTrue((self.target_home / ".cursor" / "skills" / "run-aru-factory").is_symlink())


if __name__ == "__main__":
    unittest.main()
