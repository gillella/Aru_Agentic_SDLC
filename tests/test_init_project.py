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
            )

        self.assertEqual(result.returncode, 0, result.stderr)

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


if __name__ == "__main__":
    unittest.main()
