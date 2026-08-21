"""Mechanical truth contract for the canonical factory documentation."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATHS = (
    Path("README.md"),
    Path("docs/ARU-SOFTWARE-FACTORY.md"),
    Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md"),
)
STATUS_ROWS = (
    "| **Shipped** | Present in the repository with linked implementation evidence. |",
    "| **Current** | The live roadmap slice represented by open board work; consult the board for item state. |",
    "| **Deferred** | Intentionally sequenced after an unmet phase entry gate; not available now. |",
    "| **Blocked** | Cannot start or finish until an explicit dependency or operator decision is satisfied. |",
    "| **Historical** | Dated evidence about an earlier state; never a current capability claim. |",
    "| **Audit-only** | Records governance evidence but does not prove a runnable artifact or environment. |",
)


class FactoryDocsFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {
            path: (ROOT / path).read_text(encoding="utf-8")
            for path in CANONICAL_PATHS
        }

    def test_all_canonical_documents_share_one_status_vocabulary(self):
        for path, document in self.documents.items():
            with self.subTest(path=path):
                self.assertIn("## Lifecycle status vocabulary", document)
                for row in STATUS_ROWS:
                    self.assertEqual(document.count(row), 1)

    def test_live_roadmap_authority_is_board_backed(self):
        for path, document in self.documents.items():
            with self.subTest(path=path):
                self.assertIn("#335", document)
                self.assertIn("Project Board", document)
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        for issue in ("#336", "#337", "#338", "#339", "#84", "#186"):
            self.assertIn(issue, plan)
        self.assertIn("| 0 | **Current** |", plan)
        self.assertEqual(plan.count("| **Deferred** |"), 6)

    def test_historical_roadmap_cannot_look_current(self):
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        marker = "### 5.2 Historical S-roadmap snapshot — verified 2026-08-15"
        self.assertIn(marker, plan)
        self.assertIn(
            "## 6. Historical sequencing snapshot — verified 2026-08-15",
            plan,
        )
        self.assertLess(plan.index(marker), plan.index("#### Historical Phase 0"))
        for phase in range(6):
            self.assertIn(f"#### Historical Phase {phase}", plan)
        self.assertNotIn("### Phase 0 —", plan)

    def test_shipped_intake_and_telemetry_are_not_future_gaps(self):
        research = self.documents[
            Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md")
        ]
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        for stale in (
            "front of factory gap",
            "**Weak / partial**",
            "**Missing / weak**",
            "Ship `fleet_status.py` early in Phase 4",
        ):
            self.assertNotIn(stale, research)
        self.assertIn("#101–#103", research)
        self.assertIn("#106–#108", research)
        self.assertIn("#106–#108", plan)

    def test_deployment_scope_is_explicit_in_every_document(self):
        required = (
            "scripts/deploy_preview.py",
            "scripts/promote.py",
            "GitHub Pages",
            "#345",
            "immutable",
            "authoritative",
            "smoke",
            "promotion",
            "rollback",
        )
        for path, document in self.documents.items():
            with self.subTest(path=path):
                for phrase in required:
                    self.assertIn(phrase, document)
                self.assertRegex(
                    document,
                    re.compile(
                        r"Audit-only.{0,180}(does not|not hosting|no runnable)",
                        re.IGNORECASE | re.DOTALL,
                    ),
                )

    def test_merge_default_matches_the_shipped_helper(self):
        for path, document in self.documents.items():
            with self.subTest(path=path):
                self.assertIn("merge_pr.py", document)
                self.assertIn("squash", document.lower())
                self.assertIn("opt-in", document.lower())
                self.assertRegex(
                    document,
                    re.compile(
                        r"defaults? to\s+(?:a\s+)?merge\s+commits?",
                        re.IGNORECASE,
                    ),
                )

    def test_existing_document_freshness_authority_is_reused(self):
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        self.assertIn("scripts/check_docs.py", plan)
        self.assertIn("issue #115", plan)
        self.assertIn("issue #242", plan)

    def test_volatile_inventory_snapshot_language_is_absent(self):
        inventory = re.compile(r"\b\d+\s+(?:skills|scripts|tests)\b", re.IGNORECASE)
        for path, document in self.documents.items():
            with self.subTest(path=path):
                self.assertIsNone(inventory.search(document))
                self.assertNotIn("Repository snapshot", document)


if __name__ == "__main__":
    unittest.main()
