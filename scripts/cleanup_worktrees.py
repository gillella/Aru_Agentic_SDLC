#!/usr/bin/env python3
"""Remove only clean Factory worktrees whose PR is closed or merged.

A worktree is kept when git reports it locked (a worker holds it) or its
directory is missing, when it is dirty or holds ignored files other than
regenerable tool caches, when its PR is open or absent, or when its HEAD differs
from the PR head. A failure is recorded with the stage it happened in, the sweep
continues, and the command exits non-zero. A removal that succeeded is reported
even if deleting the local branch afterwards fails.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import KernelError, gh_json, git, json_print, primary_worktree

FACTORY_BRANCH = re.compile(r"^(?:feat|fix|docs)/issue-\d+-|^codex/")
# Regenerable tool caches that may be deleted with a worktree. Any other ignored path,
# including a .venv or node_modules that may hold local changes, keeps the worktree.
DISPOSABLE_IGNORED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".DS_Store"}


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


def local_state(path: Path) -> tuple[bool, list[str]]:
    """Whether the worktree is dirty, and which ignored paths are not disposable caches."""
    dirty, kept = False, []
    for line in git(["status", "--porcelain", "--ignored"], cwd=path).splitlines():
        if not line.startswith("!! "):
            dirty = True
            continue
        entry = line[3:].strip('"').rstrip("/")
        if not (set(entry.split("/")) & DISPOSABLE_IGNORED or entry.endswith(".pyc")):
            kept.append(entry)
    return dirty, kept


def inspect(path: Path, branch: str) -> tuple[str | None, dict | None]:
    """Return why the worktree must be retained (None when eligible) and its PR record."""
    dirty, kept = local_state(path)
    if dirty:
        return "dirty", None
    if kept:
        return "ignored data " + ", ".join(kept[:5]) + (" ..." if len(kept) > 5 else ""), None
    pr = pr_for_branch(branch)
    if not pr or (pr.get("state") != "CLOSED" and not pr.get("mergedAt")):
        return "PR open or absent", pr
    if pr.get("headRefOid") != git(["rev-parse", "HEAD"], cwd=path):
        return "head differs from preserved PR", pr
    return None, pr


def sweep(*, dry_run: bool = False) -> dict[str, list[str]]:
    root = primary_worktree()
    worktree_root = (root / ".worktrees").resolve()
    current = Path.cwd().resolve()
    records = parse_worktrees(git(["worktree", "list", "--porcelain"], cwd=root))
    removed: list[str] = []
    retained: list[str] = []
    failed: list[str] = []
    for record in records:
        path = Path(record.get("worktree", "")).resolve()
        branch = record.get("branch", "").removeprefix("refs/heads/")
        if path == root or path == current:
            continue
        if worktree_root not in path.parents or not FACTORY_BRANCH.search(branch):
            retained.append(f"{path}: outside Factory ownership")
            continue
        if "locked" in record:
            reason = record["locked"].strip()
            retained.append(f"{path}: locked" + (f" ({reason})" if reason else ""))
            continue
        if "prunable" in record:
            retained.append(f"{path}: directory missing; inspect, then git worktree prune")
            continue
        try:
            reason, pr = inspect(path, branch)
        except (KernelError, OSError) as exc:
            failed.append(f"{path}: inspect: {exc}")
            continue
        if reason:
            retained.append(f"{path}: {reason}")
            continue
        if not dry_run:
            try:
                # No --force: git itself still refuses a tree that became dirty or locked meanwhile.
                git(["worktree", "remove", str(path)], cwd=root)
            except (KernelError, OSError) as exc:
                failed.append(f"{path}: remove worktree: {exc}")
                continue
        removed.append(str(path))
        if not dry_run and pr and pr.get("mergedAt"):
            try:
                git(["branch", "-d", branch], cwd=root)
            except (KernelError, OSError) as exc:
                failed.append(f"{path}: worktree removed, local branch {branch} kept: {exc}")
    return {"removed": removed, "retained": retained, "failed": failed}


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
        for note in result["failed"]:
            print("failed " + note)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
