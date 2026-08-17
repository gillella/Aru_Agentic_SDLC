import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_docs.py"

sys.path.insert(0, str(ROOT / "scripts"))

import check_docs  # noqa: E402

# A fake `gh` so the issue-state check can be exercised without the network.
# States come from a JSON file the test writes; an entry of null makes the
# lookup fail, which is how the fail-closed path is exercised.
FAKE_GH = """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
states = json.load(open({states!r}))
if args[:2] == ["repo", "view"]:
    print("owner/repo")
    sys.exit(0)
if args[:1] == ["api"]:
    number = args[1].rsplit("/", 1)[-1]
    entry = states.get(number)
    if entry is None:
        sys.stderr.write("gh: HTTP 404: Not Found\\n")
        sys.exit(1)
    print(json.dumps(entry))
    sys.exit(0)
sys.stderr.write("unexpected gh invocation: %s\\n" % args)
sys.exit(1)
"""


class DocCheckFixture:
    """A throwaway repository with a docs/ tree and real source files."""

    def __init__(self, tmp: Path):
        self.root = tmp
        (self.root / "docs").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "hooks").mkdir()
        self.write("scripts/sample.py", "def one():\n    return 1\n\n\ndef two():\n    return 2\n")

    def write(self, relpath: str, content: str) -> Path:
        target = self.root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def doc(self, name: str, content: str) -> Path:
        return self.write(f"docs/{name}", textwrap.dedent(content).lstrip("\n"))


class DocFreshnessTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = DocCheckFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def findings(self, offline=True, repo=None):
        return check_docs.check_documents(
            self.fixture.root, offline=offline, repo=repo
        )

    def messages(self, **kwargs):
        return [f.render() for f in self.findings(**kwargs)]

    def install_fake_gh(self, states):
        """Put a `gh` on PATH that answers from `states` and return the env."""
        bin_dir = self.fixture.root / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        states_path = self.fixture.root / "states.json"
        states_path.write_text(json.dumps(states), encoding="utf-8")
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH.format(states=str(states_path)), encoding="utf-8")
        gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        return env


class PathReferenceTests(DocFreshnessTestCase):
    def test_known_good_document_produces_no_findings(self):
        self.fixture.doc(
            "good.md",
            """
            The picker lives in `scripts/sample.py`, and line
            `scripts/sample.py:5` returns two.
            """,
        )
        self.assertEqual(self.messages(), [])

    def test_missing_path_is_reported_with_file_and_line(self):
        self.fixture.doc(
            "drifted.md",
            """
            Intro paragraph.
            The helper is `scripts/renamed_away.py` now.
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("docs/drifted.md:2:", messages[0])
        self.assertIn("scripts/renamed_away.py", messages[0])
        self.assertIn("does not exist", messages[0])

    def test_line_citation_past_end_of_file_is_reported(self):
        self.fixture.doc("cite.md", "See `scripts/sample.py:900` for details.\n")
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("has 6 lines", messages[0])

    def test_line_range_inside_the_file_is_accepted(self):
        self.fixture.doc("range.md", "See `scripts/sample.py:1-6`.\n")
        self.assertEqual(self.messages(), [])

    def test_reversed_line_range_is_rejected(self):
        # Comparing only the upper bound would let `:900-1` through because
        # line 1 exists.
        self.fixture.doc("range.md", "See `scripts/sample.py:900-1`.\n")
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("not a valid line range", messages[0])

    def test_zero_line_citation_is_rejected(self):
        self.fixture.doc("range.md", "See `scripts/sample.py:0`.\n")
        self.assertIn("not a valid line range", self.messages()[0])

    def test_traversal_out_of_the_repository_is_rejected(self):
        outside = self.fixture.root.parent / "outside.conf"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        self.fixture.doc("escape.md", "See `scripts/../../outside.conf`.\n")
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("outside the repository", messages[0])

    def test_missing_directory_reference_is_reported(self):
        self.fixture.doc("dir.md", "Worktrees live under `scripts/nowhere/`.\n")
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("directory `scripts/nowhere/`", messages[0])

    def test_branch_names_are_not_treated_as_paths(self):
        # docs/30-current-state-gap-analysis is a branch name in this repo's
        # own audit document. Extensionless tokens must stay out of scope.
        self.fixture.doc("branch.md", "Branch `docs/30-current-state-gap-analysis` was wrong.\n")
        self.assertEqual(self.messages(), [])

    def test_templates_globs_and_unknown_roots_are_ignored(self):
        self.fixture.doc(
            "templates.md",
            """
            Edit `.worktrees/<branch>/thing.py`, match `.github/workflows/*`,
            and see `path/to/file.py` in the illustration.
            """,
        )
        self.assertEqual(self.messages(), [])

    def test_framework_home_prefix_is_resolved_against_the_repo(self):
        self.fixture.doc("prefix.md", "Run `$ARU_SDLC_HOME/scripts/sample.py`.\n")
        self.assertEqual(self.messages(), [])

    def test_framework_home_prefix_still_catches_a_missing_file(self):
        self.fixture.doc("prefix.md", "Run `$ARU_SDLC_HOME/scripts/gone.py`.\n")
        self.assertEqual(len(self.messages()), 1)

    def test_paths_inside_fenced_blocks_are_out_of_scope(self):
        self.fixture.doc(
            "fenced.md",
            """
            Example:

            ```bash
            python3 `scripts/not_real.py` --flag
            ```
            """,
        )
        self.assertEqual(self.messages(), [])


class IssueStateTests(DocFreshnessTestCase):
    def resolver(self, states):
        self.install_fake_gh(states)
        return check_docs.IssueStateResolver(self.fixture.root, repo="owner/repo")

    def run_with_states(self, states):
        env = self.install_fake_gh(states)
        return subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(self.fixture.root), "--repo", "owner/repo"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_matching_state_passes(self):
        self.fixture.doc("state.md", "Consolidated by #78 (closed).\n")
        result = self.run_with_states({"78": {"state": "closed"}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_contradicted_state_fails_and_names_the_contradiction(self):
        self.fixture.doc(
            "state.md",
            """
            Background paragraph.
            The gap is tracked in #50 (open).
            """,
        )
        result = self.run_with_states({"50": {"state": "closed"}})
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/state.md:2:", result.stderr)
        self.assertIn("cites #50 as open, but it is closed", result.stderr)

    def test_annotation_form_is_checked(self):
        self.fixture.doc("state.md", "Still tracked. <!-- doc-check: issue 91 open -->\n")
        result = self.run_with_states({"91": {"state": "closed"}})
        self.assertEqual(result.returncode, 1)
        self.assertIn("cites #91 as open, but it is closed", result.stderr)

    def test_merged_pull_request_satisfies_a_closed_claim(self):
        self.fixture.doc("state.md", "Landed in #215 (closed).\n")
        result = self.run_with_states(
            {"215": {"state": "closed", "pull_request": {"merged_at": "2026-08-01T00:00:00Z"}}}
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unresolvable_state_fails_closed(self):
        self.fixture.doc("state.md", "Tracked in #404 (open).\n")
        result = self.run_with_states({})
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not be resolved", result.stderr)

    def test_offline_skips_the_issue_check(self):
        self.fixture.doc("state.md", "Tracked in #404 (open).\n")
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(self.fixture.root), "--offline"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_gh_is_a_finding_not_a_traceback(self):
        self.fixture.doc("state.md", "Tracked in #404 (open).\n")
        empty_bin = self.fixture.root / "emptybin"
        empty_bin.mkdir()
        env = dict(os.environ)
        env["PATH"] = str(empty_bin)
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(self.fixture.root), "--repo", "owner/repo"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh is not installed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_stalled_gh_is_bounded_and_reported(self):
        self.fixture.doc("state.md", "Tracked in #404 (open).\n")
        bin_dir = self.fixture.root / "slowbin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, %r); import check_docs;"
                    " check_docs.GH_TIMEOUT_SECONDS = 1;"
                    " sys.exit(check_docs.main(['--root', %r, '--repo', 'owner/repo']))"
                )
                % (str(ROOT / "scripts"), str(self.fixture.root)),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out after 1s", result.stderr)

    def test_prose_that_only_mentions_an_issue_is_not_a_claim(self):
        # Both spellings appear in this repo's docs and mean something other
        # than issue state.
        self.fixture.doc(
            "prose.md",
            """
            #113 merged the evidence produced under #78.
            #18 was opened through the sanctioned path.
            """,
        )
        result = self.run_with_states({})
        self.assertEqual(result.returncode, 0, result.stderr)


class ExcerptTests(DocFreshnessTestCase):
    def test_faithful_excerpt_passes(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py L1-L2 -->
            ```python
            def one():
                return 1
            ```
            """,
        )
        self.assertEqual(self.messages(), [])

    def test_drifted_excerpt_names_the_diverging_line(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py L1-L2 -->
            ```python
            def one():
                return 42
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("diverges at source line 2", messages[0])
        self.assertIn("return 1", messages[0])
        self.assertIn("return 42", messages[0])

    def test_excerpt_length_mismatch_is_reported(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py L1-L2 -->
            ```python
            def one():
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("has 1 lines", messages[0])

    def test_excerpt_of_whole_file_passes(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py -->
            ```python
            def one():
                return 1


            def two():
                return 2
            ```
            """,
        )
        self.assertEqual(self.messages(), [])

    def test_excerpt_range_past_end_of_file_is_reported(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py L1-L99 -->
            ```python
            def one():
            ```
            """,
        )
        self.assertIn("has 6 lines", self.messages()[0])

    def test_excerpt_of_missing_file_is_reported(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/gone.py L1 -->
            ```python
            gone()
            ```
            """,
        )
        self.assertIn("does not exist", self.messages()[0])

    def test_marker_without_a_fenced_block_is_reported(self):
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/sample.py L1 -->
            Just prose, no fence.
            """,
        )
        self.assertIn("not followed by a fenced code block", self.messages()[0])

    def test_absolute_excerpt_path_is_refused_without_disclosing_content(self):
        # Joining an absolute operand would discard the root entirely, and a
        # mismatch prints the source line into the job log.
        secret = self.fixture.root.parent / "absolute-secret.conf"
        secret.write_text("AUTHORIZATION: basic SECRET_TOKEN\n", encoding="utf-8")
        self.addCleanup(secret.unlink)
        self.fixture.doc(
            "quote.md",
            f"""
            <!-- doc-check: excerpt {secret} L1 -->
            ```text
            altered
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("outside the repository", messages[0])
        self.assertNotIn("SECRET_TOKEN", messages[0])

    def test_traversal_excerpt_path_is_refused(self):
        secret = self.fixture.root.parent / "traversal-secret.conf"
        secret.write_text("AUTHORIZATION: basic SECRET_TOKEN\n", encoding="utf-8")
        self.addCleanup(secret.unlink)
        self.fixture.doc(
            "quote.md",
            f"""
            <!-- doc-check: excerpt ../{secret.name} L1 -->
            ```text
            altered
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("outside the repository", messages[0])
        self.assertNotIn("SECRET_TOKEN", messages[0])

    def test_git_config_is_refused_as_an_excerpt_source(self):
        # .git/config holds the checkout credential when persist-credentials
        # is left on anywhere.
        self.fixture.write(
            ".git/config", "[http]\n\textraheader = AUTHORIZATION: basic SECRET_TOKEN\n"
        )
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt .git/config L2 -->
            ```text
            altered
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("outside the repository", messages[0])
        self.assertNotIn("SECRET_TOKEN", messages[0])

    def test_symlink_pointing_out_of_the_repository_is_refused(self):
        secret = self.fixture.root.parent / "symlink-secret.conf"
        secret.write_text("AUTHORIZATION: basic SECRET_TOKEN\n", encoding="utf-8")
        self.addCleanup(secret.unlink)
        link = self.fixture.root / "scripts" / "linked.conf"
        link.symlink_to(secret)
        self.fixture.doc(
            "quote.md",
            """
            <!-- doc-check: excerpt scripts/linked.conf L1 -->
            ```text
            altered
            ```
            """,
        )
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("outside the repository", messages[0])
        self.assertNotIn("SECRET_TOKEN", messages[0])

    def test_marker_shown_inside_a_fenced_block_is_documentation_not_a_claim(self):
        self.fixture.doc(
            "howto.md",
            """
            Mark an excerpt like this:

            ```markdown
            <!-- doc-check: excerpt scripts/gone.py L1-L2 -->
            ```
            """,
        )
        self.assertEqual(self.messages(), [])

    def test_unmarked_fenced_block_is_out_of_scope(self):
        self.fixture.doc(
            "quote.md",
            """
            See `scripts/sample.py`:

            ```python
            something_entirely_different()
            ```
            """,
        )
        self.assertEqual(self.messages(), [])


class CliTests(DocFreshnessTestCase):
    def test_json_output_lists_findings(self):
        self.fixture.doc("drifted.md", "Missing `scripts/gone.py`.\n")
        result = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--root",
                str(self.fixture.root),
                "--offline",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["findings"]), 1)
        self.assertEqual(payload["findings"][0]["file"], "docs/drifted.md")
        self.assertEqual(payload["findings"][0]["line"], 1)

    def test_missing_root_is_a_usage_error(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(self.fixture.root / "nope")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)

    def test_absent_docs_directory_is_clean(self):
        empty = self.fixture.root / "empty"
        empty.mkdir()
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(empty), "--offline"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class RepositoryDocumentationTests(unittest.TestCase):
    """The gate applied to this repository's own docs, minus the network."""

    def test_this_repositorys_docs_agree_with_its_code(self):
        findings = check_docs.check_documents(ROOT, offline=True)
        self.assertEqual([f.render() for f in findings], [])


if __name__ == "__main__":
    unittest.main()


class WorkflowCredentialBoundaryTests(unittest.TestCase):
    """The checker is PR-controlled code; it must never be handed a token.

    Parsed as text rather than YAML on purpose: pyyaml is not a declared
    dependency of this repo, and adding one so a test can read CI config
    would be a worse trade than string matching.
    """

    WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

    def _docs_job(self) -> str:
        text = self.WORKFLOW.read_text(encoding="utf-8")
        start = text.index("\n  docs-freshness:")
        rest = text[start + 1:]
        # The job ends at the next job key at the same indent level.
        end = rest.find("\n  test-and-lint:")
        return rest if end == -1 else rest[:end]

    def _steps(self):
        return self._docs_job().split("\n      - name:")[1:]

    def _step_running_checker(self, *, on_pull_request: bool) -> str:
        wanted = "== 'pull_request'" if on_pull_request else "!= 'pull_request'"
        for step in self._steps():
            if "check_docs.py" in step and wanted in step:
                return step
        self.fail(
            f"no docs-freshness step running check_docs.py guarded by {wanted}"
        )

    def test_pull_request_path_receives_no_token(self):
        step = self._step_running_checker(on_pull_request=True)
        self.assertNotIn("GH_TOKEN", step)
        self.assertNotIn("GITHUB_TOKEN", step)
        self.assertNotIn("secrets.", step)

    def test_pull_request_path_runs_offline(self):
        # Without a token the issue-state lookup cannot succeed, so the PR
        # path must skip it explicitly rather than fail the build on every PR.
        step = self._step_running_checker(on_pull_request=True)
        self.assertIn("--offline", step)

    def test_trusted_path_keeps_the_full_check(self):
        step = self._step_running_checker(on_pull_request=False)
        self.assertIn("GH_TOKEN", step)
        self.assertNotIn("--offline", step)

    def _workflow_permissions(self) -> list:
        """Grant lines from the top-level permissions block, comments excluded.

        Scoped to the block rather than the whole header: the header prose
        legitimately mentions 'issues:' when explaining why it is absent.
        """
        text = self.WORKFLOW.read_text(encoding="utf-8")
        block = text[text.index("\npermissions:") + 1: text.index("\njobs:")]
        grants = []
        for line in block.splitlines()[1:]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line.startswith("  "):
                break
            grants.append(stripped)
        return grants

    def test_issues_read_is_not_granted_workflow_wide(self):
        grants = self._workflow_permissions()
        self.assertNotIn("issues: read", grants)
        self.assertIn("contents: read", grants)

    def test_issues_read_is_granted_to_the_docs_job(self):
        self.assertIn("issues: read", self._docs_job())

    def test_secret_scan_declares_no_job_level_permissions(self):
        """gitleaks-action only works when secret-scan inherits the default.

        Adding a job-level permissions block here -- even one granting a
        superset of contents/pull-requests/issues read -- made
        ScanPullRequest fail 403 on GET /pulls/<n>/commits. Observed on runs
        32037999517 and 32039000036; removing the block fixed it. This test
        exists so the next person to "tidy up" permissions sees why first.
        """
        text = self.WORKFLOW.read_text(encoding="utf-8")
        job = text[text.index("\n  secret-scan:"): text.index("\n  dependency-audit:")]
        code = "\n".join(
            line for line in job.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("permissions:", code)

    def test_checkout_does_not_persist_credentials(self):
        self.assertIn("persist-credentials: false", self._docs_job())
