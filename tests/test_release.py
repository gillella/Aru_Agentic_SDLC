import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402


SHA = "a" * 40
RUN_URL = "https://github.com/gillella/Aru_Agentic_SDLC/actions/runs/12345"


class SemVerValidationTests(unittest.TestCase):
    def test_valid_semver(self):
        self.assertTrue(release.validate_semver("v0.1.0"))
        self.assertTrue(release.validate_semver("v1.0.0"))
        self.assertTrue(release.validate_semver("v2.14.3"))

    def test_invalid_semver(self):
        self.assertFalse(release.validate_semver("0.1.0"))
        self.assertFalse(release.validate_semver("v1.0"))
        self.assertFalse(release.validate_semver("v1.0.0-beta"))
        self.assertFalse(release.validate_semver("v01.0.0"))
        self.assertFalse(release.validate_semver("v1.00.0"))
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


class ReleaseDocumentationTests(unittest.TestCase):
    def test_release_procedure_documents_exact_checkpoint_commands(self):
        content = (ROOT / "docs" / "releases.md").read_text(encoding="utf-8")
        required_fragments = (
            "python3 scripts/increment_release.py",
            "gh workflow run ci.yml --ref \"$DEFAULT_BRANCH\"",
            "--commit \"$TARGET_COMMIT\"",
            "--json url,headSha,workflowDatabaseId",
            "python3 scripts/release.py \\",
            "--checkpoint-run-url <canonical GitHub Actions run URL>",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)


class ReleaseTaggingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name)
        self.original_root = release.ROOT
        release.ROOT = self.repo_dir

        subprocess.run(["git", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=str(self.repo_dir), check=True)

        # Create initial commit
        (self.repo_dir / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.repo_dir), check=True)

    def tearDown(self):
        release.ROOT = self.original_root
        self.temp_dir.cleanup()

    def test_create_tag_invalid_semver(self):
        code = release.create_release_tag("invalid", dry_run=False)
        self.assertEqual(code, 1)

    def test_create_tag_dry_run(self):
        code = release.create_release_tag("v0.1.0", dry_run=True)
        self.assertEqual(code, 0)

    def test_create_tag_real(self):
        code = release.create_release_tag("v0.1.0", dry_run=False)
        self.assertEqual(code, 0)
        res = subprocess.run(["git", "cat-file", "-t", "v0.1.0"], cwd=str(self.repo_dir), capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "tag")

    @patch.object(release, "run_cmd", return_value=(0, "", ""))
    def test_create_tag_targets_the_validated_commit_explicitly(self, run_cmd):
        code = release.create_release_tag(
            "v0.1.0", target_commit=SHA, dry_run=False
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            run_cmd.call_args.args[0],
            ["git", "tag", "-a", "v0.1.0", "-m", "Release v0.1.0", SHA],
        )

    def test_breaking_major_bump_enforcement(self):
        release.create_release_tag("v0.1.0", dry_run=False)
        # Minor bump with breaking -> fails
        code_minor = release.create_release_tag("v0.2.0", dry_run=False, breaking=True)
        self.assertEqual(code_minor, 1)
        # Major bump with breaking -> succeeds
        code_major = release.create_release_tag("v1.0.0", dry_run=False, breaking=True)
        self.assertEqual(code_major, 0)

    def test_main_cli_flags(self):
        code_check = release.main(["--check"])
        self.assertEqual(code_check, 0)

        code_dry = release.main(["--generate-changelog", "--dry-run"])
        self.assertEqual(code_dry, 0)

    @patch.object(release, "validate_release_checkpoint")
    def test_tag_dry_run_requires_and_validates_checkpoint(self, validate):
        code = release.main([
            "--tag", "v0.1.0", "--commit", SHA,
            "--checkpoint-run-url", RUN_URL, "--dry-run",
        ])
        self.assertEqual(code, 0)
        validate.assert_called_once_with(RUN_URL, SHA)

    @patch.object(release, "validate_release_checkpoint")
    def test_tag_refuses_missing_checkpoint_inputs(self, validate):
        self.assertEqual(release.main(["--tag", "v0.1.0", "--dry-run"]), 1)
        validate.assert_not_called()

    @patch.object(release, "validate_release_checkpoint")
    def test_tag_refuses_failed_checkpoint_validation(self, validate):
        validate.side_effect = release.ReleaseCheckpointError("stale")
        code = release.main([
            "--tag", "v0.1.0", "--commit", SHA,
            "--checkpoint-run-url", RUN_URL, "--dry-run",
        ])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
