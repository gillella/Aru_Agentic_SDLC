"""The fleet surface is gone and cannot come back by accident (#406).

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

# The fleet supervisor, presence launchers, simulation harness, and the
# standalone metrics product. Each is a runtime the kernel no longer has.
REMOVED_PATHS = (
    "scripts/run_fleet.py",
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
)

REMOVED_MODULES = (
    "run_fleet", "spawn_ephemeral_worker", "factory_metrics", "fixtures",
)

# `#406` may not edit these; `#414` reserves all three in its own `touches:`
# and removes the last presence dependency there. Pinning the set means the
# coupling can only shrink, never grow, while that removal is outstanding.
PRESENCE_DEPENDENTS_PENDING_414 = {
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
        for rel in ("scripts/fixtures", "tests/e2e", "tests/fixtures/fleet"):
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

    def test_presence_coupling_is_pinned_to_the_paths_414_owns(self):
        """Blocked, not forgotten: see the #406 amended plan for the blocker.

        `scripts/agent_presence.py` cannot be deleted here because
        `scripts/fetch_next_work.py` still calls `PresenceStore` and two test
        modules import it -- all three are `#414`'s reserved paths. This guard
        fails if a fourth dependent appears, and fails again once `#414` lands
        so the removal is finished rather than silently left half-done.
        """
        dependents = {
            rel for rel, text in source_files()
            if "agent_presence" in imported_modules(text)
        }
        self.assertEqual(dependents, PRESENCE_DEPENDENTS_PENDING_414)


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
