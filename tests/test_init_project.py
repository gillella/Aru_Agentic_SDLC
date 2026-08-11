import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:  # PyYAML is optional: the suite must stay runnable with the stdlib alone.
    import yaml as _yaml  # noqa: F401
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

import init_project  # noqa: E402
from init_project import (  # noqa: E402
    render_ci_workflow,
    render_gitignore,
    write_templates,
)


class ProjectBootstrapTests(unittest.TestCase):
    def test_generated_governance_uses_agent_review_and_mechanical_merge(self):
        rules = init_project.DEFAULT_AGENTS_TEMPLATE

        self.assertIn("distinct agent", rules)
        self.assertIn("including the implementation author", rules)
        self.assertIn("merge_pr.py", rules)
        self.assertIn("must never\nself-review", rules)
        self.assertIn("severe merge", rules)
        self.assertIn("merge/close-out failure", rules)
        self.assertNotIn("--require-plan-ack", rules)
        self.assertNotIn("human-acknowledgement", rules)

    def test_active_factory_guidance_has_no_legacy_human_only_rule(self):
        paths = [
            ROOT / "AGENTS.md",
            ROOT / "prompts" / "fleet-worker.md",
            ROOT / "skills" / "implement-next-issue" / "SKILL.md",
            ROOT / "docs" / "project_board_workflow.md",
            ROOT / "docs" / "ARU-SOFTWARE-FACTORY.md",
            ROOT / "docs" / "Aru-Software-Factory-Future-Improvements.md",
        ]
        guidance = "\n".join(path.read_text().lower() for path in paths)

        self.assertNotIn("needs-human-review", guidance)
        self.assertNotIn("--require-plan-ack", guidance)
        self.assertNotIn("a human merges", guidance)
        self.assertNotIn("fully autonomous merge of money", guidance)

    def test_ci_is_stack_aware_and_does_not_mask_failures(self):
        python_ci = render_ci_workflow("python", "pytest -q")
        node_ci = render_ci_workflow("node", "npm test")
        go_ci = render_ci_workflow("go", "go test ./...")

        self.assertNotIn("|| true", python_ci)
        self.assertNotIn("ruff reported findings", python_ci)
        self.assertIn("actions/setup-node", node_ci)
        self.assertIn("npm test", node_ci)
        self.assertNotIn("actions/setup-python", node_ci)
        self.assertIn("actions/setup-go", go_ci)

    def test_gitignore_is_tailored_to_stack(self):
        node_ignore = render_gitignore("typescript")
        go_ignore = render_gitignore("go")

        self.assertIn("node_modules/", node_ignore)
        self.assertNotIn("__pycache__/", node_ignore)
        self.assertIn("coverage.out", go_ignore)

    def test_issue_forms_include_project_and_touches_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.makedirs(Path(temp_dir) / ".github" / "ISSUE_TEMPLATE")
            write_templates(temp_dir, "octocat/12")
            feature = (Path(temp_dir) / ".github" / "ISSUE_TEMPLATE" / "feature.yml").read_text()

        self.assertIn('projects: ["octocat/12"]', feature)
        self.assertIn("        touches:", feature)

    def test_documented_private_flag_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.update({
                "GIT_AUTHOR_NAME": "Aru SDLC Test",
                "GIT_AUTHOR_EMAIL": "aru-sdlc-test@example.invalid",
                "GIT_COMMITTER_NAME": "Aru SDLC Test",
                "GIT_COMMITTER_EMAIL": "aru-sdlc-test@example.invalid",
            })
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "init_project.py"),
                    "--name", "private-smoke",
                    "--private",
                    "--no-remote",
                    "--target-dir", temp_dir,
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_commit_works_without_git_identity_env(self):
        """Empty HOME / no GIT_* vars must still produce the initial commit."""
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as home:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": home,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.path.join(home, "nonexistent-gitconfig"),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "init_project.py"),
                    "--name", "identity-smoke",
                    "--private",
                    "--no-remote",
                    "--target-dir", temp_dir,
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            log = subprocess.run(
                ["git", "-C", temp_dir, "log", "-1", "--format=%an <%ae>"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(log.returncode, 0, log.stderr)
            self.assertIn("Aru Agentic SDLC", log.stdout)
            self.assertIn("aru-agentic-sdlc@users.noreply.github.com", log.stdout)

    @patch.object(init_project, "run_cmd", return_value=(0, "", ""))
    @patch.object(init_project, "run_gh_json")
    def test_three_project_views_are_configured(self, run_gh_json, run_cmd):
        run_gh_json.return_value = {
            "data": {
                "node": {
                    "views": {
                        "nodes": [
                            {"id": "VIEW_default", "name": "View 1", "layout": "TABLE_LAYOUT"}
                        ]
                    }
                }
            }
        }

        result = init_project.configure_project_views("PROJECT_1")

        self.assertTrue(result)
        self.assertEqual(run_cmd.call_count, 3)
        mutations = "\n".join(call.args[0][4] for call in run_cmd.call_args_list)
        self.assertIn('name:"Kanban"', mutations)
        self.assertIn('name:"Jira-Style Backlog"', mutations)
        self.assertIn('name:"Sprint"', mutations)


class CiGateTests(unittest.TestCase):
    """The generated CI must fail, not warn.

    A bootstrapped project inherits whatever this renders, so a gate that
    skips is worse than no gate: it reports green and certifies nothing.
    """

    STACKS = (("python", "pytest -q"), ("node", "npm test"), ("go", "go test ./..."))

    def test_test_gate_keys_on_source_not_on_tests(self):
        # Keying on tests is self-defeating - a repo with code and no tests
        # takes the skip branch and passes, which is the state being guarded.
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                ci = render_ci_workflow(stack, runner)
                self.assertIn("has_src=", ci)
                self.assertIn("::error::", ci)
                self.assertIn("exit 1", ci)
                self.assertNotIn("becomes mandatory at first source commit", ci)

    def test_gate_recognises_every_layout_its_runner_collects(self):
        """The gate must not be narrower than the test runner it gates.

        A precheck that misses a convention the runner supports fails a
        project whose suite runs perfectly well - the same false-positive
        shape as the redirect scan in #21, and just as disruptive, because
        the build stops before the runner gets a chance to disagree.
        """
        python_ci = render_ci_workflow("python", "pytest -q")
        # pytest's default python_files is test_*.py AND *_test.py.
        self.assertIn("test_*.py", python_ci)
        self.assertIn("*_test.py", python_ci)

        node_ci = render_ci_workflow("node", "npm test")
        # Jest's default testMatch includes __tests__/ directories.
        self.assertIn("__tests__", node_ci)
        self.assertIn("*.test.*", node_ci)
        self.assertIn("*.spec.*", node_ci)

        go_ci = render_ci_workflow("go", "go test ./...")
        self.assertIn("*_test.go", go_ci)

    def test_every_stack_scans_secrets_over_full_history(self):
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                ci = render_ci_workflow(stack, runner)
                self.assertIn("gitleaks/gitleaks-action", ci)
                # depth-1 would hide a secret added then removed later.
                self.assertIn("fetch-depth: 0", ci)

    def test_every_stack_audits_dependencies(self):
        expected = {"python": "pip-audit", "node": "npm audit", "go": "govulncheck"}
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                self.assertIn(expected[stack], render_ci_workflow(stack, runner))

    def test_github_expressions_survive_template_formatting(self):
        # The steps templates go through .format(); a stray brace would eat
        # ${{ secrets.* }} and produce a workflow that silently loses its token.
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                ci = render_ci_workflow(stack, runner)
                self.assertIn("${{ secrets.GITHUB_TOKEN }}", ci)
                self.assertNotIn("{test_runner}", ci)

    def test_generated_workflow_is_structurally_wellformed(self):
        """Always runs. YAML is indentation-sensitive and tabs are illegal in it.

        Kept separate from the PyYAML parse below so this coverage is never
        reported as skipped: a suite that says "skipped" where it actually
        checked something teaches people to ignore skips.
        """
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                ci = render_ci_workflow(stack, runner)
                self.assertNotIn("\t", ci)
                for line in ci.splitlines():
                    if line.strip():
                        indent = len(line) - len(line.lstrip(" "))
                        self.assertEqual(indent % 2, 0, f"odd indent: {line!r}")

    @unittest.skipUnless(_HAS_YAML, "PyYAML not installed")
    def test_generated_workflow_parses_as_yaml(self):
        """The real parse, where the parser is available.

        The suite is deliberately stdlib-only and the repo ships no
        requirements.txt, so PyYAML cannot be a hard dependency without
        changing how CI installs. Verified against PyYAML for all three
        stacks during development; this pins it wherever the lib exists.
        """
        import yaml

        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                parsed = yaml.safe_load(render_ci_workflow(stack, runner))
                steps = parsed["jobs"]["verify"]["steps"]
                self.assertEqual(steps[0]["with"]["fetch-depth"], 0)
                self.assertIn("Secret scan", [s.get("name") for s in steps])


class CursorProjectRuleTests(unittest.TestCase):
    """The don't-clobber guarantee is the whole point of this function.

    A sibling installer shipped a branch that rm -rf'd a user's own skill
    directory while claiming not to, so the equivalent path here is pinned by
    a test rather than by a comment.
    """

    def test_rule_is_written_into_a_fresh_repo(self):
        with tempfile.TemporaryDirectory() as target:
            init_project.create_cursor_project_rule(target)

            rule = Path(target) / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
            self.assertTrue(rule.is_file())
            self.assertIn("alwaysApply: true", rule.read_text())

    def test_an_existing_rule_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as target:
            rule = Path(target) / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
            rule.parent.mkdir(parents=True)
            rule.write_text("MY HAND-EDITED RULE")

            init_project.create_cursor_project_rule(target)

            self.assertEqual(rule.read_text(), "MY HAND-EDITED RULE")

    def test_fallback_is_written_when_the_template_is_missing(self):
        with tempfile.TemporaryDirectory() as target, \
                tempfile.TemporaryDirectory() as empty_home:
            # A partial checkout has no templates/ tree; the rule must still
            # land, since a bootstrapped repo with no governance rule is worse
            # than one with a terse fallback.
            with patch.dict(os.environ, {"ARU_SDLC_HOME": empty_home}):
                init_project.create_cursor_project_rule(target)

            rule = Path(target) / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
            self.assertTrue(rule.is_file())
            self.assertIn("Issue-First Law", rule.read_text())


if __name__ == "__main__":
    unittest.main()
