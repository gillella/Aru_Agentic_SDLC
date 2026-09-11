#!/usr/bin/env python3
"""Remove only clean Factory worktrees whose PR is closed or merged.

A worktree is kept when git reports it locked (a worker holds it), when it is
dirty or holds ignored files other than disposable caches, when its PR is open
or absent, or when its HEAD differs from the PR head. A failure on one worktree
is recorded, the sweep continues, and the command exits non-zero.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import KernelError, gh_json, git, json_print, primary_worktree

FACTORY_BRANCH = re.compile(r"^(?:feat|fix|docs)/issue-\d+-|^codex/")
# Ignored paths that are safe to delete with a worktree; any other ignored path keeps it.
DISPOSABLE_IGNORED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "node_modules", ".DS_Store"}


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


def clean_one(root: Path, path: Path, branch: str, dry_run: bool) -> str | None:
    """Remove one eligible worktree, or return why it is retained."""
    dirty, kept = local_state(path)
    if dirty:
        return "dirty"
    if kept:
        return "ignored data " + ", ".join(kept[:5]) + (" ..." if len(kept) > 5 else "")
    pr = pr_for_branch(branch)
    if not pr or (pr.get("state") != "CLOSED" and not pr.get("mergedAt")):
        return "PR open or absent"
    if pr.get("headRefOid") != git(["rev-parse", "HEAD"], cwd=path):
        return "head differs from preserved PR"
    if not dry_run:
        # No --force: git itself still refuses a tree that became dirty or locked meanwhile.
        git(["worktree", "remove", str(path)], cwd=root)
        if pr.get("mergedAt"):
            git(["branch", "-d", branch], cwd=root)
    return None


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
            reason = clean_one(root, path, branch, dry_run)
        except KernelError as exc:
            failed.append(f"{path}: {exc}")
            continue
        if reason:
            retained.append(f"{path}: {reason}")
        else:
            removed.append(str(path))
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
