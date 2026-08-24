# line-ceiling: 430
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
    character occupies two columns while `len()` would count it as one, and
    a nonspacing or enclosing mark stacks onto the preceding character for
    zero columns while `len()` would count it as one.
    """
    width = 0
    for char in text:
        if unicodedata.category(char) in ("Mn", "Me"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
    return width


def turns_a_flow_line(lines, index, column):
    """True when a bottom-left corner is arrow glue rather than a box foot.

    A box foot stands at the bottom of a wall that rises to the box's own
    top-left corner. The arrows drawn between boxes reuse the same corner
    glyph to turn a flow line sideways, and those strokes rise to a tee
    instead, so reading the column upward tells the two apart.
    """
    for above in range(index - 1, -1, -1):
        line = lines[above]
        char = line[column] if column < len(line) else " "
        if char != "\u2502":
            return char in "\u252c\u2534\u253c\u251c\u2524"
    return False


def ascii_boxes(document):
    """Return complete box-drawing rectangles and any unpaired border lines.

    A border missing its partner -- an opener never closed, or a foot with no
    opener -- is reported by line number rather than skipped: dropping it
    would otherwise leave the width check with nothing to inspect and pass
    vacuously on genuinely broken box-drawing output.
    """
    lines = document.splitlines()
    boxes, unpaired, start = [], [], None
    for index, line in enumerate(lines):
        stripped = line.rstrip()
        if "\u250c" in stripped and stripped.endswith("\u2510"):
            if start is not None:
                unpaired.append(start + 1)
            start = index
        elif "\u2514" in stripped and stripped.endswith("\u2518"):
            if start is not None:
                boxes.append(lines[start:index + 1])
                start = None
            elif not turns_a_flow_line(lines, index, stripped.index("\u2514")):
                unpaired.append(index + 1)
    if start is not None:
        unpaired.append(start + 1)
    return boxes, unpaired


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

    Markdown spells a code fence with either backticks or tildes, and only
    the character that opened a fence can close it, so both markers are
    tracked and a `~~~` block is skipped exactly like a ``` one.
    """
    opens_block = re.compile(r"^\s*(?:[#>|]|[-*+]\s|\d+[.)]\s)")
    ends_block = re.compile(r"^\s*[#|]")
    fence = re.compile(r"^\s*(`{3,}|~{3,})")
    lines = document.splitlines()
    open_fence = None
    for index in range(len(lines) - 1):
        first, second = lines[index], lines[index + 1]
        first_fence = fence.match(first)
        if first_fence:
            marker = first_fence.group(1)[0]
            if open_fence is None:
                open_fence = marker
            elif open_fence == marker:
                open_fence = None
        if open_fence is not None or not first.strip() or not second.strip():
            continue
        if ends_block.match(first) or first_fence:
            continue
        if opens_block.match(second) or fence.match(second):
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

    def test_current_review_policy_keeps_agents_out_of_review_role(self):
        plan = self.documents[Path("docs/ARU-SOFTWARE-FACTORY.md")]
        introduction = section(
            plan,
            plan.splitlines()[0],
            "## Lifecycle status vocabulary",
        )
        normalized_intro = introduction.replace("> ", "")
        self.assertRegex(
            normalized_intro,
            re.compile(
                r"implement,\s+remediate,\s+and\s+mechanically\s+merge.*they never review",
                re.IGNORECASE | re.DOTALL,
            ),
        )
        operator = section(plan, "## 4. The operator visibility and intervention interface", "### 4.1")
        self.assertNotIn("independent-agent review", operator)
        principle = section(plan, "### 4.3 The operating principle", "---")
        self.assertIn("CodeRabbit reviews", principle)
        self.assertNotIn("A distinct agent reviews", principle)
        intervention = section(plan, "### 4.1 The one mandatory human intervention", "### 4.2")
        self.assertIn("implementation/remediation/mechanical-merge loop", intervention)
        self.assertNotIn("implementation/review/remediation loop", intervention)

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
            boxes, unpaired = ascii_boxes(document)
            with self.subTest(path=path):
                self.assertEqual(
                    unpaired, [],
                    f"box border(s) at line(s) {unpaired} have no partner",
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


class DocumentParserTests(unittest.TestCase):
    """Focused coverage for the helpers the freshness contract leans on."""

    # The final `\u2514` turns a flow line rather than standing on a box wall.
    GLUE = """\
┌───┐   ┌───┐
│ a │   │ b │
└─┬─┘   └─┬─┘
  │       │
  └───┬───┘
"""

    def test_ascii_boxes_pairs_borders_and_reports_the_unpaired(self):
        box = ["┌──┐", "ok", "└──┘"]
        cases = {
            "complete box": ("┌──┐\nok\n└──┘\n", [box], []),
            "unclosed opener": ("intro\n┌──┐\nok\n", [], [2]),
            "orphan foot after a box": ("┌──┐\nok\n└──┘\n\n└──┘\n", [box], [5]),
            "foot before any opener": ("└──┘\n", [], [1]),
            "foot on a wall rising to nothing": ("│ok│\n└──┘\n", [], [2]),
            "arrow glue turning a flow line": (self.GLUE, [self.GLUE.splitlines()[:3]], []),
        }
        for name, (document, boxes, unpaired) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(ascii_boxes(document), (boxes, unpaired))

    def test_marks_and_wide_characters_get_their_rendered_width(self):
        self.assertEqual(display_width("e\u0301"), display_width("e"))
        self.assertEqual(display_width("\u3042"), 2)

    def test_prose_wraps_reads_a_tilde_fence_like_a_backtick_fence(self):
        for document in ("~~~\nword\nword\n~~~\n", "~~~\n```\nword\nword\n```\n~~~\n"):
            with self.subTest(document=document):
                self.assertEqual(list(prose_wraps(document)), [])
        self.assertEqual(
            list(prose_wraps("alpha beta\nbeta gamma\n\n~~~\ncode\n~~~\n")),
            [("alpha beta", "beta gamma")],
        )


if __name__ == "__main__":
    unittest.main()
