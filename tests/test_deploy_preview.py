import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class DeployPreviewSkillTests(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parents[1]
        self.skill_file = self.root_dir / "skills" / "deploy-preview" / "SKILL.md"

    def test_deploy_preview_skill_file_exists(self):
        self.assertTrue(self.skill_file.exists(), f"Skill file {self.skill_file} missing")

    def test_deploy_preview_skill_frontmatter(self):
        content = self.skill_file.read_text()
        self.assertIn("name: deploy-preview", content)
        self.assertIn("triggers:", content)
        self.assertIn("deploy preview", content)

    def test_install_cursor_integration_includes_deploy_preview(self):
        installer = self.root_dir / "scripts" / "install_cursor_integration.sh"
        content = installer.read_text()
        self.assertIn("deploy-preview", content)

    def test_skill_defines_failure_remediation_path(self):
        content = self.skill_file.read_text()
        self.assertIn("Handle Deployment Failures", content)
        self.assertIn("governed GitHub issue", content)
        self.assertIn("type:fix", content)


if __name__ == "__main__":
    unittest.main()
