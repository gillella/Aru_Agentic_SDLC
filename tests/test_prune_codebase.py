import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prune_codebase as pc


class PruneCodebaseTests(unittest.TestCase):
    def test_discover_python_targets_excludes_tests_by_default(self):
        files = [
            Path("scripts/live.py"),
            Path("hooks/guard.py"),
            Path("tests/test_live.py"),
            Path("test_widget.py"),
            Path("widget_test.py"),
            Path("src/pkg/tests/helpers.py"),
            Path("README.md"),
        ]

        self.assertEqual(pc.discover_python_targets(files), ["hooks/guard.py", "scripts/live.py"])
        self.assertEqual(
            pc.discover_python_targets(files, include_tests=True),
            [
                "hooks/guard.py",
                "scripts/live.py",
                "src/pkg/tests/helpers.py",
                "test_widget.py",
                "tests/test_live.py",
                "widget_test.py",
            ],
        )

    def test_parse_vulture_output_preserves_structured_and_unknown_lines(self):
        findings = pc.parse_vulture_output(
            "scripts/a.py:7: unused function 'old' (60% confidence)\ncustom diagnostic\n"
        )

        self.assertEqual(findings[0]["path"], "scripts/a.py")
        self.assertEqual(findings[0]["line"], 7)
        self.assertEqual(findings[0]["confidence"], 60)
        self.assertEqual(findings[1], {"raw": "custom diagnostic"})

    def test_run_vulture_classifies_findings_exit_code(self):
        calls = []

        def runner(command, cwd):
            calls.append((list(command), cwd))
            return 3, "scripts/a.py:7: unused function 'old' (60% confidence)\n", ""

        result = pc.run_vulture(Path("/repo"), ["scripts/a.py"], 60, runner)

        self.assertEqual(result["status"], "findings")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(calls[0][0][:3], [sys.executable, "-m", "vulture"])
        self.assertEqual(calls[0][0][-2:], ["--min-confidence", "60"])

    def test_run_vulture_reports_tool_error(self):
        def runner(command, cwd):
            return 1, "", "No module named vulture"

        result = pc.run_vulture(Path("/repo"), ["scripts/a.py"], 100, runner)

        self.assertEqual(result["status"], "error")
        self.assertIn("No module named vulture", result["error"])

    @patch("prune_codebase.subprocess.run")
    def test_run_command_maps_timeout_to_error(self, run):
        run.side_effect = pc.subprocess.TimeoutExpired(["git", "status"], pc.COMMAND_TIMEOUT_SECONDS)

        code, output, error = pc.run_command(["git", "status"], Path("/repo"))

        self.assertEqual(code, pc.EXIT_ERROR)
        self.assertEqual(output, "")
        self.assertIn("timed out", error)

    def test_run_vulture_with_pinned_tool_detects_dead_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            source = repo / "dead_code.py"
            source.write_text("def abandoned():\n    return 1\n")

            result = pc.run_vulture(repo, [source.name], 60)

            self.assertEqual(result["status"], "findings")
            self.assertTrue(
                any("unused function 'abandoned'" in finding["raw"] for finding in result["findings"])
            )

    def test_unreferenced_prompts_require_a_full_path_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "prompts").mkdir()
            (repo / "docs").mkdir()
            (repo / "prompts" / "used.md").write_text("used")
            (repo / "prompts" / "orphan.md").write_text("orphan")
            (repo / "docs" / "guide.md").write_text("Load prompts/used.md. orphan.md is only a basename.")
            files = [Path("prompts/used.md"), Path("prompts/orphan.md"), Path("docs/guide.md")]

            self.assertEqual(pc.unreferenced_prompts(repo, files), ["prompts/orphan.md"])

    def test_unreferenced_prompts_do_not_follow_tracked_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "prompts").mkdir()
            (repo / "docs").mkdir()
            (repo / "outside.txt").write_text("Load prompts/used.md")
            (repo / "prompts" / "used.md").write_text("prompt")
            (repo / "docs" / "reference.md").symlink_to(repo / "outside.txt")

            unused = pc.unreferenced_prompts(
                repo,
                [Path("prompts/used.md"), Path("docs/reference.md")],
            )

            self.assertEqual(unused, ["prompts/used.md"])

    def test_orphaned_worktrees_excludes_registered_and_retained_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            root = repo / ".worktrees"
            registered = root / "registered"
            orphan = root / "orphan"
            retained = root / ".retained"
            for path in (registered, orphan, retained):
                path.mkdir(parents=True, exist_ok=True)

            def runner(command, cwd):
                return 0, f"worktree {repo}\nHEAD abc\n\nworktree {registered}\nHEAD def\n", ""

            self.assertEqual(pc.orphaned_worktree_dirs(repo, runner), [".worktrees/orphan"])

    def test_scan_repository_aggregates_all_findings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "scripts").mkdir()
            (repo / "prompts").mkdir()
            (repo / ".worktrees" / "orphan").mkdir(parents=True)
            (repo / "scripts" / "a.py").write_text("def old(): pass\n")
            (repo / "prompts" / "unused.md").write_text("hello\n")

            def runner(command, cwd):
                if list(command[:3]) == ["git", "ls-files", "-z"]:
                    return 0, "scripts/a.py\0prompts/unused.md\0", ""
                if list(command[:3]) == [sys.executable, "-m", "vulture"]:
                    return 3, "scripts/a.py:1: unused function 'old' (60% confidence)\n", ""
                if list(command[:3]) == ["git", "worktree", "list"]:
                    return 0, f"worktree {repo}\nHEAD abc\n", ""
                self.fail(f"unexpected command: {command}")

            report = pc.scan_repository(repo, runner=runner)

            self.assertEqual(report["status"], "findings")
            self.assertEqual(report["unreferenced_prompts"], ["prompts/unused.md"])
            self.assertEqual(report["orphaned_worktrees"], [".worktrees/orphan"])
            self.assertEqual(report["vulture"]["findings"][0]["line"], 1)

    def test_scan_repository_fails_closed_when_git_inventory_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def runner(command, cwd):
                return 1, "", "not a git repository"

            report = pc.scan_repository(Path(temp_dir), runner=runner)

            self.assertEqual(report["status"], "error")
            self.assertIn("not a git repository", " ".join(report["errors"]))
            self.assertEqual(report["vulture"]["status"], "skipped")
            self.assertNotIn("Vulture failed", report["errors"])

    def test_main_json_uses_findings_exit_code(self):
        report = {
            "repo_dir": "/repo",
            "vulture": {"status": "findings", "findings": [{"raw": "candidate"}], "targets": []},
            "unreferenced_prompts": [],
            "orphaned_worktrees": [],
            "errors": [],
            "status": "findings",
        }
        output = io.StringIO()
        with patch("prune_codebase.scan_repository", return_value=report), redirect_stdout(output):
            code = pc.main(["--json"])

        self.assertEqual(code, pc.EXIT_FINDINGS)
        self.assertEqual(json.loads(output.getvalue())["status"], "findings")


if __name__ == "__main__":
    unittest.main()
