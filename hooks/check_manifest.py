#!/usr/bin/env python3
"""Base-branch verification that a pull request head's Factory-managed files still
match the manifest that is already merged.

This runs in `aru-merge-policy`, which checks out the BASE branch and never the head.
Every managed file is read at the exact head through the GitHub contents API and
hashed here; nothing from the pull request is checked out, installed or executed.

What it proves: at that head, no Factory-managed file, and no managed block (the
governance markers of `AGENTS.md` and what lies between them), differs from
`.aru/manifest.json` on the base branch. What it does not prove: the authenticity of an upgrade head's new
manifest -- a head that bumps `.aru/factory-version` is judged against its own manifest,
which only the Factory's `merge_pr.py` can authenticate -- and nothing about a
repository administrator rewriting this workflow or the ruleset.

usage: check_manifest.py --pr N --expected-head SHA
exit 0  every managed file at the head matches the reference manifest
exit 2  refusal (prints one `::error::` line naming the reason)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

# Shared with scripts/manifest.py; tests pin the schema string and the excluded set equal.
SCHEMA = "aru.managed-files/v1"
MANIFEST_PATH = ".aru/manifest.json"
VERSION_PATH = ".aru/factory-version"
EXCLUDED = (".aru/review.json", ".aru/verify-project.sh", ".gitignore",
            ".github/ISSUE_TEMPLATE/governed-task.yml",
            ".github/PULL_REQUEST_TEMPLATE.md", MANIFEST_PATH)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class Refusal(RuntimeError):
    pass


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode:
        raise Refusal((result.stderr or result.stdout or "command failed").strip())
    return result.stdout.strip()


def repository_slug() -> str:
    raw = run(["gh", "repo", "view", "--json", "nameWithOwner"])
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed repository data") from exc
    slug = record.get("nameWithOwner") if isinstance(record, dict) else None
    if not isinstance(slug, str) or slug.count("/") != 1:
        raise Refusal("repository identity is unavailable")
    return slug


def pull_request(number: int) -> dict:
    raw = run(["gh", "pr", "view", str(number), "--json", "number,state,isDraft,headRefOid"])
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed pull request data") from exc
    if (
        not isinstance(record, dict)
        or record.get("number") != number
        or record.get("state") != "OPEN"
        or record.get("isDraft") is not False
        or not isinstance(record.get("headRefOid"), str)
        or not _SHA.fullmatch(record["headRefOid"])
    ):
        raise Refusal("pull request is unavailable or not ready")
    return record


def validate(text: str, origin: str) -> dict:
    """The same rules as scripts/manifest.py::parse, including the excluded set: a
    test pins the two equal, because a head that could name a consumer-owned file
    in its manifest would make this checker judge a file the Factory does not own."""
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise Refusal(f"{origin} manifest is not JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise Refusal(f"{origin} manifest schema is missing or unknown")
    for key in ("factory_version", "runner_profile"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise Refusal(f"{origin} manifest {key} is missing")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise Refusal(f"{origin} manifest lists no files")
    for path, value in files.items():
        if not isinstance(path, str) or not isinstance(value, str) or not _HEX64.match(value):
            raise Refusal(f"{origin} manifest entry for {path!r} is malformed")
        forbid(path, origin)
    validate_blocks(manifest.get("blocks", {}), files, origin)
    return manifest


def validate_blocks(blocks, files: dict, origin: str) -> None:
    if not isinstance(blocks, dict):
        raise Refusal(f"{origin} manifest blocks is not an object")
    for path, entry in blocks.items():
        if (not isinstance(path, str) or not isinstance(entry, dict)
                or set(entry) != {"begin", "end", "sha256"}
                or not all(isinstance(entry[k], str) and entry[k].strip() for k in ("begin", "end"))
                or not isinstance(entry["sha256"], str) or not _HEX64.match(entry["sha256"])):
            raise Refusal(f"{origin} manifest block entry for {path!r} is malformed")
        if entry["begin"] == entry["end"]:
            raise Refusal(f"{origin} manifest block for {path!r} has identical markers")
        forbid(path, origin)
        if path in files:
            raise Refusal(f"{origin} manifest lists {path!r} as both a file and a block")


def forbid(path: str, origin: str) -> None:
    if path in EXCLUDED or path.startswith(("/", "../")) or "/../" in path or "\\" in path:
        raise Refusal(f"{origin} manifest names a path it may not: {path!r}")


def extract_block(text: str, begin: str, end: str) -> str | None:
    """The same rules as scripts/manifest.py::extract_block: one begin line, one end
    line, begin first; both lines included; one trailing newline."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == begin]
    ends = [i for i, line in enumerate(lines) if line == end]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    return "\n".join(lines[starts[0]:ends[0] + 1]) + "\n"


def fetch(slug: str, path: str, head: str) -> bytes:
    """One managed file's exact bytes at the head. Never executed, only hashed."""
    endpoint = f"repos/{slug}/contents/{quote(path)}?ref={head}"
    try:
        raw = run(["gh", "api", endpoint])
    except Refusal as exc:
        raise Refusal(f"{path} is unreadable at the head: {exc}") from exc
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"GitHub returned malformed contents for {path}") from exc
    # `encoding` is absent (or "none") for a file over 1 MiB: refuse rather than
    # compare a truncated body against a hash of the whole file.
    if (
        not isinstance(record, dict)
        or record.get("type") != "file"
        or record.get("encoding") != "base64"
        or not isinstance(record.get("content"), str)
    ):
        raise Refusal(f"{path} at the head is missing, not a file, or too large to read")
    try:
        return base64.b64decode(record["content"], validate=False)
    except (binascii.Error, ValueError) as exc:
        raise Refusal(f"{path} at the head did not decode") from exc


def read_local(relative: str, origin: str) -> str:
    try:
        return Path(relative).read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(
            f"{origin} has no readable {relative}; sync this repository from the Factory"
        ) from exc


def reference_manifest(slug: str, head: str) -> tuple[dict, str]:
    """The manifest this head is judged against, and which branch it came from."""
    base_manifest_text = read_local(MANIFEST_PATH, "base branch")
    base_manifest = validate(base_manifest_text, "base branch")
    base_version = read_local(VERSION_PATH, "base branch").strip()
    head_manifest_text = fetch(slug, MANIFEST_PATH, head).decode("utf-8", "replace")
    head_version = fetch(slug, VERSION_PATH, head).decode("utf-8", "replace").strip()
    if head_manifest_text == base_manifest_text:
        if head_version != base_version:
            raise Refusal(
                "factory-version changed without a manifest change; managed files change "
                "only through init_project.py --sync at a Factory release"
            )
        return base_manifest, "base"
    if head_version == base_version:
        raise Refusal(
            "manifest changed without a factory-version change; managed files change only "
            "through init_project.py --sync at a Factory release"
        )
    head_manifest = validate(head_manifest_text, "head")
    if head_manifest["factory_version"] != head_version:
        raise Refusal("head manifest factory_version disagrees with .aru/factory-version")
    if head_manifest["runner_profile"] != base_manifest["runner_profile"]:
        raise Refusal("head manifest changes the runner profile")
    print(
        f"upgrade pull request: judged against its own manifest "
        f"(factory {base_version} -> {head_version}); its authenticity is verified by "
        f"the Factory's merge_pr.py, not here"
    )
    return head_manifest, "head"


def check(number: int, expected_head: str) -> str:
    if not _SHA.fullmatch(expected_head or ""):
        raise Refusal("expected head is malformed")
    pr = pull_request(number)
    head = str(pr["headRefOid"])
    if head.lower() != expected_head.lower():
        raise Refusal("expected head does not match the current PR head")
    slug = repository_slug()
    manifest, origin = reference_manifest(slug, head)
    findings = []
    for relative, expected in sorted(manifest["files"].items()):
        if hashlib.sha256(fetch(slug, relative, head)).hexdigest() != expected:
            findings.append(f"{relative}: content at the head differs from the manifest")
    blocks = manifest.get("blocks", {})
    for relative, entry in sorted(blocks.items()):
        text = fetch(slug, relative, head).decode("utf-8", "replace")
        block = extract_block(text, entry["begin"], entry["end"])
        if block is None:
            findings.append(f"{relative}: managed block markers are missing or duplicated at the head")
        elif hashlib.sha256(block.encode("utf-8")).hexdigest() != entry["sha256"]:
            findings.append(f"{relative}: managed block at the head differs from the manifest")
    if findings:
        raise Refusal(
            "Factory-managed files at the head diverge from the "
            f"{origin} branch's manifest: {'; '.join(findings)}"
        )
    refreshed = pull_request(number)
    if str(refreshed["headRefOid"]).lower() != head.lower():
        raise Refusal("pull request head changed during verification")
    return (
        f"{len(manifest['files'])} managed files and {len(blocks)} managed blocks at "
        f"{head[:7]} match the {origin} manifest "
        f"(factory {manifest['factory_version']}, profile {manifest['runner_profile']})"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    try:
        print(check(args.pr, args.expected_head), flush=True)
    except Refusal as exc:
        print(f"::error::{exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
