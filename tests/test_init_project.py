import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import init_project  # noqa: E402
from init_project import (  # noqa: E402
    render_ci_workflow,
    render_gitignore,
    write_templates,
)


class ProjectBootstrapTests(unittest.TestCase):
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
