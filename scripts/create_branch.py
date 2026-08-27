#!/usr/bin/env python3
"""Create the one governed branch and isolated worktree for an issue."""

from __future__ import annotations

import argparse
import re

from claim_issue import safe_agent
from common import (
    AGENT_PREFIX,
    KernelError,
    git,
    issue,
    label_names,
    primary_worktree,
    run,
    status_of,
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:42] or "work"


def create_worktree(number: int, kind: str, agent: str) -> dict[str, str]:
    agent = safe_agent(agent)
    record = issue(number)
    if status_of(record) != "In Progress":
        raise KernelError("issue must be In Progress before branch creation")
    claimants = [name for name in label_names(record) if name.startswith(AGENT_PREFIX)]
    if claimants != [AGENT_PREFIX + agent]:
        raise KernelError("branch creation requires the exclusive issue claimant")

    root = primary_worktree()
    if git(["status", "--porcelain", "--untracked-files=no"], cwd=root):
        raise KernelError("tracked changes exist in the invoking worktree")
    branch = f"{kind}/issue-{number}-{slugify(str(record.get('title') or 'work'))}"
    path = root / ".worktrees" / branch.replace("/", "-")
    if path.exists():
        raise KernelError(f"worktree path already exists: {path}")
    existing = git(["branch", "--list", branch], cwd=root)
    if existing:
        raise KernelError(f"branch already exists: {branch}")
    remote_main = run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "show-ref",
            "--verify",
            "--quiet",
            "refs/remotes/origin/main",
        ],
        cwd=root,
        check=False,
    )
    base = "origin/main" if remote_main.returncode == 0 else "main"
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path), base], cwd=root)
    return {"branch": branch, "path": str(path), "base": base}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--type", choices=("feat", "fix", "docs"), required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    try:
        result = create_worktree(args.issue, args.type, args.agent)
    except KernelError as exc:
        parser.error(str(exc))
    print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
