"""Manifest of Factory-managed consumer files: one hashing path for scaffold, sync,
the adoption inspector and the tests.

`templates/manifests/<profile>.json` is RENDERED from the framework files; do not hand
edit it. Regenerate with :data:`REGEN_COMMAND`. Library only: `tests/test_surface.py`
caps supported commands at 14 and all fourteen are in use.

What the manifest proves and what it does not: it detects drift and accidental edits in
a consumer's Factory-managed files. It cannot establish the authenticity of a manifest
that a pull request rewrites together with its stubs, and it does not constrain a
repository administrator editing the workflow or the ruleset.

`AGENTS.md` is managed as a BLOCK, not a file: only the text between the governance
markers (both marker lines included) is hashed, under `blocks` rather than `files`, so a
consumer's own instructions after the END marker survive verification and `--sync`.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "aru.managed-files/v1"
MANIFEST_PATH = ".aru/manifest.json"
VERSION_PATH = ".aru/factory-version"
# Consumer-owned or self-referential: never hashed.
EXCLUDED = (".aru/review.json", ".aru/verify-project.sh", ".gitignore",
            ".github/ISSUE_TEMPLATE/governed-task.yml",
            ".github/PULL_REQUEST_TEMPLATE.md", MANIFEST_PATH)
# Managed as a marked block inside a consumer-owned file: path -> (begin line, end line).
# Shared with hooks/check_manifest.py and scripts/verify_consumer.sh; a test pins them equal.
BLOCKS = {
    "AGENTS.md": ("<!-- BEGIN ARU_SDLC_GOVERNANCE -->", "<!-- END ARU_SDLC_GOVERNANCE -->"),
}
CANONICAL_DIR = Path(__file__).resolve().parents[1] / "templates" / "manifests"
REGEN_COMMAND = (
    "python3 -c \"import sys; sys.path.insert(0, 'scripts'); "
    "import manifest; manifest.write_canonical()\""
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """The manifest is missing, malformed or does not describe these files."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def managed_paths(files: Mapping[str, str]) -> list[str]:
    """Whole-file managed paths: everything rendered except consumer-owned and blocks."""
    return sorted(path for path in files if path not in EXCLUDED and path not in BLOCKS)


def extract_block(text: str, begin: str, end: str) -> str | None:
    """The managed block of a file: the begin-marker line through the end-marker line,
    both included, with one trailing newline. None when either marker is absent,
    appears more than once, or the end precedes the begin -- the caller refuses rather
    than guessing which text the Factory owns."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line == begin]
    ends = [index for index, line in enumerate(lines) if line == end]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    return "\n".join(lines[starts[0]:ends[0] + 1]) + "\n"


def replace_block(text: str, begin: str, end: str, block: str) -> str | None:
    """`text` with its managed block swapped for `block`; everything outside the
    markers is kept byte for byte. None under the same conditions as extract_block."""
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == begin]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == end]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    before = "".join(lines[:starts[0]])
    after = "".join(lines[ends[0] + 1:])
    return before + block + after


def build(files: Mapping[str, str], *, profile: str, factory_version: str) -> dict[str, Any]:
    """Hash every managed file, and every managed block, of one rendered scaffold."""
    blocks: dict[str, dict[str, str]] = {}
    for path, (begin, end) in sorted(BLOCKS.items()):
        block = extract_block(files[path], begin, end)
        if block is None:
            raise ManifestError(f"rendered {path} does not carry exactly one managed block")
        blocks[path] = {"begin": begin, "end": end, "sha256": digest(block.encode("utf-8"))}
    return {
        "schema": SCHEMA,
        "factory_version": factory_version,
        "runner_profile": profile,
        "files": {path: digest(files[path].encode("utf-8")) for path in managed_paths(files)},
        "blocks": blocks,
    }


def render(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def parse(text: str) -> dict[str, Any]:
    """Validate a manifest document; anything unexpected refuses."""
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"manifest is not JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ManifestError("manifest schema is missing or unknown")
    for key in ("factory_version", "runner_profile"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ManifestError(f"manifest {key} is missing")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ManifestError("manifest lists no files")
    for path, value in files.items():
        if not isinstance(path, str) or not isinstance(value, str) or not _HEX64.match(value):
            raise ManifestError(f"manifest entry for {path!r} is malformed")
        _refuse_forbidden_path(path)
    # `blocks` is optional so a v2.3.0 manifest, which listed AGENTS.md under files,
    # still parses: the schema string did not change.
    _validate_blocks(manifest.get("blocks", {}), files)
    return manifest


def _validate_blocks(blocks: Any, files: Mapping[str, Any]) -> None:
    if not isinstance(blocks, dict):
        raise ManifestError("manifest blocks is not an object")
    for path, entry in blocks.items():
        if (not isinstance(path, str) or not isinstance(entry, dict)
                or set(entry) != {"begin", "end", "sha256"}
                or not all(isinstance(entry[key], str) and entry[key].strip()
                           for key in ("begin", "end"))
                or not isinstance(entry["sha256"], str) or not _HEX64.match(entry["sha256"])):
            raise ManifestError(f"manifest block entry for {path!r} is malformed")
        if entry["begin"] == entry["end"]:
            raise ManifestError(f"manifest block for {path!r} has identical markers")
        _refuse_forbidden_path(path)
        if path in files:
            raise ManifestError(f"manifest lists {path!r} as both a file and a block")


def _refuse_forbidden_path(path: str) -> None:
    if path in EXCLUDED or path.startswith(("/", "../")) or "/../" in path or "\\" in path:
        raise ManifestError(f"manifest names a path it may not: {path!r}")


def canonical_path(profile: str) -> Path:
    return CANONICAL_DIR / f"{profile}.json"


def rendered(profile: str) -> dict[str, Any]:
    """The manifest the current templates and hooks produce for one profile."""
    import init_project  # local import: init_project imports this module
    import policy

    files = init_project.framework_files(profile, canonical=False)
    return build(files, profile=profile, factory_version=policy.version())


def write_canonical() -> list[Path]:
    """Rewrite every committed canonical manifest from the current framework files."""
    import init_project

    written = []
    CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
    for profile in sorted(init_project.RUNNER_PROFILES):
        target = canonical_path(profile)
        target.write_text(render(rendered(profile)), encoding="utf-8")
        written.append(target)
    return written


def managed_files(profile: str) -> dict[str, bytes]:
    """Path -> expected bytes for one profile; the adoption inspector reads this.
    For a block path the bytes are the managed block, not the whole file."""
    import init_project

    files = init_project.framework_files(profile)
    expected = {path: files[path].encode("utf-8") for path in managed_paths(files)}
    for path, (begin, end) in BLOCKS.items():
        block = extract_block(files[path], begin, end)
        if block is None:
            raise ManifestError(f"rendered {path} does not carry exactly one managed block")
        expected[path] = block.encode("utf-8")
    return expected


def compare(
    root: Path,
    manifest: Mapping[str, Any],
    read: Callable[[Path, str], bytes | None],
) -> list[str]:
    """Findings for a checkout: one line per managed file that is missing or differs.

    `read(root, relative) -> bytes | None` is injected so the adoption inspector and
    the tests share the comparison without sharing a filesystem policy.
    """
    findings = []
    for relative, expected in sorted(manifest["files"].items()):
        data = read(root, relative)
        if data is None:
            findings.append(f"{relative}: missing, unreadable, not a regular file, or oversized")
        elif digest(data) != expected:
            findings.append(f"{relative}: content differs from the manifest")
    for relative, entry in sorted(manifest.get("blocks", {}).items()):
        data = read(root, relative)
        if data is None:
            findings.append(f"{relative}: missing, unreadable, not a regular file, or oversized")
            continue
        block = extract_block(data.decode("utf-8", "replace"), entry["begin"], entry["end"])
        if block is None:
            findings.append(f"{relative}: managed block markers are missing or duplicated")
        elif digest(block.encode("utf-8")) != entry["sha256"]:
            findings.append(f"{relative}: managed block differs from the manifest")
    return findings
