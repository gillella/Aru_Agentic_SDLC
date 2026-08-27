#!/usr/bin/env python3
"""Remove only clean Factory worktrees whose PR is closed or merged."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import KernelError, gh_json, git, json_print, primary_worktree

FACTORY_BRANCH = re.compile(r"^(?:feat|fix|docs)/issue-\d+-|^codex/")


def parse_worktrees(raw: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def pr_for_branch(branch: str) -> dict | None:
    data = gh_json(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number,state,mergedAt,headRefOid",
        ]
    )
    if not isinstance(data, list):
        raise KernelError("pull-request inventory is malformed")
    if not data:
        return None
    if len(data) != 1:
        raise KernelError(f"branch {branch} has ambiguous pull-request history")
    return data[0]


def sweep(*, dry_run: bool = False) -> dict[str, list[str]]:
    root = primary_worktree()
    worktree_root = (root / ".worktrees").resolve()
    current = Path.cwd().resolve()
    records = parse_worktrees(git(["worktree", "list", "--porcelain"], cwd=root))
    removed: list[str] = []
    retained: list[str] = []
    for record in records:
        path = Path(record.get("worktree", "")).resolve()
        branch = record.get("branch", "").removeprefix("refs/heads/")
        if path == root or path == current:
            continue
        if worktree_root not in path.parents or not FACTORY_BRANCH.search(branch):
            retained.append(f"{path}: outside Factory ownership")
            continue
        if git(["status", "--porcelain"], cwd=path):
            retained.append(f"{path}: dirty")
            continue
        pr = pr_for_branch(branch)
        if not pr or (pr.get("state") != "CLOSED" and not pr.get("mergedAt")):
            retained.append(f"{path}: PR open or absent")
            continue
        head = git(["rev-parse", "HEAD"], cwd=path)
        if pr.get("headRefOid") != head:
            retained.append(f"{path}: head differs from preserved PR")
            continue
        if not dry_run:
            git(["worktree", "remove", str(path)], cwd=root)
            if pr.get("mergedAt"):
                git(["branch", "-d", branch], cwd=root)
        removed.append(str(path))
    return {"removed": removed, "retained": retained}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = sweep(dry_run=args.dry_run)
    except KernelError as exc:
        parser.error(str(exc))
    if args.json:
        json_print(result)
    else:
        for path in result["removed"]:
            print(("would remove " if args.dry_run else "removed ") + path)
        for note in result["retained"]:
            print("retained " + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
