"""Mechanical truth contract for the canonical factory documentation."""

import re
import unicodedata
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
PHASE_ROWS = (
    "| 0 | **Current** | Restore a trustworthy baseline and prove one real external deployment | #336 |",
    "| 1 | **Deferred** | Define and enforce the governed kernel boundary after Phase 0 evidence | #337 |",
    "| 2 | **Deferred** | Specify and enforce capability admission after Phase 1 and an approved PRD | #338 |",
    "| 3 | **Deferred** | Compose clarification and convergence stations after the kernel/admission gates | #339 |",
    "| 4 | **Deferred** | Generalize real delivery, observation, rollback, and learning from provider proof | #84 |",
    "| 5 | **Deferred** | Prove, measure, and release the isolated multi-project factory | #186 |",
)


def section(document, start, end):
    """Return one named document section, failing loudly if boundaries drift."""
    start_index = document.index(start)
    end_index = document.index(end, start_index + len(start))
    return document[start_index:end_index]



def display_width(text):
    """Rendered column count, not character count.

    Box-drawing borders line up by rendered width, so a wide or fullwidth
    character occupies two columns while `len()` would count it as one.
    """
    return sum(
        2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def ascii_boxes(document):
    """Return complete box-drawing rectangles and any unclosed opener lines.

    An unclosed opener is reported rather than skipped: dropping a closing
    border would otherwise leave the width check with nothing to inspect and
    pass vacuously.
    """
    lines = document.splitlines()
    boxes, unclosed, start = [], [], None
    for index, line in enumerate(lines):
        stripped = line.rstrip()
        if "\u250c" in stripped and stripped.endswith("\u2510"):
            if start is not None:
                unclosed.append(start + 1)
            start = index
        elif start is not None and "\u2514" in stripped and stripped.endswith("\u2518"):
            boxes.append(lines[start:index + 1])
            start = None
    if start is not None:
        unclosed.append(start + 1)
    return boxes, unclosed


def prose_wraps(document):
    """Yield (line, next_line) pairs joined only by a soft wrap.

    Duplicated words are an artifact of rewrapping one paragraph, so the pair
    must sit inside a single prose block. Blank lines, fenced code, and any
    line that opens a new structural element (heading, table row, quote, list
    item) end a block -- joining across those would flag a document whose one
    block ends with a word the next block happens to begin with.

    Headings and table rows are single-line blocks, so one appearing as the
    *first* line also ends the block. List items and blockquotes are not:
    prose after them is a lazy continuation of the same block, and a wrap
    there is a real one worth catching.
    """
    opens_block = re.compile(r"^\s*(?:[#>|]|[-*+]\s|\d+[.)]\s)")
    ends_block = re.compile(r"^\s*[#|]")
    lines = document.splitlines()
    fenced = False
    for index in range(len(lines) - 1):
        first, second = lines[index], lines[index + 1]
        if first.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or not first.strip() or not second.strip():
            continue
        if ends_block.match(first) or first.lstrip().startswith("```"):
            continue
        if opens_block.match(second) or second.lstrip().startswith("```"):
            continue
        yield first, second


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
                introduction = section(
                    document, document.splitlines()[0], "## Lifecycle status vocabulary"
                )
                self.assertRegex(
                    introduction,
                    re.compile(
                        r"roadmap epic #335.{0,240}Project Board.{0,120}live authority",
                        re.IGNORECASE | re.DOTALL,
                    ),
                )
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        roadmap = section(plan, "### 5.1 Current governed roadmap", "### 5.2")
        for row in PHASE_ROWS:
            self.assertEqual(roadmap.count(row), 1)

    def test_visualizer_status_claim_waits_for_its_own_issue(self):
        for path in (Path("README.md"), Path("docs/ARU-SOFTWARE-FACTORY.md")):
            introduction = section(
                self.documents[path],
                self.documents[path].splitlines()[0],
                "## Lifecycle status vocabulary",
            )
            with self.subTest(path=path):
                self.assertRegex(
                    introduction,
                    re.compile(
                        r"Issue #342 is the Current status-legend correction; "
                        r"until it merges, the visualizer may lag",
                        re.IGNORECASE,
                    ),
                )

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
        for path, document in self.documents.items():
            with self.subTest(path=path):
                for stale in (
                    "front of factory gap",
                    "**Weak / partial**",
                    "**Missing / weak**",
                    "Ship `fleet_status.py` early in Phase 4",
                ):
                    self.assertNotIn(stale, document)
                for capability in ("intake", "telemetry"):
                    self.assertRegex(
                        document,
                        re.compile(
                            rf"(?:Shipped.{{0,160}}{capability}|"
                            rf"{capability}.{{0,160}}Shipped)",
                            re.IGNORECASE | re.DOTALL,
                        ),
                    )
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
        scopes = {
            Path("README.md"): section(
                self.documents[Path("README.md")],
                "Current delivery truth",
                "\n---\n",
            ),
            Path("docs/ARU-SOFTWARE-FACTORY.md"): section(
                self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")],
                "#### Deployment truth",
                "### 5.2",
            ),
            Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md"): section(
                self.documents[
                    Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md")
                ],
                "## 5. Where Aru stands today",
                "**Strategic read:**",
            ),
        }
        for path, deployment in scopes.items():
            with self.subTest(path=path):
                for phrase in required:
                    self.assertIn(phrase, deployment)
                self.assertRegex(
                    deployment,
                    re.compile(
                        r"Audit-only.{0,180}(does not|not hosting|no runnable)",
                        re.IGNORECASE | re.DOTALL,
                    ),
                )

    def test_merge_default_matches_the_shipped_helper(self):
        scopes = {
            Path("README.md"): section(
                self.documents[Path("README.md")],
                "Current delivery truth",
                "\n---\n",
            ),
            Path("docs/ARU-SOFTWARE-FACTORY.md"): section(
                self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")],
                "### 5.1 Current governed roadmap",
                "### 5.2",
            ),
            Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md"): section(
                self.documents[
                    Path("docs/AGENTIC-SOFTWARE-FACTORY-RESEARCH-GUIDE.md")
                ],
                "## 5. Where Aru stands today",
                "**Strategic read:**",
            ),
        }
        merge_source = (ROOT / "scripts" / "merge_pr.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('parser.add_argument("--merge-method", default="merge"', merge_source)
        for path, merge_claim in scopes.items():
            with self.subTest(path=path):
                self.assertIn("merge_pr.py", merge_claim)
                self.assertIn("squash", merge_claim.lower())
                self.assertIn("opt-in", merge_claim.lower())
                self.assertRegex(
                    merge_claim,
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


    def test_no_duplicated_word_survives_a_line_rewrap(self):
        """A rewrap once left "so PR / PR #18" reading as "so PR PR #18"."""
        for path, document in self.documents.items():
            for first, second in prose_wraps(document):
                tail = first.split()[-1]
                head = second.split()[0]
                with self.subTest(path=path, wrap=f"{tail} / {head}"):
                    self.assertNotEqual(
                        tail, head,
                        f"'{tail}' is duplicated across a line wrap:\n"
                        f"{first}\n{second}",
                    )

    def test_ascii_box_borders_align(self):
        inspected = 0
        for path, document in self.documents.items():
            boxes, unclosed = ascii_boxes(document)
            with self.subTest(path=path):
                self.assertEqual(
                    unclosed, [],
                    f"box opened at line(s) {unclosed} is never closed",
                )
            inspected += len(boxes)
            for box in boxes:
                widths = {display_width(line.rstrip()) for line in box}
                with self.subTest(path=path, top=box[0].strip()[:40]):
                    self.assertEqual(
                        len(widths), 1,
                        f"box lines render at differing widths {sorted(widths)}:\n"
                        + "\n".join(box),
                    )
        # Guards the whole assertion against passing on zero rectangles.
        self.assertGreater(inspected, 0)


if __name__ == "__main__":
    unittest.main()
