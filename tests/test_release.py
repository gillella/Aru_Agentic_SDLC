import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402


class SemVerValidationTests(unittest.TestCase):
    def test_valid_semver(self):
        self.assertTrue(release.validate_semver("v0.1.0"))
        self.assertTrue(release.validate_semver("v1.0.0"))
        self.assertTrue(release.validate_semver("v2.14.3"))

    def test_invalid_semver(self):
        self.assertFalse(release.validate_semver("0.1.0"))
        self.assertFalse(release.validate_semver("v1.0"))
        self.assertFalse(release.validate_semver("v1.0.0-beta"))
        self.assertFalse(release.validate_semver("latest"))


class ChangelogGeneratorTests(unittest.TestCase):
    def test_consumed_cli_surface_listed(self):
        content = release.generate_changelog_content()
        self.assertIn("# CHANGELOG", content)
        self.assertIn("## Consumed-CLI Surface", content)
        for script in release.CONSUMED_CLI_SURFACE:
            self.assertIn(f"- `{script}`", content)

    def test_generate_changelog_dry_run(self):
        content = release.write_changelog(dry_run=True)
        self.assertIn("# CHANGELOG", content)


class ReleaseTaggingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name)
        subprocess.run(["git", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=str(self.repo_dir), check=True)

        # Create initial commit
        (self.repo_dir / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo_dir), check=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_tag_invalid_semver(self):
        code = release.create_release_tag("invalid", dry_run=False)
        self.assertEqual(code, 1)

    def test_create_tag_dry_run(self):
        code = release.create_release_tag("v0.1.0", dry_run=True)
        self.assertEqual(code, 0)

    def test_main_cli_flags(self):
        code_check = release.main(["--check"])
        self.assertEqual(code_check, 0)

        code_dry = release.main(["--generate-changelog", "--dry-run"])
        self.assertEqual(code_dry, 0)


if __name__ == "__main__":
    unittest.main()
