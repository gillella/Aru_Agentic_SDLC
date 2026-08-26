"""Focused cross-surface assertions for issue #472 review governance."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReviewAuthoritySkillSurfaceTests(unittest.TestCase):
    def text(self, relative):
        return " ".join((ROOT / relative).read_text(encoding="utf-8").split())

    def test_implementation_skill_uses_balanced_external_assignment(self):
        text = self.text("skills/implement-next-issue/SKILL.md")
        self.assertIn("deterministic least-loaded", text)
        for label in ("review:coderabbit", "review:sourcery", "review:codeant"):
            self.assertIn(label, text)
        self.assertNotIn("Await CodeRabbit by default", text)

    def test_emergency_review_skill_forbids_ordinary_agent_review(self):
        text = self.text("skills/code-review/SKILL.md").lower()
        self.assertIn("never ordinary review", text)
        self.assertIn("external exhaustion", text)
        self.assertIn("exact current head", text)
        self.assertIn("aru-agent-review:v1", text)


if __name__ == "__main__":
    unittest.main()
