#!/usr/bin/env python3
"""Read-only consumer compatibility report; never runs consumer verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import init_project  # noqa: E402


def command(argv: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def read_file(repo: Path, relative: str) -> bytes | None:
    path = repo / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != repo):
        return None
    try:
        path.resolve().relative_to(repo)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            limit = 2 * 1024 * 1024
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                return None
            content = handle.read(limit + 1)
            return content if len(content) <= limit else None
    except (OSError, ValueError):
        return None


def expected_files(profile: str) -> dict[str, bytes]:
    templates = {
        "AGENTS.md": "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml": "issue.yml",
        ".github/PULL_REQUEST_TEMPLATE.md": "pull_request.md",
        ".github/workflows/governed-pr.yml": "governed-pr.yml",
        ".aru/verify.sh": "verify.sh",
    }
    result = {}
    for target, name in templates.items():
        content = (ROOT / "templates" / name).read_text()
        result[target] = init_project.render_profile(content, profile).encode()
    for target, source in {
        ".aru/lib/touches.py": "scripts/touches.py",
        ".aru/hooks/pre-push": "hooks/pre-push",
        ".aru/hooks/enforce_touches.py": "hooks/enforce_touches.py",
    }.items():
        result[target] = (ROOT / source).read_bytes()
    return result


def compare(repo: Path, profile: str) -> list[dict]:
    result = []
    for relative, expected in expected_files(profile).items():
        actual = read_file(repo, relative)
        status = "missing-or-unreadable" if actual is None else (
            "matches" if actual == expected else "differs-review-customizations")
        result.append({"path": relative, "status": status,
                       "expected_sha256": hashlib.sha256(expected).hexdigest(),
                       "actual_sha256": hashlib.sha256(actual).hexdigest() if actual else None})
    return result


def verification(repo: Path) -> dict:
    relative = ".aru/verify-project.sh"
    content = read_file(repo, relative)
    status = "configured-not-executed"
    if content is None:
        status = "missing-or-unreadable"
    elif not content.strip() or b"ARU_CONSUMER_VERIFICATION_UNCONFIGURED" in content:
        status = "unconfigured"
    elif not os.access(repo / relative, os.X_OK):
        status = "not-executable"
    return {"path": relative, "status": status, "executed": False,
            "next_action": "Review real product checks, then run bash .aru/verify.sh."}


def github_readiness(repo: Path, owner: str) -> dict:
    raw = command(["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"], repo)
    try:
        identity = json.loads(raw or "null")
    except ValueError:
        identity = None
    if not isinstance(identity, dict) or not isinstance(identity.get("nameWithOwner"), str):
        return {"status": "unavailable", "next_action": "Check repository-scoped GitHub read access."}
    if identity["nameWithOwner"].split("/")[0].casefold() != owner.casefold():
        return {"status": "owner-mismatch", "repository": identity["nameWithOwner"]}
    reviewer_raw = command([sys.executable, str(ROOT / "scripts/create_pr.py"),
                            "--reviewer-status", "--json"], repo)
    try:
        reviewer = json.loads(reviewer_raw or "null")
    except ValueError:
        reviewer = None
    valid = isinstance(reviewer, dict) and reviewer.get("valid") is True
    return {"status": "read", "repository": identity["nameWithOwner"],
            "reviewer_configuration": "valid-not-probed" if valid else "unavailable-or-invalid",
            "next_action": "Verify linked Project, assigned runner capacity and a real governed pilot.",
            "runtime_readiness_proven": False}


def inspect(repo: Path, owner: str, online: bool = False) -> dict:
    repo = repo.expanduser().resolve(strict=True)
    git_root = command(["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"], repo)
    if not git_root or Path(git_root).resolve() != repo:
        raise ValueError("--repo must be the root of a Git checkout")
    profile = init_project.resolve_runner_profile(owner, None)
    files = compare(repo, profile)
    checks = verification(repo)
    attention = any(item["status"] != "matches" for item in files)
    attention |= checks["status"] != "configured-not-executed"
    canonical_status = command(["git", "-c", "core.fsmonitor=false", "status",
                                "--porcelain", "--untracked-files=all"], ROOT)
    return {"schema": "aru.consumer-inspection/v1", "read_only": True,
            "status": "attention" if attention else "local-files-match",
            "canonical_revision": command(["git", "rev-parse", "HEAD"], ROOT),
            "canonical_dirty": None if canonical_status is None else bool(canonical_status),
            "consumer_revision": command(["git", "rev-parse", "HEAD"], repo),
            "runner_profile": profile, "files": files, "verification": checks,
            "github": github_readiness(repo, owner) if online else {"status": "not-requested"},
            "adoption_proven": False,
            "next_action": "Review differences in a staging scaffold; preserve consumer policy and checks."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--github", action="store_true", help="Read GitHub and reviewer configuration")
    args = parser.parse_args()
    try:
        report = inspect(args.repo, args.owner, args.github)
    except (OSError, ValueError, init_project.BootstrapError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "read_only": True}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "local-files-match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
