"""Parity tests for the agent-integration installer.

The installer used to carry a hand-maintained array of skill names guarded only
by a comment asking the next reader to keep it in step with `skills/` on disk.
That invariant failed silently: `run-aru-factory` — the entrypoint every other
skill is dispatched from — was never added, so no external agent could reach
the door the README and AGENTS.md told people to use (#163).

These tests assert the property that actually failed, rather than the shape of
the fix: after an install, every skill on disk is reachable. They run the real
script against a throwaway HOME so the assertion covers what the installer
links, not what it appears to link on reading.

This issue changes the legacy Cursor installer specifically. The newer
multi-agent installer has a separate contract and test suite, so selecting the
longest matching filename would silently test the wrong program.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"


def installer_path():
    """The vendor-neutral agent installer (renamed from install_cursor_integration.sh)."""
    path = ROOT / "scripts" / "install_agent_integration.sh"
    if not path.is_file():
        raise AssertionError("scripts/install_agent_integration.sh is missing")
    return path


def skills_on_disk():
    return {d.name for d in SKILLS_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()}


def run_installer(home, script=None, cwd=None):
    """Runs the installer with HOME redirected at a scratch directory.

    Every path the script writes is HOME-relative (~/.cursor, ~/.agents,
    ~/.zshrc, ~/.zprofile), so overriding HOME fully contains it. Without that
    containment this test would rewrite the developer's own shell profile.
    """
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(
        ["bash", str(script or installer_path())],
        env=env,
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
    )


def forensics(result, script, home):
    """Everything needed to diagnose a failure from CI logs alone.

    These tests passed on macOS/bash-3.2 and failed on Ubuntu/bash-5 with
    assertion text that did not distinguish "wrong script ran" from "script
    ran and did nothing". Re-running CI to add print statements costs a full
    round trip per guess, so every failure message carries the evidence.
    """
    listing = []
    for sub in (".cursor/skills", ".agents/skills"):
        path = home / sub
        names = sorted(p.name for p in path.iterdir()) if path.is_dir() else "<absent>"
        listing.append(f"    {sub}: {names}")
    return (
        f"\n  script:  {script}"
        f"\n  bash:    {subprocess.run(['bash','--version'],capture_output=True,text=True).stdout.splitlines()[0]}"
        f"\n  rc:      {result.returncode}"
        f"\n  stdout:  {result.stdout.strip()[-800:]!r}"
        f"\n  stderr:  {result.stderr.strip()[-800:]!r}"
        f"\n  HOME contents:\n" + "\n".join(listing)
    )


class InstallerParityTest(unittest.TestCase):
    def test_default_all_and_explicit_single_agent_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            all_home = root / "all"
            explicit_all_home = root / "explicit-all"
            cursor_home = root / "cursor"
            clean_env = dict(os.environ, PATH="/usr/bin:/bin")

            all_result = subprocess.run(
                ["bash", str(installer_path()), "--target-home", str(all_home)],
                env=dict(clean_env, HOME=str(all_home)), capture_output=True, text=True,
            )
            self.assertEqual(all_result.returncode, 0, all_result.stderr)
            for path in (
                ".codex/skills", ".claude/skills", ".cursor/skills",
                ".gemini/antigravity/skills",
            ):
                self.assertTrue((all_home / path / "run-aru-factory").is_symlink(), path)
            self.assertNotIn("not requested/detected", all_result.stdout)

            explicit_all_result = subprocess.run(
                ["bash", str(installer_path()), "--agent", "all",
                 "--target-home", str(explicit_all_home)],
                env=dict(clean_env, HOME=str(explicit_all_home)),
                capture_output=True, text=True,
            )
            self.assertEqual(
                explicit_all_result.returncode, 0, explicit_all_result.stderr,
            )
            for path in (
                ".codex/skills", ".claude/skills", ".cursor/skills",
                ".gemini/antigravity/skills",
            ):
                self.assertTrue(
                    (explicit_all_home / path / "run-aru-factory").is_symlink(),
                    path,
                )
            self.assertNotIn(
                "not requested/detected", explicit_all_result.stdout,
            )

            cursor_result = subprocess.run(
                ["bash", str(installer_path()), "--agent", "cursor",
                 "--target-home", str(cursor_home)],
                env=dict(clean_env, HOME=str(cursor_home)), capture_output=True, text=True,
            )
            self.assertEqual(cursor_result.returncode, 0, cursor_result.stderr)
            self.assertTrue((cursor_home / ".cursor/skills/run-aru-factory").is_symlink())
            for path in (".codex/skills", ".claude/skills", ".gemini/antigravity/skills"):
                self.assertFalse((cursor_home / path).exists(), path)

    def test_every_skill_on_disk_is_installed(self):
        """The regression that shipped: a skill exists but is unreachable."""
        expected = skills_on_disk()
        self.assertIn(
            "run-aru-factory",
            expected,
            "the entrypoint skill is missing from skills/ entirely",
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            script = installer_path()
            result = run_installer(home)
            self.assertEqual(result.returncode, 0, forensics(result, script, home))

            for sub in (".cursor/skills", ".agents/skills"):
                dest = home / sub
                installed = {d.name for d in dest.iterdir()} if dest.is_dir() else set()
                self.assertEqual(
                    expected - installed,
                    set(),
                    f"skills on disk but not installed into {sub}: "
                    f"{sorted(expected - installed)}" + forensics(result, script, home),
                )

    def test_installed_skills_resolve_to_readable_procedures(self):
        """A dangling symlink installs a name, not a usable procedure."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = run_installer(home)
            self.assertEqual(result.returncode, 0, result.stderr)

            for sub in (".cursor/skills", ".agents/skills"):
                dest = home / sub
                for name in skills_on_disk():
                    self.assertTrue(
                        (dest / name / "SKILL.md").is_file(),
                        f"{name} installed into {sub} but its SKILL.md does not resolve",
                    )

    def test_no_hardcoded_skill_name_list(self):
        """Guards the fix itself: a reintroduced literal list rots the same way."""
        text = installer_path().read_text(encoding="utf-8")
        for name in skills_on_disk():
            self.assertNotIn(
                f"\n  {name}\n",
                text,
                f"{name} appears as a hardcoded array entry; the skill list "
                f"must be derived from skills/ on disk",
            )


class InstallerRejectionTest(unittest.TestCase):
    """A directory under skills/ that defines no procedure is an error.

    Silently skipping it would recreate the original bug in a new form: the
    skill looks installed to anyone reading skills/, but no agent can reach it.
    Built against a synthetic tree because the real repo has no malformed skill
    and should not grow one to satisfy a test.
    """

    def _fake_home(self, tmp):
        fake = Path(tmp) / "sdlc"
        (fake / "scripts").mkdir(parents=True)
        shutil.copy(installer_path(), fake / "scripts" / installer_path().name)
        (fake / "skills" / "good").mkdir(parents=True)
        (fake / "skills" / "good" / "SKILL.md").write_text("# good\n", encoding="utf-8")
        return fake

    def test_skill_directory_without_skill_md_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_home(tmp)
            (fake / "skills" / "malformed").mkdir()

            home = Path(tmp) / "home"
            home.mkdir()
            script = fake / "scripts" / installer_path().name
            result = run_installer(home, script=script)

            self.assertNotEqual(
                result.returncode,
                0,
                "installer accepted a skill directory with no SKILL.md"
                + forensics(result, script, home),
            )
            self.assertIn("SKILL.md", result.stderr, forensics(result, script, home))
            for sub in (".cursor/skills", ".agents/skills"):
                self.assertFalse(
                    (home / sub / "good").exists(),
                    f"installer linked skills into {sub} before validating the whole set",
                )

    def test_unreadable_skill_md_aborts_before_linking(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_home(tmp)
            manifest = fake / "skills" / "unreadable" / "SKILL.md"
            manifest.parent.mkdir()
            manifest.write_text("# unreadable\n", encoding="utf-8")
            manifest.chmod(0)

            home = Path(tmp) / "home"
            home.mkdir()
            script = fake / "scripts" / installer_path().name
            try:
                result = run_installer(home, script=script)
            finally:
                manifest.chmod(0o600)

            self.assertNotEqual(
                result.returncode,
                0,
                "installer accepted an unreadable SKILL.md"
                + forensics(result, script, home),
            )
            self.assertIn("readable SKILL.md", result.stderr, forensics(result, script, home))
            for sub in (".cursor/skills", ".agents/skills"):
                self.assertFalse(
                    (home / sub / "good").exists(),
                    f"installer linked skills into {sub} before validating the whole set",
                )

    def test_empty_skills_tree_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "sdlc"
            (fake / "scripts").mkdir(parents=True)
            shutil.copy(installer_path(), fake / "scripts" / installer_path().name)
            (fake / "skills").mkdir()

            home = Path(tmp) / "home"
            home.mkdir()
            script = fake / "scripts" / installer_path().name
            result = run_installer(home, script=script)

            self.assertNotEqual(
                result.returncode,
                0,
                "installer accepted an empty skills tree" + forensics(result, script, home),
            )
            self.assertIn("no skills found", result.stderr, forensics(result, script, home))


class LegacyShimTest(unittest.TestCase):
    def test_cursor_installer_name_is_a_deprecated_shim(self):
        """The old name must keep working for one release and point forward."""
        shim = ROOT / "scripts" / "install_cursor_integration.sh"
        self.assertTrue(shim.is_file(), "install_cursor_integration.sh shim is missing")
        text = shim.read_text(encoding="utf-8")
        self.assertIn("deprecated", text)
        self.assertIn("install_agent_integration.sh", text)


if __name__ == "__main__":
    unittest.main()
