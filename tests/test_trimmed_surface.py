"""The fleet and Slack surfaces are gone and cannot come back by accident (#406, #415).

Deleting files is easy; keeping them deleted is the part that needs a test. A
retained module that still imports a removed one is a broken command nobody
notices until an operator runs it, and a "compact" status file grows back into
a supervisor one convenience helper at a time.

These tests read the repository as data rather than importing it, so a module
whose dependencies are themselves being trimmed cannot make the guard vacuous.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The fleet supervisor, presence launchers, simulation harness, standalone
# metrics product, and the Slack control-room runtime. Each is a runtime the
# kernel no longer has.
REMOVED_PATHS = (
    "scripts/run_fleet.py",
    # The runner's prompt/fingerprint helpers, which arrived on `main` while
    # this slice was open and have no consumer once the runner is gone.
    "scripts/fleet_cycle.py",
    "scripts/spawn_ephemeral_worker.py",
    "scripts/launch_fleet.sh",
    "scripts/factory_metrics.py",
    "scripts/fixtures/__init__.py",
    "scripts/fixtures/fleet/__init__.py",
    "scripts/fixtures/fleet/harness.py",
    "tests/test_run_fleet.py",
    "tests/test_spawn_ephemeral_worker.py",
    "tests/test_factory_metrics.py",
    "tests/e2e/__init__.py",
    "tests/e2e/test_unattended_board_completion.py",
    "tests/fixtures/fleet/unattended_completion.json",
    # Slack control-room runtime, notifications, projects registry, and direct tests (#415).
    "scripts/slack_control_room.py",
    "scripts/slack_notify.py",
    "scripts/slack_projects.py",
    "tests/test_slack_control_room.py",
    "tests/test_slack_notify.py",
    "tests/test_slack_projects.py",
    "scripts/factory_loop_snapshot.py",
    "tests/test_factory_loop_snapshot.py",
    "scripts/factory_loop_ledger.py",
    "tests/test_factory_loop_ledger.py",
    "scripts/build_preview.py",
    "scripts/deploy_preview.py",
    "scripts/smoke_preview.py",
    ".github/workflows/deploy-preview.yml",
    ".github/workflows/promote.yml",
    "scripts/promote.py",
    "tests/test_deploy_preview.py",
    "tests/test_smoke_preview.py",
    "tests/test_promote.py",
    "templates/stacks/python/deploy-preview.yml",
    "templates/stacks/node/deploy-preview.yml",
    "templates/stacks/go/deploy-preview.yml",
    "templates/stacks/python/docs/deploy.md",
    "templates/stacks/node/docs/deploy.md",
    "templates/stacks/go/docs/deploy.md",
    "sdlc_flow_visualizer/index.html",
    "sdlc_flow_visualizer/app.js",
    "sdlc_flow_visualizer/styles.css",
    "scripts/verify_citations.py",
    "docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md",
    "tests/test_research_skill.py",
    "scripts/delivery_increments.py",
    "scripts/increment_release.py",
    "tests/test_delivery_increments.py",
    "tests/test_increment_release.py",
    "scripts/incident_intake.py",
    "tests/test_incident_intake.py",
    "templates/cursor/commands/aru-agentic-sdlc.md",
    "templates/cursor/rules/aru-agentic-sdlc.mdc",
    "templates/slack/manifest.yaml",
    "templates/slack/manifest-hermes-war-room.yaml",
)

REMOVED_MODULES = (
    "run_fleet", "fleet_cycle", "spawn_ephemeral_worker", "factory_metrics",
    "fixtures", "slack_control_room", "slack_notify", "slack_projects", "slack",
    "slack_sdk", "slack_bolt", "factory_loop_snapshot", "factory_loop_ledger",
    "build_preview", "deploy_preview", "smoke_preview", "promote",
    "verify_citations", "delivery_increments", "increment_release",
    "incident_intake",
)

# `scripts/agent_presence.py` outlives this slice. Its three live dependents
# are `#414`'s reserved paths, not `#406`'s, so deleting the module here would
# turn CI red on files this issue may not edit. The set below is a ceiling
# rather than an equality: the coupling may shrink as those paths are cleaned,
# but a new dependent must not appear while the module is on its way out.
PRESENCE_DEPENDENTS_ALLOWED = {
    "scripts/fetch_next_work.py",
    "tests/test_agent_fingerprint.py",
    "tests/test_claim_review.py",
    "tests/test_agent_presence.py",
}

STATUS_LINE_BUDGET = 250

# Eligibility, telemetry, cost, CI-rate, review-round, and supervisor concepts.
# Each was a real feature of the old file; none belongs in a read-only report.
FORBIDDEN_STATUS_TOKENS = (
    "stall", "fleet_size", "ready_target", "operator_screen", "merge_queue",
    "cost", "usd", "ci_failure", "review_round", "rework", "heartbeat",
    "presence", "scheduler", "daemon", "capacity", "quota", "telemetry",
    "eligib", "spawn", "supervis",
)


def source_files():
    """Every retained Python file the framework ships, as (rel_path, text)."""
    for directory in ("scripts", "hooks", "tests"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def code_vocabulary(text):
    """Identifiers, attributes, and string literals -- the module's real words.

    Prose is excluded on purpose. A comment explaining *why* the cost telemetry
    was removed must not be read as the telemetry coming back.
    """
    words = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            words.append(node.name)
        elif isinstance(node, ast.arg):
            words.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            words.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.append(node.value)
    return "\n".join(words).lower()


def imported_modules(text):
    """Top-level module names imported by ``text``, from the parsed AST.

    Parsing rather than grepping keeps a module name inside a docstring, a
    comment, or an assertion from counting as a dependency -- several retained
    tests assert that a removed module is *absent* by name.
    """
    names = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class RemovedSurfaceTests(unittest.TestCase):
    def test_every_removed_path_is_absent(self):
        for rel in REMOVED_PATHS:
            with self.subTest(path=rel):
                self.assertFalse((ROOT / rel).exists(), f"{rel} still exists")

    def test_removed_package_directories_are_absent(self):
        for rel in (
            "scripts/fixtures",
            "tests/e2e",
            "tests/fixtures/fleet",
            "sdlc_flow_visualizer",
        ):
            with self.subTest(path=rel):
                self.assertFalse((ROOT / rel).exists(), f"{rel} still exists")

    def test_no_retained_module_imports_a_removed_module(self):
        for rel, text in source_files():
            with self.subTest(path=rel):
                leftover = imported_modules(text) & set(REMOVED_MODULES)
                self.assertEqual(leftover, set(), f"{rel} imports {leftover}")

    def test_retained_claim_and_recovery_code_never_imports_factory_metrics(self):
        """#410 moved claim recovery off metrics; nothing may import it back."""
        for rel, text in source_files():
            with self.subTest(path=rel):
                self.assertNotIn("factory_metrics", imported_modules(text))

    def test_presence_coupling_never_grows_past_the_paths_406_cannot_edit(self):
        """Blocked, not forgotten: see the #406 amended plan for the blocker.

        `scripts/agent_presence.py` cannot be deleted here because
        `scripts/fetch_next_work.py` still calls `PresenceStore` and two test
        modules import it -- all three are `#414`'s reserved paths, not this
        issue's. Asserting a subset rather than equality keeps the guard honest
        in both directions: a fourth dependent fails it, while a later slice
        that drops one of these imports does not fail a file it cannot edit.
        """
        dependents = {
            rel for rel, text in source_files()
            if "agent_presence" in imported_modules(text)
        }
        unexpected = dependents - PRESENCE_DEPENDENTS_ALLOWED
        self.assertEqual(unexpected, set(), f"new presence coupling: {unexpected}")


class RetainedRuntimeBoundaryTests(unittest.TestCase):
    """Retained kernel modules cannot import Slack or read retired runtime state."""

    RETAINED_KERNEL_SCRIPTS = (
        "scripts/fleet_status.py",
        "scripts/claim_issue.py",
        "scripts/create_branch.py",
        "scripts/create_pr.py",
        "scripts/merge_pr.py",
        "scripts/revert_merge.py",
        "scripts/check_ci.py",
        "scripts/fetch_pr_feedback.py",
        "scripts/update_issue_status.py",
        "scripts/common.py",
        "scripts/agent_identity.py",
        "scripts/doctor_local_agent_integrations.py",
        "scripts/fetch_next_issue.py",
    )

    FORBIDDEN_STORAGE_PATTERNS = (
        r"slack-control-room",
        r"slack_control_room",
        r"slack_notify",
        r"slack_projects",
        r"slack-control-room-seen\.json",
        r"delivery-increments\.json",
        r"agent-presence\.json",
        r"projects\.json",
    )

    def test_retained_kernel_never_imports_slack_packages(self):
        slack_pkgs = {"slack", "slack_sdk", "slack_bolt", "slack_notify", "slack_control_room"}
        for rel in self.RETAINED_KERNEL_SCRIPTS:
            path = ROOT / rel
            if not path.exists():
                continue
            with self.subTest(script=rel):
                imports = imported_modules(path.read_text(encoding="utf-8"))
                self.assertEqual(imports & slack_pkgs, set())

    def test_retained_kernel_does_not_read_retired_state_stores(self):
        """Retained kernel cannot reference retired on-disk stores or registries."""
        for rel in self.RETAINED_KERNEL_SCRIPTS:
            path = ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            vocab = code_vocabulary(text)
            for pattern in self.FORBIDDEN_STORAGE_PATTERNS:
                with self.subTest(script=rel, pattern=pattern):
                    self.assertFalse(
                        re.search(pattern, vocab, re.IGNORECASE),
                        f"{rel} references retired storage pattern: {pattern}",
                    )

    def test_retained_kernel_never_imports_delivery_increments(self):
        for rel in self.RETAINED_KERNEL_SCRIPTS:
            path = ROOT / rel
            if not path.exists():
                continue
            with self.subTest(script=rel):
                imports = imported_modules(path.read_text(encoding="utf-8"))
                self.assertNotIn("delivery_increments", imports)

    def test_retained_kernel_does_not_read_operator_credentials_or_seen_store(self):
        """Retained kernel does not read ~/.aru/projects.json, ~/.aru/slack-control-room-seen.json, or Slack env tokens."""
        forbidden_tokens = (
            "slack_bot_token", "slack_app_token", ".hermes/.env",
            "slack-control-room.pid", "slack_channel_id", "slack_team_id",
        )
        for rel in self.RETAINED_KERNEL_SCRIPTS:
            path = ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            vocab = code_vocabulary(text)
            for token in forbidden_tokens:
                with self.subTest(script=rel, token=token):
                    self.assertNotIn(token, vocab)

    def test_no_retained_module_imports_slack_sdk_or_bolt(self):
        for rel, text in source_files():
            with self.subTest(path=rel):
                leftover = imported_modules(text) & {"slack", "slack_sdk", "slack_bolt"}
                self.assertEqual(leftover, set(), f"{rel} imports Slack package {leftover}")


class CompactStatusTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / "scripts" / "fleet_status.py"
        self.text = self.path.read_text(encoding="utf-8")

    def test_status_command_is_retained(self):
        self.assertTrue(self.path.exists())

    def test_status_is_within_the_line_budget(self):
        effective = [
            line for line in self.text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertLessEqual(
            len(effective), STATUS_LINE_BUDGET,
            f"fleet_status.py has {len(effective)} nonblank/noncomment lines",
        )

    def test_status_carries_no_supervisor_or_telemetry_concept(self):
        vocabulary = code_vocabulary(self.text)
        for token in FORBIDDEN_STATUS_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, vocabulary)

    def test_status_reads_the_board_once_instead_of_once_per_issue(self):
        """The per-issue Project query was the N+1 that drained the quota."""
        self.assertNotIn("query_issue_project_items", self.text)
        self.assertIn("governed_board_inventory", self.text)

    def test_status_declares_only_repo_dir_and_json(self):
        flags = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', self.text))
        self.assertEqual(flags, {"--json", "--repo-dir"})

    def test_status_writes_nothing(self):
        """A read-only command must not be able to reach a mutating helper."""
        forbidden = {
            "set_board_status", "update_status", "ensure_label",
            "attach_issue_to_governed_project", "create_worktree",
            "set_issue_priority_field",
        }
        self.assertEqual(forbidden & set(re.findall(r"\w+", self.text)), set())


if __name__ == "__main__":
    unittest.main()
