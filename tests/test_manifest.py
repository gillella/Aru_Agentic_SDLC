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
# A thin consumer's whole managed surface: three hashed files plus the AGENTS.md
# block. Everything else it used to carry -- the verifier, the touches parser, the
# hooks -- now runs from the Factory through the two workflow stubs.
MANAGED_FILES = (
    ".aru/factory-version",
    ".github/workflows/governed-pr.yml",
    ".github/workflows/merge-policy.yml",
)


def test_manifest_is_a_library_not_a_command():
    """Supported commands are capped at 14 and all fourteen are in use."""
    source = (ROOT / "scripts" / "manifest.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__"' not in source


@pytest.mark.parametrize("profile", PROFILES)
def test_managed_set_is_three_files_and_one_block(profile):
    """The managed set is still every framework file but the consumer-owned ones --
    which, for a thin consumer, is exactly three files and the AGENTS.md block."""
    managed = set(manifest.managed_files(profile))
    assert managed == set(init_project.framework_files(profile)) - set(manifest.EXCLUDED)
    assert managed == set(MANAGED_FILES) | {"AGENTS.md"}
    # AGENTS.md is in the managed set as a block: hashed between its markers, not whole.
    document = manifest.parse(manifest.canonical_path(profile).read_text(encoding="utf-8"))
    assert set(document["blocks"]) == set(manifest.BLOCKS) == {"AGENTS.md"}
    assert set(document["files"]) == set(MANAGED_FILES)
    # Consumer-owned or self-referential, so never hashed. The two .github templates
    # joined that list when consumers stopped receiving a framework-owned copy of them.
    for excluded in (".aru/review.json", ".aru/verify-project.sh", ".gitignore",
                     ".aru/manifest.json",
                     ".github/ISSUE_TEMPLATE/governed-task.yml",
                     ".github/PULL_REQUEST_TEMPLATE.md"):
        assert excluded in manifest.EXCLUDED, excluded
        assert excluded not in managed
    # Retired copies are not written any more, so none of them can be managed either.
    for retired in init_project.RETIRED:
        assert retired not in managed


@pytest.mark.parametrize("profile", PROFILES)
def test_committed_manifests_describe_only_the_thin_consumer_set(profile):
    """The manifest a consumer actually receives names the three managed files and the
    one managed block, and nothing under the hook or library directories the Factory
    stopped shipping -- a stale entry there would make every sync report drift."""
    document = manifest.parse(manifest.canonical_path(profile).read_text(encoding="utf-8"))
    assert set(document["files"]) == set(MANAGED_FILES)
    assert set(document["blocks"]) == {"AGENTS.md"}
    for path in (*document["files"], *document["blocks"]):
        assert not path.startswith((".aru/hooks/", ".aru/lib/")), path


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
    # A still-framework-owned file: the stub that pins which Factory action judges
    # this repository is exactly what a tampering head would want to rewrite.
    tampered = ".github/workflows/merge-policy.yml"
    (target / tampered).write_text("# tampered\n", encoding="utf-8")
    (target / manifest.MANIFEST_PATH).write_text("{}\n", encoding="utf-8")

    report = consumer.sync_report(target)
    assert tampered in report["stale"]
    assert manifest.MANIFEST_PATH in report["stale"]

    applied = consumer.sync_apply(target)
    assert tampered in applied["rewritten"]
    assert manifest.MANIFEST_PATH in applied["rewritten"]

    document = manifest.parse((target / manifest.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest.compare(
        target, document, lambda root, relative: (root / relative).read_bytes()
    ) == []


def test_the_schema_string_is_the_same_in_both_implementations():
    """Consumers get no scripts/manifest.py, so the base-branch hook duplicates it.

    There are two implementations now, not three: the consumer verifier stopped
    reading manifests when manifest verification moved into the merge-policy action,
    which runs `check_manifest.py` from the base branch against head bytes.
    """
    literal = f'"{manifest.SCHEMA}"'
    assert literal in (ROOT / "hooks" / "check_manifest.py").read_text(encoding="utf-8")
    assert literal not in (ROOT / "scripts" / "verify_consumer.sh").read_text(encoding="utf-8")
    action = (ROOT / ".github/actions/merge-policy/action.yml").read_text(encoding="utf-8")
    assert "hooks/check_manifest.py" in action


# --- AGENTS.md is a managed block: consumer text outside the markers survives ---

BEGIN, END = manifest.BLOCKS["AGENTS.md"]
TRAILING = "\n## Consumer policy\n\nWorkers run on the Mini. Keep credentials out of Git.\n"


def _read(root, relative):
    return (root / relative).read_bytes()


def test_consumer_text_after_the_end_marker_passes_compare(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    agents = target / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + TRAILING, encoding="utf-8")
    document = manifest.parse((target / manifest.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert manifest.compare(target, document, _read) == []


def test_a_tampered_block_and_broken_markers_are_findings(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    document = manifest.parse((target / manifest.MANIFEST_PATH).read_text(encoding="utf-8"))
    agents = target / "AGENTS.md"
    original = agents.read_text(encoding="utf-8")
    agents.write_text(original.replace("Require a valid Ready issue", "Skip the Ready issue"),
                      encoding="utf-8")
    assert manifest.compare(target, document, _read) == [
        "AGENTS.md: managed block differs from the manifest"]
    for broken in (original.replace(END + "\n", ""), original + BEGIN + "\n" + END + "\n"):
        agents.write_text(broken, encoding="utf-8")
        assert manifest.compare(target, document, _read) == [
            "AGENTS.md: managed block markers are missing or duplicated"]


def test_sync_rewrites_the_block_and_keeps_the_consumer_text(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    agents = target / "AGENTS.md"
    rendered = agents.read_text(encoding="utf-8")
    stale = rendered.replace("Require a valid Ready issue", "Skip the Ready issue")
    agents.write_text(stale + TRAILING, encoding="utf-8")

    report = consumer.sync_report(target)
    assert report["stale"] == ["AGENTS.md"]
    applied = consumer.sync_apply(target)
    assert applied["rewritten"] == ["AGENTS.md"]
    assert agents.read_text(encoding="utf-8") == rendered + TRAILING
    assert consumer.sync_report(target)["in_sync"]


def test_sync_refuses_agents_without_exactly_one_block(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    agents = target / "AGENTS.md"
    rendered = agents.read_text(encoding="utf-8")
    for broken in (rendered.replace(BEGIN + "\n", ""), rendered + rendered):
        agents.write_text(broken, encoding="utf-8")
        with pytest.raises(init_project.BootstrapError, match="exactly one managed block"):
            consumer.sync_report(target)
        with pytest.raises(init_project.BootstrapError, match="exactly one managed block"):
            consumer.sync_apply(target)
        assert agents.read_text(encoding="utf-8") == broken


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        {"AGENTS.md": {"begin": BEGIN, "end": END}},
        {"AGENTS.md": {"begin": BEGIN, "end": END, "sha256": "zz"}},
        {"AGENTS.md": {"begin": BEGIN, "end": BEGIN, "sha256": "0" * 64}},
        {"AGENTS.md": {"begin": "", "end": END, "sha256": "0" * 64}},
        {".aru/manifest.json": {"begin": BEGIN, "end": END, "sha256": "0" * 64}},
        {"../escape": {"begin": BEGIN, "end": END, "sha256": "0" * 64}},
        {".aru/factory-version": {"begin": BEGIN, "end": END, "sha256": "0" * 64}},
    ],
)
def test_parse_refuses_malformed_blocks(blocks):
    document = json.dumps({"schema": manifest.SCHEMA, "factory_version": "1.0.0",
                           "runner_profile": "self-hosted-mac",
                           "files": {".aru/factory-version": "0" * 64},
                           "blocks": blocks})
    with pytest.raises(manifest.ManifestError):
        manifest.parse(document)


def test_a_v230_manifest_without_blocks_still_parses():
    """The schema string did not change, so a base branch on v2.3.0 validates a v2.3.1
    upgrade head's manifest and the reverse."""
    document = json.dumps({"schema": manifest.SCHEMA, "factory_version": "2.3.0",
                           "runner_profile": "self-hosted-mac", "files": {"AGENTS.md": "0" * 64}})
    assert manifest.parse(document)["files"] == {"AGENTS.md": "0" * 64}


def test_the_block_markers_are_the_same_in_both_implementations():
    source = (ROOT / "hooks" / "check_manifest.py").read_text(encoding="utf-8")
    assert "extract_block" in source and '"blocks"' in source
    # The second half of the same fact: the consumer verifier holds no block logic,
    # so there is exactly one copy of these rules outside scripts/manifest.py.
    verifier = (ROOT / "scripts" / "verify_consumer.sh").read_text(encoding="utf-8")
    assert "extract_block" not in verifier and '"blocks"' not in verifier
    template = (ROOT / "templates" / "AGENTS.md").read_text(encoding="utf-8")
    assert template.startswith(BEGIN + "\n") and template.endswith(END + "\n")


def test_the_excluded_set_is_the_same_in_both_implementations():
    """A consumer gets no `scripts/`, so `hooks/check_manifest.py` duplicates the
    excluded set. If the two drift, a head can name a consumer-owned file in its
    manifest and the base-branch checker will judge a file the Factory does not own."""
    source = (ROOT / "hooks" / "check_manifest.py").read_text(encoding="utf-8")
    declared = source.split("EXCLUDED = (", 1)[1].split(")", 1)[0]
    for relative in manifest.EXCLUDED:
        if relative == manifest.MANIFEST_PATH:
            assert "MANIFEST_PATH" in declared
        else:
            assert f'"{relative}"' in declared, relative
    assert declared.count('"') // 2 == len(manifest.EXCLUDED) - 1
