"""The Factory-managed file manifest: one definition of the managed set, one hashing
path, and a committed per-profile manifest that cannot drift from the files it describes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import consumer
import init_project
import manifest
import policy

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("self-hosted-mac", "github-hosted")


def test_manifest_is_a_library_not_a_command():
    """Supported commands are capped at 14 and all fourteen are in use."""
    source = (ROOT / "scripts" / "manifest.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__"' not in source


@pytest.mark.parametrize("profile", PROFILES)
def test_managed_set_is_every_framework_file_but_consumer_owned(profile):
    managed = set(manifest.managed_files(profile))
    assert managed == set(init_project.framework_files(profile)) - set(manifest.EXCLUDED)
    # The three paths this issue added, and the omission that made the old
    # adoption inspector report a drifting governance workflow as matching.
    for required in (".github/workflows/merge-policy.yml", ".aru/factory-version",
                     ".aru/hooks/check_manifest.py"):
        assert required in managed
    for excluded in (".aru/review.json", ".aru/verify-project.sh", ".gitignore",
                     ".aru/manifest.json"):
        assert excluded not in managed


@pytest.mark.parametrize("profile", PROFILES)
def test_canonical_manifests_are_byte_identical_to_the_rendering(profile):
    committed = manifest.canonical_path(profile).read_text(encoding="utf-8")
    assert committed == manifest.render(manifest.rendered(profile)), (
        f"templates/manifests/{profile}.json has drifted from the framework files.\n"
        "The manifest is generated; do not hand-edit it. Regenerate with:\n"
        f"    {manifest.REGEN_COMMAND}"
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_scaffold_manifest_matches_hashing_the_tree(tmp_path, profile):
    """A fresh scaffold hashes to exactly the manifest it was given."""
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile=profile)
    document = manifest.parse((target / manifest.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert document["factory_version"] == policy.version()
    assert document["runner_profile"] == profile
    assert (target / manifest.VERSION_PATH).read_text(encoding="utf-8") == policy.version() + "\n"
    findings = manifest.compare(
        target, document, lambda root, relative: (root / relative).read_bytes()
    )
    assert findings == []


def test_render_is_deterministic_and_sorted():
    document = manifest.rendered("self-hosted-mac")
    text = manifest.render(document)
    assert text == manifest.render(json.loads(text))
    assert text.endswith("\n")
    keys = list(json.loads(text))
    assert keys == sorted(keys)
    assert list(json.loads(text)["files"]) == sorted(json.loads(text)["files"])


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        json.dumps({"schema": "other/v1", "factory_version": "1.0.0",
                    "runner_profile": "self-hosted-mac", "files": {"AGENTS.md": "0" * 64}}),
        json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                    "runner_profile": "self-hosted-mac", "files": {}}),
        json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                    "runner_profile": "self-hosted-mac", "files": {"AGENTS.md": "zz"}}),
        json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                    "runner_profile": "self-hosted-mac",
                    "files": {".aru/manifest.json": "0" * 64}}),
        json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                    "runner_profile": "self-hosted-mac", "files": {"../escape": "0" * 64}}),
        json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                    "files": {"AGENTS.md": "0" * 64}}),
    ],
)
def test_parse_refuses_malformed(document):
    with pytest.raises(manifest.ManifestError):
        manifest.parse(document)


def _newest_tag() -> str | None:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "tag", "--list", "v[0-9]*"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    versions = [tag[1:] for tag in result.stdout.split()
                if policy.RELEASE_VERSION.match(tag[1:])]
    return max(versions, key=lambda v: tuple(int(p) for p in v.split(".")), default=None)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_managed_change_requires_a_release_bump(profile):
    """Consumers upgrade at a release, so changed managed files must ship a new version.

    Skipped while the newest tag predates the canonical manifests; from the first tag
    that carries them it binds every later change.
    """
    tag = _newest_tag()
    if tag is None:
        pytest.skip("no v* tags are visible in this checkout")
    relative = manifest.canonical_path(profile).relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "show", f"v{tag}:{relative}"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        pytest.skip(f"v{tag} predates {relative}")
    tagged = json.loads(result.stdout)["files"]
    if tagged != json.loads(manifest.canonical_path(profile).read_text(encoding="utf-8"))["files"]:
        assert policy.version() != tag, (
            f"{relative} changed since v{tag}; bump [release] in scripts/policy.toml"
        )


def test_check_py_owns_no_file_list():
    """The adoption inspector's own list omitted merge-policy.yml; it keeps none now."""
    source = (ROOT / "integrations" / "adoption" / "check.py").read_text(encoding="utf-8")
    assert "manifest.managed_files" in source
    for literal in ('".aru/lib/touches.py"', '"governed-pr.yml"', '"hooks/pre-push"'):
        assert literal not in source


def test_sync_rewrites_a_tampered_manifest_and_file(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    (target / ".aru/lib/touches.py").write_text("# tampered\n", encoding="utf-8")
    (target / manifest.MANIFEST_PATH).write_text("{}\n", encoding="utf-8")

    report = consumer.sync_report(target)
    assert ".aru/lib/touches.py" in report["stale"]
    assert manifest.MANIFEST_PATH in report["stale"]

    applied = consumer.sync_apply(target)
    assert ".aru/lib/touches.py" in applied["rewritten"]
    assert manifest.MANIFEST_PATH in applied["rewritten"]

    document = manifest.parse((target / manifest.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest.compare(
        target, document, lambda root, relative: (root / relative).read_bytes()
    ) == []


def test_the_schema_string_is_the_same_in_all_three_implementations():
    """Consumers get no scripts/manifest.py, so verify.sh and the hook duplicate it."""
    literal = f'"{manifest.SCHEMA}"'
    assert literal in (ROOT / "hooks" / "check_manifest.py").read_text(encoding="utf-8")
    assert literal in (ROOT / "templates" / "verify.sh").read_text(encoding="utf-8")
