"""Contract tests that keep the static lifecycle visualizer truthful."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VISUALIZER = ROOT / "sdlc_flow_visualizer"
INDEX_PATH = VISUALIZER / "index.html"
APP_PATH = VISUALIZER / "app.js"
STYLES_PATH = VISUALIZER / "styles.css"


class FactoryStatusFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = INDEX_PATH.read_text(encoding="utf-8")
        cls.app = APP_PATH.read_text(encoding="utf-8")
        cls.styles = STYLES_PATH.read_text(encoding="utf-8")

    def test_toolbar_has_no_current_looking_volatile_snapshot(self):
        self.assertIn("Current committed lifecycle contract", self.index)
        self.assertNotRegex(self.index, r"Repository snapshot\s*[·-]\s*20\d\d-")
        for stale in (
            "Helper Scripts",
            "Skills</div>",
            "Local Tests Passing",
            "511 Local Tests",
        ):
            self.assertNotIn(stale, self.index)

    def test_closed_intake_work_is_shipped_and_points_to_committed_skills(self):
        for node, skill, issue in (
            ("node-prd", "skills/idea-to-prd/SKILL.md", "#102"),
            ("node-dag", "skills/prd-to-issues/SKILL.md", "#103"),
        ):
            card = re.search(
                rf'<div class="([^"]*)" id="{node}".*?</div>\s*</div>',
                self.index,
                re.DOTALL,
            )
            self.assertIsNotNone(card, node)
            self.assertNotIn("planned-card", card.group(1))
            self.assertIn(skill, self.app)
            self.assertIn(f"issue {issue} closed", self.app)
            self.assertTrue((ROOT / skill).is_file())
        for stale in ("Planned: #102", "Planned: #103", "not yet shipped"):
            self.assertNotIn(stale, self.index + self.app)

    def test_deployment_cards_separate_runnable_audit_and_open_provider_work(self):
        self.assertIn('id="node-preview" data-id="preview"', self.index)
        self.assertIn("Runnable Preview &amp; Smoke", self.index)
        self.assertIn('class="flow-card scope-card" id="node-deploy"', self.index)
        self.assertIn("audit-only", self.index.lower())
        self.assertIn('class="flow-card planned-card" id="node-provider"', self.index)
        self.assertIn("Open: #345", self.index)

        for shipped in (
            "skills/deploy-preview/SKILL.md",
            "scripts/deploy_preview.py",
            "scripts/smoke_preview.py",
            "scripts/promote.py",
            "scripts/revert_merge.py",
        ):
            self.assertTrue((ROOT / shipped).is_file(), shipped)
        for required_truth in (
            "authoritative Pages URL",
            "does not deploy, copy, rebuild",
            "not an application URL",
            "immutable deployment identity",
            "authoritative hosted URLs",
            "live smoke results",
            "production promotion",
            "rollback evidence",
        ):
            self.assertIn(required_truth, self.app)

    def test_merge_card_matches_the_shipped_merge_commit_default(self):
        merge_source = (ROOT / "scripts" / "merge_pr.py").read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--merge-method", default="merge"', merge_source)
        self.assertIn("Current default is a merge commit", self.app)
        self.assertIn("#89 is closed", self.app)
        self.assertNotIn("Current default is squash", self.app)
        self.assertNotIn("--merge-method squash'", self.app)
        self.assertNotIn("7-point", self.index)
        self.assertNotIn("Validates 7 strict checks", self.app)

    def test_telemetry_card_names_the_shipped_metrics_path(self):
        self.assertTrue((ROOT / "scripts" / "factory_metrics.py").is_file())
        self.assertIn("fleet_status.py + factory_metrics.py", self.index)
        for metric in ("dwell", "rework", "CI failure", "cycle time", "cost"):
            self.assertIn(metric, self.index)
        self.assertNotIn("metrics are planned in #106", self.index + self.app)

    def test_every_visual_card_has_drawer_metadata(self):
        card_ids = set(re.findall(r'data-id="([a-z0-9-]+)"', self.index))
        detail_ids = set(re.findall(r"^    ([a-z0-9]+): \{$", self.app, re.MULTILINE))
        self.assertTrue(card_ids)
        self.assertEqual(card_ids, detail_ids)

    def test_visualizer_opens_directly_from_disk(self):
        self.assertIn('<link rel="stylesheet" href="styles.css">', self.index)
        self.assertIn('<script src="app.js"></script>', self.index)
        self.assertTrue(STYLES_PATH.is_file())
        self.assertTrue(APP_PATH.is_file())
        self.assertNotRegex(self.index, r'<(?:script|link)[^>]+(?:src|href)="https?://')
        for network_api in ("fetch(", "XMLHttpRequest", "import("):
            self.assertNotIn(network_api, self.app)
        self.assertIn(".scope-card", self.styles)
        self.assertIn(".planned-card", self.styles)


if __name__ == "__main__":
    unittest.main()
