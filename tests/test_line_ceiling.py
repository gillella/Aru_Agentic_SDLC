"""Tests for the per-file line-ceiling guard (issue #308).

The behaviour under test is the ratchet: a file may never exceed its recorded
allowance, and the allowance lives in the file it governs so that growing two
unrelated files never requires editing one shared path.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_line_ceilings as guard


def write(root, relpath, lines, header=None):
    """Write a file of `lines` total lines, optionally starting with `header`."""
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = []
    if header is not None:
        body.extend(header.split("\n"))
    body.extend(f"x = {i}" for i in range(lines - len(body)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body) + "\n")
    return path


class LineCeilingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def paths(self, violations):
        return {os.path.relpath(p, self.root) for p, _, _ in violations}

    def test_new_file_at_ceiling_passes(self):
        write(self.root, "a.py", 400)
        self.assertEqual(guard.check_tree(self.root), [])

    def test_new_file_over_ceiling_fails(self):
        # The 400-line default is unchanged for files carrying no marker.
        write(self.root, "a.py", 401)
        self.assertEqual(self.paths(guard.check_tree(self.root)), {"a.py"})

    def test_marker_raises_allowance_for_that_file_only(self):
        write(self.root, "legacy.py", 500, header="# line-ceiling: 500")
        write(self.root, "new.py", 401)
        self.assertEqual(self.paths(guard.check_tree(self.root)), {"new.py"})

    def test_file_grown_beyond_its_marker_fails(self):
        # The anti-bloat guarantee: a marker is a ceiling, not a licence.
        write(self.root, "legacy.py", 501, header="# line-ceiling: 500")
        violations = guard.check_tree(self.root)
        self.assertEqual(self.paths(violations), {"legacy.py"})
        self.assertEqual(violations[0][1:], (501, 500))

    def test_two_files_grow_without_a_common_edited_path(self):
        """The defect #308 fixes: growing two baselined files used to require
        both agents to edit .github/workflows/ci.yml."""
        write(self.root, "one.py", 600, header="# line-ceiling: 600")
        write(self.root, "two.py", 700, header="# line-ceiling: 700")
        self.assertEqual(guard.check_tree(self.root), [])
        # Each allowance is readable from, and only from, its own file.
        self.assertEqual(guard.read_allowance(os.path.join(self.root, "one.py")), 600)
        self.assertEqual(guard.read_allowance(os.path.join(self.root, "two.py")), 700)

    def test_marker_accepted_in_every_scanned_comment_form(self):
        # A governed file whose comment syntax is unrecognised would fall back
        # to the strict default and fail CI for no stated reason.
        write(self.root, "a.js", 450, header="// line-ceiling: 450")
        write(self.root, "b.css", 450, header="/* line-ceiling: 450 */")
        write(self.root, "c.html", 450, header="<!-- line-ceiling: 450 -->")
        self.assertEqual(guard.check_tree(self.root), [])

    def test_missing_root_fails_rather_than_reporting_a_clean_tree(self):
        self.assertEqual(guard.main(["--root", os.path.join(self.root, "nope")]), 1)

    def test_marker_below_shebang_is_found(self):
        write(self.root, "a.sh", 450, header="#!/usr/bin/env bash\n# line-ceiling: 450")
        self.assertEqual(guard.check_tree(self.root), [])

    def test_token_inside_a_string_is_not_a_marker(self):
        # A mention of the token in code or prose must not grant an allowance.
        path = write(self.root, "a.py", 401, header='TOKEN = "line-ceiling: 9999"')
        self.assertEqual(guard.read_allowance(path), guard.LINE_CEILING)
        self.assertEqual(self.paths(guard.check_tree(self.root)), {"a.py"})

    def test_marker_buried_past_the_scan_window_is_ignored(self):
        header = "\n".join(["# pad"] * 20 + ["# line-ceiling: 9999"])
        path = write(self.root, "a.py", 401, header=header)
        self.assertEqual(guard.read_allowance(path), guard.LINE_CEILING)

    def test_malformed_marker_falls_back_to_the_strict_default(self):
        path = write(self.root, "a.py", 401, header="# line-ceiling: many")
        self.assertEqual(guard.read_allowance(path), guard.LINE_CEILING)
        self.assertEqual(self.paths(guard.check_tree(self.root)), {"a.py"})

    def test_skipped_directories_are_not_scanned(self):
        write(self.root, ".worktrees/a.py", 5000)
        write(self.root, "node_modules/b.js", 5000)
        self.assertEqual(guard.check_tree(self.root), [])

    def test_typescript_and_go_sources_are_governed(self):
        """#316: the bootstrap offers node, typescript, react and go stacks, so a
        guard that scans only .py/.js/.sh/.css/.html lets those grow unbounded."""
        for name in ("a.ts", "b.tsx", "c.jsx", "d.go"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                write(root, name, 401)
                self.assertEqual(
                    {os.path.relpath(p, root) for p, _, _ in guard.check_tree(root)},
                    {name})

    def test_markers_work_in_typescript_and_go(self):
        write(self.root, "a.ts", 450, header="// line-ceiling: 450")
        write(self.root, "b.go", 450, header="// line-ceiling: 450")
        self.assertEqual(guard.check_tree(self.root), [])

    def test_non_source_suffixes_are_ignored(self):
        write(self.root, "notes.md", 5000)
        self.assertEqual(guard.check_tree(self.root), [])

    def test_main_exit_codes(self):
        write(self.root, "ok.py", 10)
        self.assertEqual(guard.main(["--root", self.root]), 0)
        write(self.root, "bad.py", 401)
        self.assertEqual(guard.main(["--root", self.root]), 1)


class RepositoryTreeTests(unittest.TestCase):
    def test_repository_passes_its_own_guard(self):
        repo = os.path.join(os.path.dirname(__file__), "..")
        self.assertEqual(guard.check_tree(repo), [])


if __name__ == "__main__":
    unittest.main()
