"""Manifest of Factory-managed consumer files: one hashing path for scaffold, sync,
the adoption inspector and the tests.

`templates/manifests/<profile>.json` is RENDERED from the framework files; do not hand
edit it. Regenerate with :data:`REGEN_COMMAND`. Library only: `tests/test_surface.py`
caps supported commands at 14 and all fourteen are in use.

What the manifest proves and what it does not: it detects drift and accidental edits in
a consumer's Factory-managed files. It cannot establish the authenticity of a manifest
that a pull request rewrites together with `.aru/verify.sh`, and it does not constrain a
repository administrator editing the workflow or the ruleset.
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
EXCLUDED = (".aru/review.json", ".aru/verify-project.sh", ".gitignore", MANIFEST_PATH)
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
    return sorted(path for path in files if path not in EXCLUDED)


def build(files: Mapping[str, str], *, profile: str, factory_version: str) -> dict[str, Any]:
    """Hash every managed file of one rendered scaffold."""
    return {
        "schema": SCHEMA,
        "factory_version": factory_version,
        "runner_profile": profile,
        "files": {path: digest(files[path].encode("utf-8")) for path in managed_paths(files)},
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
        if path in EXCLUDED or path.startswith(("/", "../")) or "/../" in path or "\\" in path:
            raise ManifestError(f"manifest names a path it may not: {path!r}")
    return manifest


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
    """Path -> expected bytes for one profile; the adoption inspector reads this."""
    import init_project

    files = init_project.framework_files(profile)
    return {path: files[path].encode("utf-8") for path in managed_paths(files)}


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
    return findings
