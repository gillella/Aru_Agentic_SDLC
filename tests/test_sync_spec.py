import io
import json
import os
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

def _private_helper():
    pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Target file")
    parser.add_argument("-v", "--verbose", action="store_true")
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
        func_names = [f["name"] for f in tool_spec["functions"]]
        self.assertIn("public_action", func_names)
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

    def test_update_refuses_when_drift_present(self):
        dummy_script = self.scripts_dir / "svc.py"
        dummy_script.write_text(
            "import argparse\nparser = argparse.ArgumentParser()\n",
            encoding="utf-8",
        )
        doc = self.docs_dir / "doc.md"
        doc.write_text(
            '<!-- claim:c1 verification="2020-01-01" -->\n`scripts/svc.py --bogus`\n',
            encoding="utf-8",
        )
        exit_code = sync_spec.main(["--repo-dir", str(self.root), "--update"])
        self.assertEqual(exit_code, 1)
        self.assertIn("2020-01-01", doc.read_text())

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
