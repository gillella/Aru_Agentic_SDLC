import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import sync_spec
from merge_pr import check_spec_sync


class TestSyncSpec(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.scripts_dir = self.root / "scripts"
        self.docs_dir = self.root / "docs"
        self.scripts_dir.mkdir()
        self.docs_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ast_argument_extractor(self):
        dummy_script = self.scripts_dir / "tool.py"
        dummy_script.write_text(
            '''
import argparse

def public_action(name, count=1):
    pass

class PublicWorker:
    pass

def _private_helper():
    pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Target file")
    parser.add_argument("-v", "--verbose", action="store_true", default=False)
    parser.add_argument("command", help="Positional command")
''',
            encoding="utf-8",
        )

        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)
        self.assertIn("tool.py", specs)
        tool_spec = specs["tool.py"]
        self.assertIn("--target", tool_spec["flags"])
        self.assertIn("--verbose", tool_spec["flags"])
        self.assertIn("-v", tool_spec["flags"])
        self.assertIn("-h", tool_spec["flags"])
        self.assertIn("command", tool_spec["positionals"])
        self.assertEqual(tool_spec["options"]["--verbose"]["default"], False)
        func_names = [f["name"] for f in tool_spec["functions"]]
        self.assertIn("public_action", func_names)
        self.assertIn("PublicWorker", func_names)
        self.assertNotIn("_private_helper", func_names)

    def test_audit_docs_clean_and_drift(self):
        dummy_script = self.scripts_dir / "deploy.py"
        dummy_script.write_text(
            '''
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--env", required=True)
parser.add_argument("--dry-run", action="store_true")
''',
            encoding="utf-8",
        )
        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)

        # 1. Clean doc
        clean_doc = self.docs_dir / "clean.md"
        clean_doc.write_text(
            "Run `python3 scripts/deploy.py --env prod --dry-run` to test.\n",
            encoding="utf-8",
        )
        drift, claims = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(drift), 0)

        # 2. Drift doc with non-existent flag
        drift_doc = self.docs_dir / "drift.md"
        drift_doc.write_text(
            'Run `python3 "$ARU_SDLC_HOME/scripts/deploy.py" --invalid-flag` to deploy.\n',
            encoding="utf-8",
        )
        drift, _ = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["flag"], "--invalid-flag")
        self.assertEqual(drift[0]["script"], "deploy.py")

    def test_multiline_backslash_continued_commands_in_fences(self):
        dummy_script = self.scripts_dir / "runner.py"
        dummy_script.write_text(
            '''
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--agent", required=True)
parser.add_argument("--family")
''',
            encoding="utf-8",
        )
        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)

        doc = self.docs_dir / "workflow.md"
        doc.write_text(
            '''```bash
python3 "$ARU_SDLC_HOME/scripts/runner.py" \
  --agent gemini-1 \
  --bad-flag value
```''',
            encoding="utf-8",
        )

        drift, _ = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["flag"], "--bad-flag")
        self.assertEqual(drift[0]["script"], "runner.py")

    def test_claim_tag_target_verification(self):
        dummy_script = self.scripts_dir / "auth.py"
        dummy_script.write_text(
            "def login_handler():\n    pass\n",
            encoding="utf-8",
        )
        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)

        # Valid claim
        doc1 = self.docs_dir / "valid_claim.md"
        doc1.write_text(
            '<!-- claim:auth-logic target="scripts/auth.py:login_handler" verification="2026-08-17" -->\n',
            encoding="utf-8",
        )
        drift, claims = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(drift), 0)
        self.assertEqual(len(claims), 1)

        # Invalid claim (missing symbol)
        doc2 = self.docs_dir / "broken_claim.md"
        doc2.write_text(
            '<!-- claim:auth-bad target="scripts/auth.py:missing_fn" verification="2026-08-17" -->\n',
            encoding="utf-8",
        )
        drift, claims = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["type"], "invalid_claim_target")

    def test_structured_param_table_default_drift(self):
        dummy_script = self.scripts_dir / "service.py"
        dummy_script.write_text(
            '''
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--timeout", default=30)
''',
            encoding="utf-8",
        )
        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)

        doc = self.docs_dir / "service.md"
        doc.write_text(
            '''# service.py Documentation
| Flag | Description | Default |
|---|---|---|
| `--timeout` | Connection timeout in seconds | `60` |
''',
            encoding="utf-8",
        )
        drift, _ = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertTrue(any(d.get("type") == "cli_default_drift" for d in drift))

    def test_update_refuses_when_drift_present(self):
        dummy_script = self.scripts_dir / "svc.py"
        dummy_script.write_text(
            "import argparse\nparser = argparse.ArgumentParser()\n",
            encoding="utf-8",
        )
        doc = self.docs_dir / "doc.md"
        doc.write_text(
            '<!-- claim:c1 target="scripts/svc.py" verification="2020-01-01" -->\n`scripts/svc.py --bogus`\n',
            encoding="utf-8",
        )
        exit_code = sync_spec.main(["--repo-dir", str(self.root), "--update"])
        self.assertEqual(exit_code, 1)
        self.assertIn("2020-01-01", doc.read_text())

    def test_sync_shipped_roadmap_idempotent(self):
        roadmap = self.docs_dir / "ARU-SOFTWARE-FACTORY.md"
        roadmap.write_text(
            '''# Factory Plan
## 9. Appendix — shipped roadmap rows (verified 2026-08-15)
| ID | Shipped as | Evidence |
|---|---|---|
| S0.1 | Claim split | `6e07316` |
''',
            encoding="utf-8",
        )
        updated = sync_spec.sync_shipped_roadmap(self.root)
        self.assertIsInstance(updated, int)

    def test_git_head_snapshot_reads_directly_from_sha_and_fails_closed(self):
        # Initialize a git repository
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)

        tool_script = self.scripts_dir / "alpha.py"
        tool_script.write_text(
            "import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument('--alpha')\n",
            encoding="utf-8",
        )
        tool_doc = self.docs_dir / "alpha.md"
        tool_doc.write_text(
            '<!-- claim:alpha target="scripts/alpha.py" verification="2026-08-17" -->\n`scripts/alpha.py --alpha 1`\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "feat: alpha"], check=True)
        head_sha = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

        # Modify local files on disk (dirty working tree)
        tool_script.write_text("broken syntax !!!", encoding="utf-8")
        tool_doc.write_text("`scripts/alpha.py --broken-flag`", encoding="utf-8")

        # Audit with head_sha reads ONLY from head commit, ignoring dirty disk
        cli_specs = sync_spec.extract_script_cli_specs(self.scripts_dir, head_sha=head_sha, repo_dir=self.root)
        self.assertIn("alpha.py", cli_specs)
        drift, claims = sync_spec.audit_docs_and_skills(self.root, cli_specs, head_sha=head_sha)
        self.assertEqual(len(drift), 0)
        self.assertEqual(len(claims), 1)

        # Invalid head fails closed
        drift_bad, _ = sync_spec.audit_docs_and_skills(self.root, cli_specs, head_sha="deadbeef00000000000000000000000000000000")
        self.assertTrue(any(d.get("type") == "git_head_error" for d in drift_bad))

    def test_merge_pr_dod_check_spec_sync_with_head_and_missing_engine(self):
        pr = {"body": "Closes #242", "headRefOid": "abc1234"}
        with patch("merge_pr.run_cmd", return_value=(0, "ok", "")):
            passed, msg = check_spec_sync(pr, repo_dir=str(self.root))
            self.assertTrue(passed)

        with patch("os.path.exists", return_value=False):
            passed, msg = check_spec_sync(pr, repo_dir=str(self.root))
            self.assertFalse(passed)
            self.assertIn("missing", msg.lower())


if __name__ == "__main__":
    unittest.main()
