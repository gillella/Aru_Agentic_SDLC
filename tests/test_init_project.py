# line-ceiling: 820
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import init_project  # noqa: E402
from init_project import (  # noqa: E402
    CI_GATE_MARKERS,
    render_ci_workflow,
    render_deploy_docs,
    render_deploy_preview_workflow,
    render_gitignore,
    render_release_workflow,
    write_templates,
)


class ProjectBootstrapTests(unittest.TestCase):
    def test_generated_governance_does_not_export_aru_focused_only_exception(self):
        rules = init_project.DEFAULT_AGENTS_TEMPLATE
        self.assertNotIn("ARU CODE FACTORY REPOSITORY RULE", rules)
        self.assertNotIn("focused-only exception", rules)
        self.assertIn("Run `{test_runner}` and confirm all tests pass", rules)

    def test_generated_plan_gate_requires_reuse_audit_with_names_and_locations(self):
        """Generated governance must carry the canonical reuse audit contract."""
        rules = init_project.DEFAULT_AGENTS_TEMPLATE
        self.assertIn("### Existing Utility Reuse Audit", rules)
        self.assertIn("concrete name and exact", rules)
        self.assertIn("search location", rules)
        self.assertIn("bare `None`", rules)
        self.assertIn("is not an audit", rules)

    def test_generated_plan_gate_triggers_on_new_helper_module_or_script(self):
        rules = init_project.DEFAULT_AGENTS_TEMPLATE
        self.assertIn("introduces a new helper function, module, or script", rules)

    def test_generated_governance_uses_guarded_fallback_and_mechanical_merge(self):
        rules = init_project.DEFAULT_AGENTS_TEMPLATE
        normalized = " ".join(rules.split())
        self.assertIn("`create_pr.py` assigns CodeRabbit by default", normalized)
        self.assertIn("new PRs never rotate", rules)
        self.assertIn("Sourcery or CodeAnt", normalized)
        self.assertIn("review:agent", normalized)
        self.assertIn("unavailable, busy, or waiting too long", normalized)
        self.assertIn("never creates a review queue, rotation, fleet, scheduler", normalized)
        self.assertIn("After authoritative exact-head evidence from the assigned reviewer is present", normalized)
        self.assertIn("including the implementation author", rules)
        self.assertIn("merge_pr.py", rules)
        self.assertIn("--expected-head <HEAD_SHA>", rules)
        self.assertIn("Authors must never review their own work", rules)
        self.assertIn("severe merge", rules)
        self.assertIn("merge/close-out failure", rules)
        self.assertNotIn("--require-plan-ack", rules)
        self.assertNotIn("human-acknowledgement", rules)

    def test_generated_governance_preserves_picker_expected_head_on_merge(self):
        rules = init_project.DEFAULT_AGENTS_TEMPLATE
        normalized = " ".join(rules.split()).lower()
        self.assertIn('merge_pr.py" --pr <ID>', rules)
        self.assertIn("--expected-head <HEAD_SHA>", rules)
        self.assertIn("Authors must never review their own work.", rules)
        self.assertIn("when the picker supplies `head_sha`", normalized)

    def test_author_merge_and_emergency_agent_review_are_consistent(self):
        paths = [
            "AGENTS.md", "docs/project_board_workflow.md",
            "skills/run-aru-factory/SKILL.md", "skills/implement-next-issue/SKILL.md",
            "prompts/fleet-worker.md"]
        copies = [init_project.DEFAULT_AGENTS_TEMPLATE] + [
            (ROOT / path).read_text(encoding="utf-8") for path in paths]
        for copy in copies:
            normalized = " ".join(copy.split()).lower()
            self.assertIn("including the implementation author", normalized)
            self.assertIn("coderabbit", normalized)
            self.assertIn("review:agent", normalized)
            self.assertRegex(normalized, r"(external exhaustion|external reviewer|external paths|external review)")

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
        self.assertNotIn("human at high-leverage gates", guidance)
        self.assertNotIn("you merge via", guidance)
        self.assertNotIn("ack design/money", guidance)
        self.assertNotIn("stop for a human", guidance)
        self.assertNotIn("human merges through", guidance)

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
            research = (Path(temp_dir) / ".github" / "ISSUE_TEMPLATE" / "research.yml").read_text()

        self.assertIn('projects: ["octocat/12"]', feature)
        self.assertIn("        touches:", feature)
        self.assertIn("type:research", research)
        self.assertIn("research: ", research)
        self.assertIn("Repository under docs/research/", research)
        self.assertIn("Issue comment only", research)
        self.assertIn("Every factual claim carries a resolvable", research)
        self.assertIn("Every Findings line is marked", research)
        self.assertIn("Citation verification exits 0", research)
        self.assertIn("exact same-date entries", research)
        self.assertIn("--repo-root <consumer-repo-root> <artifact>", research)
        self.assertIn("touches: docs/research/**", research)
        self.assertIn("replace it with issue-comment-only", research)

    def test_governance_labels_include_research(self):
        names = [name for name, _, _ in init_project.GOVERNANCE_LABELS]
        self.assertIn("type:research", names)

    def test_governance_labels_include_needs_human(self):
        labels = {
            name: description
            for name, _color, description in init_project.GOVERNANCE_LABELS
        }
        self.assertEqual(
            labels["needs-human"],
            "Operator must complete; factory agents must not claim",
        )

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

    def test_governance_scripts_written_and_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir)

            check_touches = Path(temp_dir) / ".github" / "scripts" / "check_touches.py"
            check_touches_wf = Path(temp_dir) / ".github" / "workflows" / "check_touches.yml"
            promote_workflow = Path(temp_dir) / ".github" / "workflows" / "promote.yml"
            build_preview = Path(temp_dir) / "scripts" / "build_preview.py"
            smoke_preview = Path(temp_dir) / "scripts" / "smoke_preview.py"

            self.assertTrue(check_touches.is_file())
            self.assertTrue(check_touches_wf.is_file())
            self.assertTrue(promote_workflow.is_file())
            self.assertEqual(
                promote_workflow.read_text(),
                (ROOT / ".github" / "workflows" / "promote.yml").read_text(),
            )
            self.assertIn("repository_dispatch", promote_workflow.read_text())
            self.assertIn("no runnable build movement claimed", promote_workflow.read_text())
            self.assertTrue(build_preview.is_file())
            self.assertEqual(
                build_preview.read_text(),
                (ROOT / "scripts" / "build_preview.py").read_text(),
            )
            self.assertTrue(os.access(build_preview, os.X_OK))
            self.assertTrue(smoke_preview.is_file())
            self.assertEqual(
                smoke_preview.read_text(),
                (ROOT / "scripts" / "smoke_preview.py").read_text(),
            )
            self.assertTrue(os.access(smoke_preview, os.X_OK))

            smoke_scenario = Path(temp_dir) / ".github" / "scenarios" / "smoke.json"
            self.assertTrue(smoke_scenario.is_file())
            self.assertEqual(
                smoke_scenario.read_text(),
                (ROOT / ".github" / "scenarios" / "smoke.json").read_text(),
            )

    def test_generated_governance_omits_legacy_model_reviewer_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir)

            self.assertFalse((Path(temp_dir) / ".github" / "scripts" / "review.py").exists())
            self.assertFalse((Path(temp_dir) / ".github" / "workflows" / "review.yml").exists())
            self.assertFalse((Path(temp_dir) / ".github" / "reviewers.yml").exists())

    def test_generated_governance_has_no_provider_key_review_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir)

            workflow_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((Path(temp_dir) / ".github" / "workflows").glob("*.yml"))
            )
            for forbidden in (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "MISTRAL_API_KEY",
                ".github/scripts/review.py",
            ):
                self.assertNotIn(forbidden, workflow_text)

    def test_check_touches_fails_closed_when_unlinked_or_unreadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir)
            check_touches = Path(temp_dir) / ".github" / "scripts" / "check_touches.py"

            # No Closes link -> Fail closed (exit 1)
            env = {"PR_BODY": "Just a PR body without closes", "PR_HEAD": "feature-branch"}
            res = subprocess.run([sys.executable, str(check_touches)], capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 1)
            self.assertIn("::error:: Fail-closed: No linked issue", res.stderr)

    def test_check_touches_path_allowed_glob_vs_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir)
            check_touches = Path(temp_dir) / ".github" / "scripts" / "check_touches.py"

            # Load helper functions directly from check_touches script
            spec = __import__("importlib.util").util.spec_from_file_location("check_touches", check_touches)
            mod = __import__("importlib.util").util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Glob pattern 'src/*.py' MUST NOT match 'src/config.json'
            touches_glob = ["src/*.py"]
            self.assertTrue(mod.path_allowed("src/main.py", touches_glob))
            self.assertFalse(mod.path_allowed("src/config.json", touches_glob))
            self.assertFalse(mod.path_allowed("src/sub/nested.py", touches_glob))

            # Bare directory pattern 'src/' MUST match subdirectories
            touches_dir = ["src/"]
            self.assertTrue(mod.path_allowed("src/main.py", touches_dir))
            self.assertTrue(mod.path_allowed("src/sub/nested.py", touches_dir))
            self.assertFalse(mod.path_allowed("docs/readme.md", touches_dir))

            # Repository-wide remediation scope covers both root and nested files.
            touches_repo = mod.parse_touches("touches: `**`")
            self.assertEqual(touches_repo, ["**"])
            self.assertTrue(mod.path_allowed("README.md", touches_repo))
            self.assertTrue(mod.path_allowed("deep/path/app.py", touches_repo))


class CiGateTests(unittest.TestCase):
    """The generated CI must fail, not warn.

    A bootstrapped project inherits whatever this renders, so a gate that
    skips is worse than no gate: it reports green and certifies nothing.
    """

    STACKS = (("python", "pytest -q"), ("node", "npm test"), ("go", "go test ./..."))

    def test_generated_consumer_ci_does_not_inherit_aru_checkpoint_schedule(self):
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                parsed = yaml.safe_load(render_ci_workflow(stack, runner))
                steps = parsed["jobs"]["verify"]["steps"]
                tests_step = next(step for step in steps if step.get("name") == "Tests")
                self.assertNotIn("if", tests_step)

    def test_generated_checkout_does_not_persist_credentials(self):
        for stack, runner in self.STACKS:
            with self.subTest(stack=stack):
                parsed = yaml.safe_load(render_ci_workflow(stack, runner))
                checkout = parsed["jobs"]["verify"]["steps"][0]
                self.assertIs(checkout["with"]["persist-credentials"], False)

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
                self.assertIn("fetch-depth: 0", ci)
                self.assertIn("schedule:", ci)
                self.assertIn("workflow_dispatch:", ci)

    def test_pip_audit_covers_pyproject_even_when_requirements_exist(self):
        from init_project import PYTHON_PIP_AUDIT_SCRIPT
        self.assertIn("pip-audit -r pyproject.toml", PYTHON_PIP_AUDIT_SCRIPT)
        self.assertNotIn("audited\" -eq 0 ] && [ -f pyproject.toml ]", PYTHON_PIP_AUDIT_SCRIPT)
        playbook = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("pip-audit -r pyproject.toml", playbook)

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

    def test_generated_workflow_parses_as_yaml(self):
        """The declared YAML verifier parses every generated workflow."""

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
            with patch.dict(os.environ, {"ARU_SDLC_HOME": str(ROOT)}):
                init_project.create_cursor_project_rule(target)

            rule = Path(target) / ".cursor" / "rules" / "aru-agentic-sdlc.mdc"
            self.assertTrue(rule.is_file())
            self.assertIn("alwaysApply: true", rule.read_text())
            self.assertIn("Feature and remediation work", rule.read_text())

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


class DogfoodCiParityTests(unittest.TestCase):
    """This playbook must run the gates it ships to new projects (#86).

    A template that audits deps / scans secrets while the factory itself
    skips them is the dogfooding gap named in ARU-SOFTWARE-FACTORY.md §2.2.
    """

    def test_python_template_carries_every_shared_gate_marker(self):
        python_ci = render_ci_workflow("python", "pytest -q")
        for marker in CI_GATE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, python_ci)

    def test_playbook_ci_is_a_superset_of_python_template_gates(self):
        playbook_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        python_ci = render_ci_workflow("python", "pytest -q")
        for marker in CI_GATE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, python_ci)
                self.assertIn(marker, playbook_ci)

    def test_playbook_ci_gates_do_not_mask_failures(self):
        playbook_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        # Softened forms that turn a red gate green. Match YAML keys / shell
        # idioms, not the words appearing in a comment that forbids them.
        self.assertNotRegex(playbook_ci, r"(?m)^\s*continue-on-error\s*:")
        self.assertNotIn("|| true", playbook_ci)
        self.assertNotIn("|| echo", playbook_ci)

    def test_playbook_ci_names_the_three_dogfood_jobs(self):
        playbook_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        for job in ("secret-scan:", "dependency-audit:", "import-boundaries:"):
            with self.subTest(job=job):
                self.assertIn(job, playbook_ci)

    def test_gitleaks_workflow_declares_pull_request_read(self):
        """The action lists PR commits; missing this permission is a 403, not a leak."""
        playbook_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        python_ci = render_ci_workflow("python", "pytest -q")
        for ci in (playbook_ci, python_ci):
            with self.subTest(ci_len=len(ci)):
                self.assertIn("pull-requests: read", ci)
                self.assertIn("contents: read", ci)

    def test_pip_audit_fails_on_a_known_vulnerable_pin(self):
        """AC: pip-audit must fail the build on a known vulnerability."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            venv = root / "venv"
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                capture_output=True, text=True, check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"could not create venv: {created.stderr}")
            python = venv / ("Scripts/python" if sys.platform == "win32" else "bin/python")
            install = subprocess.run(
                [str(python), "-m", "pip", "install", "-q", "pip-audit"],
                capture_output=True, text=True, check=False,
            )
            if install.returncode != 0:
                self.skipTest(f"could not install pip-audit: {install.stderr}")
            req = root / "requirements.txt"
            req.write_text("jinja2==2.4.1\n")
            audited = subprocess.run(
                [str(python), "-m", "pip_audit", "-r", str(req)],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(
                audited.returncode, 0,
                "pip-audit must fail on jinja2==2.4.1; "
                f"stdout={audited.stdout!r} stderr={audited.stderr!r}",
            )

    def test_gitleaks_refuses_a_planted_dummy_secret(self):
        """AC: a tree containing a planted dummy secret must fail the scan.

        Downloads a pinned gitleaks release into a temp dir so the proof never
        commits a secret into this repository's history and does not depend on
        a host-installed binary or Docker.
        """
        import platform
        import tarfile
        import urllib.request

        system = platform.system().lower()
        machine = platform.machine().lower()
        if system == "linux" and machine in {"x86_64", "amd64"}:
            asset = "gitleaks_8.21.2_linux_x64.tar.gz"
        elif system == "linux" and machine in {"aarch64", "arm64"}:
            asset = "gitleaks_8.21.2_linux_arm64.tar.gz"
        elif system == "darwin" and machine == "arm64":
            asset = "gitleaks_8.21.2_darwin_arm64.tar.gz"
        elif system == "darwin" and machine == "x86_64":
            asset = "gitleaks_8.21.2_darwin_x64.tar.gz"
        else:
            self.skipTest(f"no pinned gitleaks asset for {system}/{machine}")

        url = (
            "https://github.com/gitleaks/gitleaks/releases/download/"
            f"v8.21.2/{asset}"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            tools = Path(temp_dir) / "tools"
            tools.mkdir()
            archive = tools / asset
            try:
                urllib.request.urlretrieve(url, archive)
            except Exception as exc:  # noqa: BLE001 — network is optional in offline runs
                self.skipTest(f"could not download gitleaks: {exc}")

            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tools)
            gitleaks = tools / "gitleaks"
            self.assertTrue(gitleaks.is_file(), f"gitleaks missing from {asset}")
            gitleaks.chmod(0o755)

            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            # Synthetic GitHub PAT shape. Official AWS *EXAMPLE* material is
            # allowlisted by modern gitleaks and would false-pass this proof.
            (repo / "leak.txt").write_text(
                "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD\n"
            )
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "add", "leak.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "plant",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            scanned = subprocess.run(
                [str(gitleaks), "detect", f"--source={repo}", "--no-banner"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(
                scanned.returncode,
                0,
                "gitleaks must fail on a planted AWS example key; "
                f"stdout={scanned.stdout!r} stderr={scanned.stderr!r}",
            )

    def test_deploy_preview_and_release_workflows_are_stack_aware(self):
        python_deploy = render_deploy_preview_workflow("python")
        node_deploy = render_deploy_preview_workflow("typescript")
        go_deploy = render_deploy_preview_workflow("go")

        # Fail-closed checks on missing deploy credentials
        for wf in (python_deploy, node_deploy, go_deploy):
            self.assertIn("Validate deployment credentials", wf)
            self.assertIn("PREVIEW_DEPLOY_TOKEN", wf)
            self.assertIn("Deploy credentials absent", wf)

        # Stack-specific runtime setups
        self.assertIn("actions/setup-python", python_deploy)
        self.assertIn("actions/setup-node", node_deploy)
        self.assertIn("actions/setup-go", go_deploy)

        # Release workflows
        python_release = render_release_workflow("python")
        node_release = render_release_workflow("react")
        go_release = render_release_workflow("go")

        for rwf in (python_release, node_release, go_release):
            self.assertIn("Validate release credentials", rwf)
            self.assertIn("RELEASE_TOKEN", rwf)
            self.assertIn("Release credentials absent", rwf)

        self.assertIn("python -m build", python_release)
        self.assertIn("npm run build", node_release)
        self.assertIn("go build", go_release)

        # Deploy docs point at governed factory skills
        docs = render_deploy_docs("python", "demo-app")
        self.assertIn("deploy-preview", docs)
        self.assertIn("deploy_preview.py", docs)
        self.assertIn("promote.py", docs)
        self.assertIn("Aru_Agentic_SDLC", docs)
        self.assertIn("PREVIEW_DEPLOY_TOKEN", docs)
        self.assertIn("Fail-Closed Gate", docs)

    def test_write_governance_scripts_creates_stack_deploy_and_release_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project.scaffold_directory_structure(temp_dir)
            init_project.write_governance_scripts(temp_dir, stack="node", project_name="node-app")

            deploy_wf = Path(temp_dir) / ".github" / "workflows" / "deploy-preview.yml"
            release_wf = Path(temp_dir) / ".github" / "workflows" / "release.yml"
            deploy_docs = Path(temp_dir) / "docs" / "deploy.md"

            self.assertTrue(deploy_wf.is_file())
            self.assertTrue(release_wf.is_file())
            self.assertTrue(deploy_docs.is_file())

            self.assertIn("actions/setup-node", deploy_wf.read_text())
            self.assertIn("npm run build", release_wf.read_text())
            self.assertIn("deploy_preview.py", deploy_docs.read_text())

    def test_cli_scaffold_creates_stack_pack_workflows_for_node_and_go(self):
        for stack in ("node", "go"):
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
                        "--name", f"stack-{stack}-smoke",
                        "--stack", stack,
                        "--no-remote",
                        "--target-dir", temp_dir,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

                deploy_wf = Path(temp_dir) / ".github" / "workflows" / "deploy-preview.yml"
                release_wf = Path(temp_dir) / ".github" / "workflows" / "release.yml"
                deploy_docs = Path(temp_dir) / "docs" / "deploy.md"

                self.assertTrue(deploy_wf.is_file())
                self.assertTrue(release_wf.is_file())
                self.assertTrue(deploy_docs.is_file())

                if stack == "node":
                    self.assertIn("actions/setup-node", deploy_wf.read_text())
                    self.assertIn("npm run build", release_wf.read_text())
                else:
                    self.assertIn("actions/setup-go", deploy_wf.read_text())
                    self.assertIn("go build", release_wf.read_text())


class LineCeilingBootstrapTests(unittest.TestCase):
    """A bootstrapped project inherits the anti-bloat ratchet (#319).

    Before this, init generated CI for python, node and go with no ceiling step
    at all, so a project this framework created was governed by none of it.
    """

    def _bootstrap(self, stack, runner):
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        with contextlib.redirect_stdout(io.StringIO()):
            init_project.write_governance_scripts(target, stack=stack, project_name="Demo")
            init_project.write_ci_workflow(target, runner, stack)
        return target

    def test_every_stack_gets_the_ceiling_step(self):
        for stack, runner in (("python", "pytest -q"),
                              ("typescript", "npm test"),
                              ("go", "go test ./...")):
            with self.subTest(stack=stack):
                target = self._bootstrap(stack, runner)
                workflow = Path(target, ".github/workflows/ci.yml").read_text(encoding="utf-8")
                self.assertIn("Enforce file line ceilings", workflow)
                self.assertIn("python3 scripts/check_line_ceilings.py", workflow)

    def test_the_guard_is_vendored_verbatim_not_reimplemented(self):
        # A regenerated copy could drift; the same file cannot.
        target = self._bootstrap("python", "pytest -q")
        vendored = Path(target, "scripts/check_line_ceilings.py").read_text(encoding="utf-8")
        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "check_line_ceilings.py").read_text(encoding="utf-8")
        self.assertEqual(vendored, source)

    def test_a_new_project_passes_its_own_guard_immediately(self):
        # No manual baseline step: any marker the source carries is vendored too,
        # which is what keeps the 460-line smoke_preview.py from failing day one.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import check_line_ceilings as guard
        for stack, runner in (("python", "pytest -q"),
                              ("typescript", "npm test"),
                              ("go", "go test ./...")):
            with self.subTest(stack=stack):
                self.assertEqual(guard.check_tree(self._bootstrap(stack, runner)), [])

    def test_the_generated_workflow_is_valid_yaml(self):
        target = self._bootstrap("go", "go test ./...")
        parsed = yaml.safe_load(Path(target, ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        names = [s.get("name") for s in parsed["jobs"]["verify"]["steps"]]
        self.assertIn("Enforce file line ceilings", names)


if __name__ == "__main__":
    unittest.main()
