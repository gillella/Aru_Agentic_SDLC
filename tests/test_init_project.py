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
    CI_GATE_MARKERS,
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
        self.assertIn("Citation verification exits 0", research)
        self.assertIn("Repo code claims are dated", research)
        self.assertIn("verify_citations.py <artifact>", research)
        self.assertIn("touches: docs/research/**", research)
        self.assertIn("replace it with issue-comment-only", research)

    def test_governance_labels_include_research(self):
        names = [name for name, _, _ in init_project.GOVERNANCE_LABELS]
        self.assertIn("type:research", names)

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
            review_py = Path(temp_dir) / ".github" / "scripts" / "review.py"
            review_wf = Path(temp_dir) / ".github" / "workflows" / "review.yml"
            reviewers_yml = Path(temp_dir) / ".github" / "reviewers.yml"

            self.assertTrue(check_touches.is_file())
            self.assertTrue(check_touches_wf.is_file())
            self.assertTrue(review_py.is_file())
            self.assertTrue(review_wf.is_file())
            self.assertTrue(reviewers_yml.is_file())

            # Test review.py degrades to notice without API keys
            env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
            res = subprocess.run(
                [sys.executable, str(review_py)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("::notice::", res.stderr)

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


if __name__ == "__main__":
    unittest.main()
