"""The version is declared once, in scripts/policy.toml; every restatement is tested here.

README's project-status line, the newest released heading in CHANGELOG.md and the newest
`v*` tag are restatements. Before this test README and CHANGELOG said v2.0.0 while three
newer tags existed, and the documentation test pinned the stale literal.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import policy

ROOT = Path(__file__).resolve().parents[1]
RELEASED_HEADING = re.compile(r"^## v(\d+\.\d+\.\d+) - .+ - (\d{4}-\d{2}-\d{2})$", re.M)
UNRELEASED_HEADING = re.compile(r"^## Unreleased\b", re.M)


def semver(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def newest_tag() -> str | None:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "tag", "--list", "v[0-9]*"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    versions = [tag[1:] for tag in result.stdout.split() if policy.RELEASE_VERSION.match(tag[1:])]
    return max(versions, key=semver, default=None)


def test_release_is_declared_once_and_well_formed():
    release = policy.release()
    assert policy.RELEASE_VERSION.match(release["version"])
    assert policy.version() == release["version"]


def test_readme_status_line_restates_the_declared_release():
    release = policy.release()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = (
        f"**Project status: v{release['version']} is the current released version "
        f"(released {release['released']}).**"
    )
    assert expected in readme
    # No other sentence may name a different current version.
    claims = re.findall(r"v(\d+\.\d+\.\d+) is the current released version", readme)
    assert claims == [release["version"]]


def test_changelog_newest_released_heading_is_the_declared_release():
    release = policy.release()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = RELEASED_HEADING.findall(changelog)
    assert headings, "CHANGELOG.md has no released heading of the form ## vX.Y.Z - title - date"
    assert headings[0] == (release["version"], release["released"])
    versions = [semver(found) for found, _ in headings]
    assert versions == sorted(versions, reverse=True), "released headings must descend"
    first_released = RELEASED_HEADING.search(changelog).start()
    unreleased = [match.start() for match in UNRELEASED_HEADING.finditer(changelog)]
    assert len(unreleased) <= 1, "one ## Unreleased section at most"
    assert all(position < first_released for position in unreleased)


def test_newest_tag_never_runs_ahead_of_the_declaration():
    tag = newest_tag()
    if tag is None:
        pytest.skip("no v* tags are visible in this checkout")
    declared = policy.version()
    assert semver(tag) <= semver(declared), (
        f"tag v{tag} is ahead of the declared {declared}; bump [release] before tagging"
    )
    if tag == declared:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## v{declared} - " in changelog


def test_operations_guide_documents_the_release_procedure():
    guide = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "## 18. Release the Factory" in guide
    assert "[release]" in guide
    assert "test_release_truth.py" in guide


@pytest.mark.parametrize(
    "table",
    [
        "",                                                        # no [release] at all
        "[release]\nversion = 1\nreleased = \"2026-09-13\"\n",     # not a string
        "[release]\nversion = \"v2.2.1\"\nreleased = \"2026-09-13\"\n",  # tag prefix
        "[release]\nversion = \"2.2\"\nreleased = \"2026-09-13\"\n",     # not MAJOR.MINOR.PATCH
        "[release]\nversion = \"2.2.1\"\n",                        # no date
        "[release]\nversion = \"2.2.1\"\nreleased = \"13-09-2026\"\n",   # not ISO
    ],
)
def test_malformed_release_refuses(table):
    base = {key: value for key, value in policy.load().items() if key != "release"}
    base.update(tomllib.loads(table))
    with pytest.raises(policy.PolicyError):
        policy.release(base)
