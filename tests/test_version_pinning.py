#!/usr/bin/env python3
"""test_version_pinning.py — Unit tests for ARU_SDLC_REF version pinning and compatibility checks."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402


class VersionPinningTests(unittest.TestCase):
    def test_parse_semver_major_valid(self):
        self.assertEqual(common.parse_semver_major("v0.1.0"), 0)
        self.assertEqual(common.parse_semver_major("v1.0.0"), 1)
        self.assertEqual(common.parse_semver_major("v2.14.3"), 2)
        self.assertEqual(common.parse_semver_major("10.5.2"), 10)

    def test_parse_semver_major_invalid(self):
        self.assertIsNone(common.parse_semver_major(""))
        self.assertIsNone(common.parse_semver_major("main"))
        self.assertIsNone(common.parse_semver_major(None))

    def test_check_version_compatibility_matching_major(self):
        with patch.dict(os.environ, {"ARU_SDLC_REF": "v1.2.0"}):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.0.0")
                self.assertTrue(res)
                written = "".join(call.args[0] for call in mock_stderr.call_args_list)
                self.assertNotIn("mismatch", written)

    def test_check_version_compatibility_mismatch_major_warns_and_continues(self):
        with patch.dict(os.environ, {"ARU_SDLC_REF": "v2.0.0"}):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.5.0")
                self.assertTrue(res)  # Must return True (never hard-fail)
                written = "".join(call.args[0] for call in mock_stderr.call_args_list)
                self.assertIn("[WARN] Framework version mismatch", written)
                self.assertIn("v2.0.0", written)
                self.assertIn("v1.5.0", written)

    def test_check_version_compatibility_unset_ref(self):
        env_without_ref = dict(os.environ)
        env_without_ref.pop("ARU_SDLC_REF", None)
        with patch.dict(os.environ, env_without_ref, clear=True):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.0.0")
                self.assertTrue(res)
                mock_stderr.assert_not_called()

    def test_foreign_cwd_resolves_framework_version_from_framework_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as foreign_dir:
            # Initialize a foreign git repository with an unrelated tag
            common.run_cmd(["git", "init"], cwd=foreign_dir, check=True)
            common.run_cmd(["git", "config", "user.name", "Test"], cwd=foreign_dir, check=True)
            common.run_cmd(["git", "config", "user.email", "test@example.com"], cwd=foreign_dir, check=True)
            common.run_cmd(["git", "commit", "--allow-empty", "-m", "init"], cwd=foreign_dir, check=True)
            common.run_cmd(["git", "tag", "v9.9.9"], cwd=foreign_dir, check=True)

            # Ensure get_current_framework_version does not adopt v9.9.9 when cwd is foreign_dir
            orig_cwd = os.getcwd()
            try:
                os.chdir(foreign_dir)
                framework_ver = common.get_current_framework_version()
                self.assertNotEqual(framework_ver, "v9.9.9")
            finally:
                os.chdir(orig_cwd)

    def test_clean_target_home_persists_exports(self):
        import shutil
        import subprocess
        import tempfile
        installer = str(ROOT / "scripts" / "install_local_agent_integrations.sh")
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as clean_home:
            common.run_cmd(["git", "init", "-b", "main"], cwd=temp_root, check=True)
            common.run_cmd(["git", "config", "user.name", "Test"], cwd=temp_root, check=True)
            common.run_cmd(["git", "config", "user.email", "test@example.com"], cwd=temp_root, check=True)
            os.makedirs(os.path.join(temp_root, "scripts"), exist_ok=True)
            os.makedirs(os.path.join(temp_root, "skills", "code-review"), exist_ok=True)
            with open(os.path.join(temp_root, "skills", "code-review", "SKILL.md"), "w") as f:
                f.write("---\nname: code-review\n---\n")
            if (ROOT / "templates").exists():
                shutil.copytree(ROOT / "templates", Path(temp_root) / "templates")
            if (ROOT / "commands").exists():
                shutil.copytree(ROOT / "commands", Path(temp_root) / "commands")
            shutil.copy(installer, os.path.join(temp_root, "scripts", "install_local_agent_integrations.sh"))
            shutil.copy(str(ROOT / "scripts" / "install_cursor_integration.sh"), os.path.join(temp_root, "scripts", "install_cursor_integration.sh"))
            common.run_cmd(["git", "add", "."], cwd=temp_root, check=True)
            common.run_cmd(["git", "commit", "-m", "init"], cwd=temp_root, check=True)
            common.run_cmd(["git", "tag", "v1.0.0"], cwd=temp_root, check=True)

            cmd = [
                os.path.join(temp_root, "scripts", "install_local_agent_integrations.sh"),
                "--cursor-only",
                "--aru-home", temp_root,
                "--target-home", clean_home,
            ]
            env = dict(os.environ, ARU_SDLC_REF="v1.0.0", SHELL="/bin/zsh")
            res = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(res.returncode, 0, f"Installer failed: {res.stderr}")

            # Verify that .zshrc was created and exports persisted
            zshrc = Path(clean_home) / ".zshrc"
            self.assertTrue(zshrc.exists())
            content = zshrc.read_text(encoding="utf-8")
            self.assertIn("export ARU_SDLC_HOME=", content)
            self.assertIn('export ARU_SDLC_REF="v1.0.0"', content)


if __name__ == "__main__":
    unittest.main()
