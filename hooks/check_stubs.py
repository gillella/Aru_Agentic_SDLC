#!/usr/bin/env python3
"""Base-branch verification that a pull request head's workflow stubs still call
this Factory's actions.

A thin consumer carries no verification logic: its two workflows are stubs whose
single `uses:` line pins the Factory's composite action to a release tag. That
makes one new tampering route worth closing. A head may legitimately change the
pinned tag, which is what an upgrade is, but it must not repoint a check at
another repository, move it to another runner, change the declared profile, or
grant itself write permissions while keeping the required check's name.

This runs in `aru-merge-policy`, which checks out the BASE branch and never the
head. Both stubs are read at the exact head through the GitHub contents API and
compared, line by meaning, to the base branch's own copies; nothing from the
pull request is checked out, installed or executed.

usage: check_stubs.py --pr N --expected-head SHA
exit 0  both stubs at the head differ from the base only in the pinned tag
exit 2  refusal (prints one `::error::` line naming the reason)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

STUBS = {
    ".github/workflows/governed-pr.yml": "governed-pr",
    ".github/workflows/merge-policy.yml": "merge-policy",
}
PROFILE_MARKER = "# aru-runner-profile:"
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_ACTION = re.compile(
    r"^(?P<slug>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/\.github/actions/"
    r"(?P<action>[a-z-]+)@(?P<ref>v\d+\.\d+\.\d+)$"
)
_LOCAL_ACTION = re.compile(r"^\./\.github/actions/(?P<action>[a-z-]+)$")
_WRITE_PERMISSION = re.compile(
    r"^\s*(contents|issues|pull-requests|actions|checks|deployments|packages|id-token):"
    r"\s*(write|admin)\s*$"
)


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


def fetch(slug: str, path: str, head: str) -> str:
    """One stub's exact text at the head. Never executed, only parsed."""
    endpoint = f"repos/{slug}/contents/{quote(path)}?ref={head}"
    try:
        raw = run(["gh", "api", endpoint])
    except Refusal as exc:
        raise Refusal(f"{path} is unreadable at the head: {exc}") from exc
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal(f"GitHub returned malformed contents for {path}") from exc
    if (
        not isinstance(record, dict)
        or record.get("type") != "file"
        or record.get("encoding") != "base64"
        or not isinstance(record.get("content"), str)
    ):
        raise Refusal(f"{path} at the head is missing, not a file, or too large to read")
    try:
        return base64.b64decode(record["content"], validate=False).decode("utf-8", "replace")
    except (binascii.Error, ValueError) as exc:
        raise Refusal(f"{path} at the head did not decode") from exc


def uncommented(text: str) -> list[str]:
    return [re.sub(r"\s*#.*$", "", line).rstrip() for line in text.splitlines()]


def action_reference(text: str, path: str, action: str, origin: str) -> str:
    """The stub's single Aru action reference, ignoring `actions/checkout`."""
    references = [
        line.split("uses:", 1)[1].strip()
        for line in uncommented(text)
        if line.strip().startswith("uses:")
    ]
    aru = [r for r in references if r and not r.startswith("actions/checkout@")]
    if len(aru) != 1:
        raise Refusal(
            f"{origin} {path} must contain exactly one Aru action reference, found {len(aru)}"
        )
    reference = aru[0]
    match = _ACTION.match(reference) or _LOCAL_ACTION.match(reference)
    if not match or match.group("action") != action:
        raise Refusal(
            f"{origin} {path} must call <owner>/<repo>/.github/actions/{action}@vX.Y.Z, "
            f"not {reference}"
        )
    return reference


def declared(text: str, path: str, origin: str) -> dict[str, object]:
    """The facts a head may not change: which action, whose repository, which
    runner, which profile, and that permissions stay read-only."""
    profiles = [
        line[len(PROFILE_MARKER):].strip()
        for line in text.splitlines()
        if line.startswith(PROFILE_MARKER)
    ]
    if len(profiles) != 1 or not profiles[0]:
        raise Refusal(f"{origin} {path} must declare exactly one '{PROFILE_MARKER}' line")
    runners = sorted({
        line.strip()[len("runs-on:"):].strip()
        for line in uncommented(text)
        if line.strip().startswith("runs-on:")
    })
    if len(runners) != 1:
        raise Refusal(f"{origin} {path} must declare exactly one active runs-on")
    writes = [line.strip() for line in uncommented(text) if _WRITE_PERMISSION.match(line)]
    if writes:
        raise Refusal(f"{origin} {path} grants a write permission: {writes[0]}")
    return {"profile": profiles[0], "runner": runners[0]}


def compare_stub(slug: str, head: str, path: str, action: str) -> str | None:
    """None when the head's stub is identical in meaning to the base's; the
    upgrade note when only the pinned tag moved; a refusal otherwise."""
    try:
        base_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(
            f"base branch has no readable {path}; sync this repository from the Factory"
        ) from exc
    head_text = fetch(slug, path, head)
    if path == ".github/workflows/governed-pr.yml" and "pull_request_target" in head_text:
        raise Refusal(f"head {path} triggers on pull_request_target")
    base_reference = action_reference(base_text, path, action, "base branch")
    head_reference = action_reference(head_text, path, action, "head")
    base_facts = declared(base_text, path, "base branch")
    head_facts = declared(head_text, path, "head")
    for field, label in (("profile", "runner profile"), ("runner", "runs-on")):
        if base_facts[field] != head_facts[field]:
            raise Refusal(
                f"head {path} changes the {label} from {base_facts[field]!r} "
                f"to {head_facts[field]!r}"
            )
    base_match, head_match = _ACTION.match(base_reference), _ACTION.match(head_reference)
    if not (base_match and head_match):
        if base_reference != head_reference:
            raise Refusal(
                f"head {path} changes the action reference from {base_reference} "
                f"to {head_reference}"
            )
        return None
    if base_match.group("slug") != head_match.group("slug"):
        raise Refusal(
            f"head {path} calls {head_match.group('slug')}, not {base_match.group('slug')}"
        )
    if base_match.group("ref") != head_match.group("ref"):
        return f"{path}: {base_match.group('ref')} -> {head_match.group('ref')}"
    return None


def check(number: int, expected_head: str) -> str:
    if not _SHA.fullmatch(expected_head or ""):
        raise Refusal("expected head is malformed")
    pr = pull_request(number)
    head = str(pr["headRefOid"])
    if head.lower() != expected_head.lower():
        raise Refusal("expected head does not match the current PR head")
    slug = repository_slug()
    upgrades = [
        note
        for path, action in sorted(STUBS.items())
        if (note := compare_stub(slug, head, path, action)) is not None
    ]
    refreshed = pull_request(number)
    if str(refreshed["headRefOid"]).lower() != head.lower():
        raise Refusal("pull request head changed during verification")
    if upgrades:
        return (
            f"{len(STUBS)} stubs at {head[:7]} call this Factory; "
            "pinned tag changed: " + "; ".join(upgrades)
        )
    return f"{len(STUBS)} stubs at {head[:7]} call this Factory at the base branch's pinned tag"


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
