import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
CHANGELOG = ROOT / "CHANGELOG.md"
GOLDEN_PATH = ROOT / "docs" / "golden-path-demo.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class GovernanceWordingTests(unittest.TestCase):
    def test_agents_describes_rulesets_as_enforcement_not_merge_authority(self):
        text = " ".join(read(AGENTS).split()).lower()
        self.assertIn("active rules block ordinary direct pushes", text)
        self.assertIn("admins can weaken or disable those rulesets", text)
        self.assertIn("merge_pr.py` remains mandatory governance for every merge", text)

    def test_agents_current_router_points_to_run_aru_factory(self):
        text = read(AGENTS)
        self.assertIn("skills/run-aru-factory/SKILL.md", text)
        self.assertNotIn("skills/aru-agentic-sdlc/SKILL.md", text)

    def test_golden_path_demo_no_longer_advertises_retired_preview_deploy(self):
        text = read(GOLDEN_PATH)
        self.assertNotIn("skills/deploy-preview", text)
        self.assertNotIn("scripts/deploy_preview.py", text)
        self.assertNotIn("deploy-preview` at the pinned sha", text.lower())
        self.assertNotIn("the helper records the github pages", text.lower())

    def test_changelog_live_surface_does_not_list_increment_release(self):
        current = read(CHANGELOG).split("## Checkpoint History", 1)[0]
        self.assertNotIn("scripts/increment_release.py", current)


if __name__ == "__main__":
    unittest.main()
