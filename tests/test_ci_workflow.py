"""Regression contract for focused story gates and full-suite checkpoints."""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PROMPT_PATH = ROOT / "prompts" / "fleet-worker.md"
SKILL_PATH = ROOT / "skills" / "implement-next-issue" / "SKILL.md"
STANDARDS_PATH = ROOT / "docs" / "coding_standards.md"
BOARD_PATH = ROOT / "docs" / "project_board_workflow.md"


def read(path):
    return path.read_text(encoding="utf-8")


def flat(path):
    return " ".join(read(path).split()).lower()


def workflow():
    return yaml.safe_load(read(WORKFLOW_PATH))


def job_steps(job_name):
    return workflow()["jobs"][job_name]["steps"]


def named_step(job_name, step_name):
    for step in job_steps(job_name):
        if step.get("name") == step_name:
            return step
    raise AssertionError(f"missing {step_name!r} in {job_name!r}")


class FocusedStoryContractTests(unittest.TestCase):
    def test_all_operator_guidance_defines_the_focused_story_contract(self):
        for path in (PROMPT_PATH, SKILL_PATH, STANDARDS_PATH):
            text = flat(path)
            self.assertIn("every", text, str(path))
            self.assertIn("`verify:`", text, str(path))
            self.assertIn("directly affected tests", text, str(path))
            for gate in ("lint", "syntax", "documentation", "build"):
                self.assertIn(gate, text, f"{path}: missing {gate}")

    def test_focused_scope_never_means_zero_behavioral_evidence(self):
        for path in (PROMPT_PATH, SKILL_PATH, STANDARDS_PATH, BOARD_PATH):
            text = flat(path)
            self.assertIn("behavioral evidence", text, str(path))
        self.assertIn("never permits zero behavioral evidence", flat(BOARD_PATH))

    def test_current_head_verification_and_merge_acceptance_remain_authoritative(self):
        text = flat(BOARD_PATH)
        self.assertIn("create_pr.py", text)
        self.assertIn("current pr head", text)
        self.assertIn("merge_pr.py", text)
        self.assertIn("missing, failing, or stale verification", text)

    def test_complete_suite_is_not_a_default_story_command(self):
        for path in (PROMPT_PATH, SKILL_PATH, STANDARDS_PATH):
            text = flat(path)
            self.assertIn("not", text, str(path))
            self.assertIn("default", text, str(path))
            self.assertIn("python3 -m unittest discover tests", text, str(path))


class WorkflowContractTests(unittest.TestCase):
    def test_fast_required_job_identity_is_preserved(self):
        self.assertEqual(workflow()["jobs"]["test-and-lint"]["name"],
                         "Lint, Verify & Test")

    def test_complete_suite_runs_only_on_schedule_or_manual_dispatch(self):
        step = named_step("test-and-lint", "Execute Tests")
        condition = step.get("if", "")
        self.assertIn("github.event_name == 'schedule'", condition)
        self.assertIn("github.event_name == 'workflow_dispatch'", condition)
        self.assertNotIn("pull_request", condition)
        self.assertNotIn("push", condition)
        self.assertNotIn("!=", condition)
        self.assertEqual(
            step["run"].strip(),
            "python -m unittest discover tests",
        )

    def test_schedule_and_manual_triggers_remain_available(self):
        text = read(WORKFLOW_PATH)
        self.assertIn("  schedule:\n", text)
        self.assertIn("  workflow_dispatch:\n", text)
        self.assertIn("  pull_request:\n", text)
        self.assertIn("  push:\n", text)

    def test_every_checkout_discards_its_persisted_credential(self):
        checkout_steps = [
            step
            for job in workflow()["jobs"].values()
            for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertTrue(checkout_steps)
        for step in checkout_steps:
            self.assertIs(step.get("with", {}).get("persist-credentials"), False,
                          step.get("name", "unnamed checkout"))

    def test_python_311_and_pinned_dependencies_remain_in_the_test_job(self):
        setup = named_step("test-and-lint", "Set up Python")
        install = named_step("test-and-lint", "Install Dependencies")
        self.assertEqual(setup["with"]["python-version"], "3.11")
        self.assertIn("requirements-dev.txt", install["run"])

    def test_workflow_permissions_are_read_only(self):
        for scope, access in workflow()["permissions"].items():
            self.assertEqual(access, "read", f"workflow {scope}")
        for job_name, job in workflow()["jobs"].items():
            for scope, access in job.get("permissions", {}).items():
                self.assertEqual(access, "read", f"{job_name} {scope}")


class PhaseCheckpointContractTests(unittest.TestCase):
    def test_phase_checkpoint_requires_exact_commit_and_actions_evidence(self):
        text = flat(BOARD_PATH)
        for phrase in (
            "exact current default-branch commit",
            "full 40-character commit sha",
            "successful github actions run url",
            "phase epic",
        ):
            self.assertIn(phrase, text)

    def test_stale_or_missing_evidence_blocks_phase_close_and_release(self):
        text = flat(BOARD_PATH)
        self.assertIn("blocks phase closure", text)
        self.assertIn("blocks release", text)
        self.assertIn("scheduled run counts only", text)

    def test_lifecycle_kernel_has_a_named_focused_predicate(self):
        text = flat(BOARD_PATH)
        self.assertIn("issue #293", text)
        self.assertIn("python3 -m unittest tests.test_pipeline_traversal", text)
        self.assertIn("unrelated documentation-only stories do not run it", text)


if __name__ == "__main__":
    unittest.main()
