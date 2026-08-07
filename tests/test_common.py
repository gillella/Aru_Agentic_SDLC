import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import parse_touches, paths_overlap  # noqa: E402


class TouchMetadataTests(unittest.TestCase):
    def test_parser_ignores_prose_and_reads_metadata_line(self):
        body = """The new `touches:` declaration prevents collisions.

## Dependencies
touches: scripts/*, tests/test_common.py
"""

        self.assertEqual(
            parse_touches(body),
            ["scripts/*", "tests/test_common.py"],
        )

    def test_path_overlap_is_conservative_without_prefix_confusion(self):
        self.assertTrue(paths_overlap("src/*", "src/app/main.py"))
        self.assertFalse(paths_overlap("src/*", "src2/main.py"))
        self.assertFalse(paths_overlap("README.md", "README.md.bak"))


if __name__ == "__main__":
    unittest.main()
