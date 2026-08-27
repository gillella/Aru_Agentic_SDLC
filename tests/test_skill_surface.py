"""Focused assertions for the retained skill surface and review governance."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"

RUNTIME_KERNEL = {
    "address-pr-feedback",
    "code-review",
    "create-github-issue",
    "implement-next-issue",
    "init-agent-project",
    "remediate-ci-failure",
    "run-aru-factory",
    "triage-backlog",
}
OPTIONAL_TOOLS = {
    "grill-aru",
    "idea-to-prd",
    "prd-to-issues",
    "prune-codebase",
}
REMOVED_SKILLS = {"aru-agentic-sdlc", "deploy-preview", "research"}


class ReviewAuthoritySkillSurfaceTests(unittest.TestCase):
    def text(self, relative):
        return " ".join((ROOT / relative).read_text(encoding="utf-8").split())

    def skill_text(self, name):
        return self.text(f"skills/{name}/SKILL.md")

    def test_installed_surface_is_kernel_plus_optional_tools(self):
        installed = {
            path.name
            for path in SKILLS.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        }
        self.assertEqual(RUNTIME_KERNEL | OPTIONAL_TOOLS, installed)
        for name in REMOVED_SKILLS:
            self.assertFalse((SKILLS / name).exists())

    def test_optional_tools_are_outside_the_runtime_lifecycle(self):
        runtime_text = " ".join(self.skill_text(name) for name in RUNTIME_KERNEL)
        for name in OPTIONAL_TOOLS:
            self.assertNotIn(name, runtime_text)

            optional_text = self.skill_text(name).lower()
            self.assertIn("optional", optional_text)
            self.assertTrue("pre-intake" in optional_text or "audit tool" in optional_text)
            for lifecycle_action in (
                "claim_issue.py",
                "create_branch.py",
                "create_pr.py",
                "merge_pr.py",
                "reassign_review.py",
                "fetch_next_work.py",
            ):
                self.assertNotIn(lifecycle_action, optional_text)

    def test_runtime_has_one_picker_and_no_removed_routes(self):
        runtime_text = " ".join(self.skill_text(name) for name in RUNTIME_KERNEL)
        self.assertIn("fetch_next_work.py", runtime_text)
        for removed_route in (
            "fetch_next_issue.py",
            "slack_control_room.py",
            "slack_notify.py",
            "incident_intake.py",
            "--emit-review-split",
            "delivery increment",
            "verify_citations.py",
            "deploy_preview.py",
            "promote.py",
            "run_fleet.py",
            "spawn_ephemeral_worker.py",
            "launch_fleet.sh",
            "skills/research",
            "skills/aru-agentic-sdlc",
        ):
            self.assertNotIn(removed_route, runtime_text.lower())

    def test_implementation_skill_uses_balanced_external_assignment(self):
        text = self.skill_text("implement-next-issue")
        self.assertIn("deterministic least-loaded", text)
        for label in ("review:coderabbit", "review:sourcery", "review:codeant"):
            self.assertIn(label, text)
        self.assertNotIn("Await CodeRabbit by default", text)

        router_text = self.skill_text("run-aru-factory")
        self.assertIn("deterministic least-loaded", router_text)
        for label in ("review:coderabbit", "review:sourcery", "review:codeant"):
            self.assertIn(label, router_text)
        self.assertNotIn("new PRs get exactly `review:coderabbit`", router_text.lower())

    def test_emergency_review_skill_forbids_ordinary_agent_review(self):
        text = self.skill_text("code-review").lower()
        self.assertIn("never ordinary review", text)
        self.assertIn("external exhaustion", text)
        self.assertIn("exact current head", text)
        self.assertIn("aru-agent-review:v1", text)


if __name__ == "__main__":
    unittest.main()
