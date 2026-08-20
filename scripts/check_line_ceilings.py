#!/usr/bin/env python3
"""Enforce per-file line ceilings with each baseline stored in the file it governs.

Every source file must be at most LINE_CEILING lines. A legacy file that was
already over that ceiling when the guard landed carries its own allowance as a
`line-ceiling:` marker comment near the top of the file.

The marker placement is the point. The previous implementation held all ~58
baselines in a dict inside .github/workflows/ci.yml, which made that one
workflow file a global mutex: two agents growing two unrelated modules both had
to edit it, so the picker either serialised them or they collided on merge.
With the allowance colocated, raising it edits only the file being grown, and
the diff a reviewer sees for "this module got bigger" contains the allowance
change beside the code that consumed it.

The ratchet is unchanged in strength: a file may never exceed its recorded
allowance, and raising an allowance is still an explicit, reviewable edit.
"""

import argparse
import os
import re
import sys

# Files with no marker get this ceiling. New code is expected to live under it.
LINE_CEILING = 400

# The marker must be a comment occupying a whole line, so that a mention of the
# token inside a string, a regex, or prose cannot be read as an allowance. Every
# comment form the scanned suffixes use is accepted -- `#` (Python, shell),
# `//` and `/* */` (JS, CSS), `<!-- -->` (HTML) -- because a governed file whose
# comment syntax is not recognised silently falls back to the strict default and
# fails CI for no stated reason.
MARKER_RE = re.compile(
    r"^\s*(?:#+|//+|/\*|<!--)\s*line-ceiling:\s*(\d+)\s*(?:\*/|-->)?\s*$"
)

# Only the head of the file is scanned. A marker buried in the middle of a
# 3,000-line module is not reviewable, which defeats the purpose.
MARKER_SCAN_LINES = 15

# Every language the framework governs or bootstraps. init_project.py offers
# python, node, typescript, react, and go stacks, so a guard that scanned only
# the first three left a project this framework created free to grow .ts, .tsx,
# .jsx, and .go files without limit (#316). This tuple is the single definition;
# nothing else describes "a governed source file".
SOURCE_SUFFIXES = (
    ".py",
    ".sh",
    ".js", ".jsx", ".ts", ".tsx",
    ".go",
    ".css", ".html",
)

SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules", "__pycache__", "dist"}


def read_allowance(path):
    """Return this file's line allowance, taking the first valid marker found.

    Falls back to LINE_CEILING when no marker is present. A malformed marker
    simply fails to match, so the file falls back to the strict default and the
    guard reports it rather than trusting an unparsable number.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index >= MARKER_SCAN_LINES:
                    break
                match = MARKER_RE.match(line)
                if match:
                    return int(match.group(1))
    except OSError:
        # Unreadable file: fall back to the strict default so the caller
        # reports a violation rather than silently skipping the path.
        return LINE_CEILING
    return LINE_CEILING


def count_lines(path):
    with open(path, "rb") as fh:
        return sum(1 for _ in fh)


def iter_source_files(root="."):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(SOURCE_SUFFIXES):
                yield os.path.normpath(os.path.join(current, name))


def check_tree(root="."):
    """Return [(path, lines, allowed)] for every file over its allowance."""
    violations = []
    for path in iter_source_files(root):
        allowed = read_allowance(path)
        lines = count_lines(path)
        if lines > allowed:
            violations.append((path, lines, allowed))
    return sorted(violations)


def format_violations(violations):
    out = [f"error: {len(violations)} files exceed allowed line ceiling:"]
    for path, lines, allowed in violations:
        out.append(f"  {path}: {lines} lines (allowed max: {allowed})")
    out.append(
        "Raise a file's allowance by editing its own `line-ceiling:` marker "
        "comment, or split the file."
    )
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="tree to scan")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        # os.walk on a missing path yields nothing, which would report a clean
        # tree and pass the job without scanning a single file.
        print(f"error: --root {args.root!r} is not a directory", file=sys.stderr)
        return 1

    violations = check_tree(args.root)
    if violations:
        print(format_violations(violations), file=sys.stderr)
        return 1
    print("File line ceiling check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
