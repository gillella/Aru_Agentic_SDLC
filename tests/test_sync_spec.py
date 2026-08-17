import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_claim_tag_extraction_and_update(self):
        doc = self.docs_dir / "spec.md"
        doc.write_text(
            '<!-- claim:sdd-architecture verification="2020-01-01" -->\nSpec content.',
            encoding="utf-8",
        )
        specs = sync_spec.extract_script_cli_specs(self.scripts_dir)
        _, claims = sync_spec.audit_docs_and_skills(self.root, specs)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["id"], "sdd-architecture")
        self.assertEqual(claims[0]["verification"], "2020-01-01")

        updated = sync_spec.update_claim_verifications(self.root, "2026-08-17")
        self.assertEqual(updated, 1)
        content = doc.read_text(encoding="utf-8")
        self.assertIn('<!-- claim:sdd-architecture verification="2026-08-17" -->', content)

    def test_cli_main_exit_codes_and_json(self):
        dummy_script = self.scripts_dir / "service.py"
        dummy_script.write_text(
            'import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--port")\n',
            encoding="utf-8",
        )
        doc = self.docs_dir / "guide.md"
        doc.write_text("`scripts/service.py --port 8080`\n", encoding="utf-8")

        # Clean check
        exit_code = sync_spec.main(["--repo-dir", str(self.root), "--check"])
        self.assertEqual(exit_code, 0)

        # JSON output check
        out = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = out
            sync_spec.main(["--repo-dir", str(self.root), "--json"])
        finally:
            sys.stdout = old_stdout

        data = json.loads(out.getvalue())
        self.assertTrue(data["clean"])
        self.assertEqual(data["status"], "clean")

        # Introduce drift
        doc.write_text("`scripts/service.py --unsupported-flag`\n", encoding="utf-8")
        drift_code = sync_spec.main(["--repo-dir", str(self.root), "--check"])
        self.assertEqual(drift_code, 1)

    def test_merge_pr_dod_check_spec_sync(self):
        pr = {"body": "Closes #242"}
        # Clean repository
        passed, msg = check_spec_sync(pr, repo_dir=ROOT)
        self.assertTrue(passed)
        self.assertIn("synchronized", msg.lower())


if __name__ == "__main__":
    unittest.main()
