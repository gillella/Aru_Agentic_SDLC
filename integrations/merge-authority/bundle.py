#!/usr/bin/env python3
"""Build a credential-free, digest-pinned operator package from reviewed objects."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path

KERNEL_FILES = (
    "merge_pr.py", "merge_authority.py", "merge_state.py", "common.py",
    "check_ci.py", "fetch_pr_feedback.py", "touches.py",
)


def git_object(source: Path, commit: str, path: str) -> bytes:
    return subprocess.run(["/usr/bin/git", "--no-replace-objects", "-C", str(source), "show", f"{commit}:{path}"],
                          check=True, capture_output=True).stdout


def make_bundle(source: Path, revision: str, helper: str, gh: bytes, repository: str, issues: list[int], *, gh_sha256: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{40}", helper) or "0" * 40 in (revision, helper):
        raise ValueError("Full reviewed source and helper commits are required")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository) or ".." in repository:
        raise ValueError("Invalid repository")
    if not issues or any(type(n) is not int or n <= 0 for n in issues) or len(set(issues)) != len(issues):
        raise ValueError("Invalid issue scope")
    if not re.fullmatch(r"[0-9a-f]{64}", gh_sha256) or hashlib.sha256(gh).hexdigest() != gh_sha256:
        raise ValueError("CLI bytes differ from the independently reviewed digest")
    payload = {name: git_object(source, revision, "integrations/merge-authority/" + name)
               for name in ("request.py", "app.py", "install.py")}
    payload.update({"lib/" + name: git_object(source, helper, "scripts/" + name) for name in KERNEL_FILES})
    payload["bin/gh"] = gh
    payload["bin/authority"] = b"#!/bin/sh\nexec /usr/bin/python3 -I /private/var/db/aru-trusted-merge/app.py \"$@\"\n"
    payload["checkout/.git/HEAD"] = b"ref: refs/heads/main\n"
    payload["checkout/.git/config"] = (
        '[core]\n\trepositoryformatversion = 0\n\tbare = false\n\thooksPath = /dev/null\n'
        f'[remote "origin"]\n\turl = https://github.com/{repository}.git\n'
    ).encode()
    manifest = {"version": 1, "repository": repository, "issues": issues,
                "source_commit": revision, "helper_commit": helper,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in sorted(payload.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, data)
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--gh", type=Path, required=True)
    parser.add_argument("--gh-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue", type=int, action="append", required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--verify", type=Path)
    args = parser.parse_args()
    raw = make_bundle(args.source, args.revision, args.helper, args.gh.read_bytes(), args.repository, args.issue, gh_sha256=args.gh_sha256)
    # No credential file is read or included. The operator reviews both revisions
    # and the gh provenance before approving this exact package digest.
    if args.verify:
        if args.verify.read_bytes() != raw:
            raise ValueError("Package does not reproduce the reviewed Git objects and CLI digest")
        print(json.dumps({"verified": True, "sha256": hashlib.sha256(raw).hexdigest()}))
        return
    with args.output.open("xb") as stream:
        stream.write(raw)
    print(json.dumps({"package": str(args.output), "sha256": hashlib.sha256(raw).hexdigest(),
                      "source_commit": args.revision, "helper_commit": args.helper}))


if __name__ == "__main__":
    main()
