#!/usr/bin/env python3
"""Post-merge janitor: orphan worktrees, merged local branches, stale claims.

Invoked from merge_pr close-out and as
``python3 scripts/cleanup_worktrees.py``. Idempotent. Never ``rm -rf`` a path
that is not a git worktree of this repository. Dirty trees are left untouched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from common import run_cmd  # noqa: E402
import merge_pr  # noqa: E402

ISSUE_BRANCH_PREFIXES = ("feat/", "fix/", "chore/", "docs/")
REVIEW_BRANCH_PREFIX = "review-pr-"
RETAINED_DIR = os.path.join(".worktrees", ".retained")


def parse_worktrees(porcelain: str) -> list[dict]:
    rows = []
    for block in porcelain.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
        if fields.get("worktree"):
            rows.append(fields)
    return rows


def owned_worktree(path: str, repo_root: str) -> bool:
    marker = os.path.join(path, ".git")
    try:
        with open(marker, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return False
    if not text.startswith("gitdir: "):
        return False
    admin_dir = os.path.realpath(text.split(": ", 1)[1])
    allowed = os.path.realpath(os.path.join(repo_root, ".git", "worktrees"))
    try:
        return os.path.commonpath([admin_dir, allowed]) == allowed
    except ValueError:
        return False


def dirty_status(path: str) -> str | None:
    code, status, _ = run_cmd(
        [
            "git", "status", "--porcelain",
            "--untracked-files=all", "--ignored=matching",
        ],
        check=False, cwd=path,
    )
    if code != 0:
        return None
    return status


def under_worktrees(path: str, repo_root: str) -> bool:
    root = os.path.realpath(os.path.join(repo_root, ".worktrees"))
    real = os.path.realpath(path)
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:
        return False


def branch_name(fields: dict) -> str:
    ref = fields.get("branch") or ""
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ""


def gone_or_merged(repo_root: str, branch: str) -> bool:
    if not branch:
        return True
    code, out, _ = run_cmd(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        check=False, cwd=repo_root,
    )
    if code != 0 or not out.strip():
        return True
    merged, _, _ = run_cmd(
        ["git", "merge-base", "--is-ancestor", branch, "origin/main"],
        check=False, cwd=repo_root,
    )
    return merged == 0


def prune_orphan_worktrees(repo_root: str) -> list[str]:
    notes = []
    code, porcelain, err = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        return [f"could not list worktrees: {err.strip()}"]
    for fields in parse_worktrees(porcelain):
        path = fields["worktree"]
        if os.path.realpath(path) == os.path.realpath(repo_root):
            continue
        if not under_worktrees(path, repo_root):
            notes.append(f"skipped {path}: outside .worktrees/")
            continue
        if not owned_worktree(path, repo_root):
            notes.append(f"skipped {path}: not a worktree of this repo")
            continue
        status = dirty_status(path)
        if status is None:
            notes.append(f"skipped {path}: status unreadable")
            continue
        if status:
            notes.append(f"skipped dirty {path}")
            continue
        branch = branch_name(fields)
        if not gone_or_merged(repo_root, branch):
            notes.append(f"kept in-flight {path} ({branch})")
            continue
        rm_code, _, rm_err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if rm_code == 0:
            notes.append(f"removed {path}")
        else:
            notes.append(f"could not remove {path}: {rm_err.strip()}")
    return notes


def prune_retained_copies(repo_root: str) -> list[str]:
    notes = []
    retained = os.path.join(repo_root, RETAINED_DIR)
    if not os.path.isdir(retained):
        return notes
    for name in sorted(os.listdir(retained)):
        path = os.path.join(retained, name)
        if not os.path.isdir(path):
            continue
        if not owned_worktree(path, repo_root):
            notes.append(f"skipped retained {path}: not a worktree of this repo")
            continue
        status = dirty_status(path)
        if status is None or status:
            notes.append(f"skipped retained {path}: dirty or unreadable")
            continue
        try:
            shutil.rmtree(path)
            notes.append(f"removed retained {path}")
        except OSError as exc:
            notes.append(f"could not remove retained {path}: {exc}")
    return notes


def attached_branches(repo_root: str) -> set[str]:
    code, porcelain, _ = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        return set()
    names = set()
    for fields in parse_worktrees(porcelain):
        name = branch_name(fields)
        if name:
            names.add(name)
    return names


def is_janitor_branch(name: str) -> bool:
    return name.startswith(ISSUE_BRANCH_PREFIXES) or name.startswith(
        REVIEW_BRANCH_PREFIX
    )


def delete_merged_local_branches(repo_root: str) -> list[str]:
    notes = []
    attached = attached_branches(repo_root)
    code, listing, err = run_cmd(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return [f"could not list local branches: {err.strip()}"]
    for name in listing.splitlines():
        name = name.strip()
        if not is_janitor_branch(name) or name in attached:
            continue
        merged, _, _ = run_cmd(
            ["git", "merge-base", "--is-ancestor", name, "origin/main"],
            check=False, cwd=repo_root,
        )
        if merged != 0:
            continue
        rm_code, _, rm_err = run_cmd(
            ["git", "branch", "-d", name], check=False, cwd=repo_root
        )
        if rm_code == 0:
            notes.append(f"deleted local branch {name}")
        else:
            notes.append(f"could not delete {name}: {rm_err.strip()}")
    return notes


def _items_with_prefix(kind: str, state: str, prefix: str) -> list[int]:
    flag = "--state"
    cmd = ["gh", kind, "list", flag, state, "--limit", "200", "--json", "number,labels"]
    data = merge_pr._gh_json(cmd)
    if not isinstance(data, list):
        return []
    found = []
    for item in data:
        labels = [
            label.get("name", "") for label in (item.get("labels") or [])
        ]
        if any(name.startswith(prefix) for name in labels):
            found.append(int(item["number"]))
    return found


def clear_stale_claim_labels() -> list[str]:
    notes = []
    for number in _items_with_prefix("issue", "closed", "agent:"):
        ok, message = merge_pr.clear_issue_claims(number)
        notes.append(message if ok else f"issue #{number}: {message}")
    for number in _items_with_prefix("pr", "merged", "reviewer:"):
        ok, message = merge_pr.clear_review_claims(number)
        notes.append(message if ok else f"PR #{number} reviewer: {message}")
    for number in _items_with_prefix("pr", "merged", "merger:"):
        ok, message = merge_pr.clear_merger_claims(number)
        notes.append(message if ok else f"PR #{number} merger: {message}")
    return notes


def sweep(repo_root: str, include_labels: bool = True) -> tuple[bool, str]:
    notes = (
        prune_orphan_worktrees(repo_root)
        + prune_retained_copies(repo_root)
        + delete_merged_local_branches(repo_root)
    )
    if include_labels:
        notes = notes + clear_stale_claim_labels()
    if not notes:
        return True, "already clean"
    return True, "; ".join(notes)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune leftover worktrees, merged local branches, and stale claims."
    )
    parser.add_argument("--repo", help="Repository root (default: this clone)")
    args = parser.parse_args()
    repo_root = args.repo or merge_pr.repository_root() or os.getcwd()
    ok, message = sweep(repo_root)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
