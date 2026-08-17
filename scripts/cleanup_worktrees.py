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
KNOWN_CACHE_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
})


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


def _known_cache_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").strip("/")
    if normalized.endswith(".pyc"):
        return True
    return any(part in KNOWN_CACHE_NAMES for part in normalized.split("/"))


def porcelain_blocks_prune(status: str | None) -> bool | None:
    """None if unreadable, True if real dirt, False if clean or cache-only."""
    if status is None:
        return None
    for line in status.splitlines():
        if not line:
            continue
        xy = line[:2]
        path = line[3:] if len(line) > 2 else ""
        if xy == "!!" and _known_cache_path(path):
            continue
        return True
    return False


def refresh_origin(repo_root: str) -> bool:
    code, _, _ = run_cmd(
        ["git", "fetch", "origin", "--prune"],
        check=False, cwd=repo_root,
    )
    return code == 0


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


def default_base_ref(repo_root: str) -> str:
    code, out, _ = run_cmd(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        check=False, cwd=repo_root,
    )
    if code == 0 and out.strip().startswith("refs/remotes/origin/"):
        return out.strip()
    for name in ("main", "master"):
        verify, _, _ = run_cmd(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"],
            check=False, cwd=repo_root,
        )
        if verify == 0:
            return f"refs/remotes/origin/{name}"
    return "refs/remotes/origin/main"


def merged_into_default(repo_root: str, branch: str) -> bool:
    """True only with positive ancestor evidence against the default branch."""
    if not branch:
        return False
    base = default_base_ref(repo_root)
    merged, _, _ = run_cmd(
        ["git", "merge-base", "--is-ancestor", branch, base],
        check=False, cwd=repo_root,
    )
    return merged == 0


def remote_branch_absent(repo_root: str, branch: str) -> bool | None:
    """True if origin lacks the branch, False if present, None if lookup failed."""
    code, out, _ = run_cmd(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return None
    return not out.strip()


def branch_has_upstream(repo_root: str, branch: str) -> bool:
    code, out, _ = run_cmd(
        ["git", "config", "--get", f"branch.{branch}.remote"],
        check=False, cwd=repo_root,
    )
    return code == 0 and bool(out.strip())


def stale_merged_branch(repo_root: str, branch: str) -> bool:
    """Prune only when upstream existed, origin deleted it, and it is merged.

    A new unpushed worktree is an ancestor of the default tip, so ancestor
    checks alone would delete in-flight trees. A failed ``ls-remote`` is not
    absence. Fast-forward leftovers still match the default SHA, so SHA
    inequality is not used as a gate.
    """
    if not branch or not branch_has_upstream(repo_root, branch):
        return False
    absent = remote_branch_absent(repo_root, branch)
    if absent is not True:
        return False
    return merged_into_default(repo_root, branch)


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
        blocked = porcelain_blocks_prune(dirty_status(path))
        if blocked is None:
            notes.append(f"skipped {path}: status unreadable")
            continue
        if blocked:
            notes.append(f"skipped dirty {path}")
            continue
        branch = branch_name(fields)
        if not stale_merged_branch(repo_root, branch):
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
        blocked = porcelain_blocks_prune(dirty_status(path))
        if blocked is None or blocked:
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
        if not stale_merged_branch(repo_root, name):
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


def linked_issues_unfinished(pr: dict) -> bool:
    """True unless every Closes #N issue is closed with status:done."""
    nums = merge_pr.linked_issues(pr.get("body") or "")
    if not nums:
        return False
    for num in nums:
        issue = merge_pr._gh_json(
            ["gh", "issue", "view", str(num), "--json", "state,labels"]
        )
        if issue is None:
            return True
        if (issue.get("state") or "").upper() == "OPEN":
            return True
        labels = {lab.get("name", "") for lab in (issue.get("labels") or [])}
        if "status:done" not in labels:
            return True
    return False


def merger_claim_still_needed(repo_root: str, pr_num: int) -> bool:
    """Keep merger: until issues, worktree, and remote branch are observably done.

    Ignores the merger: label itself so a lingering claim can still be cleared
    after the rest of close-out finished, without wiping claims on in-flight
    sibling merges.
    """
    pr = merge_pr._gh_json([
        "gh", "pr", "view", str(pr_num),
        "--json", "body,headRefName,labels,state,mergedAt",
    ])
    if not isinstance(pr, dict) or not merge_pr.is_merged(pr):
        return True
    if linked_issues_unfinished(pr):
        return True
    branch = pr.get("headRefName") or ""
    if not branch:
        return True
    code, porcelain, _ = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        return True
    path, _head = merge_pr.find_branch_worktree(porcelain, branch)
    if path:
        return True
    absent = remote_branch_absent(repo_root, branch)
    return absent is not True


def clear_stale_claim_labels(repo_root: str, retain_merger_pr: int | None = None) -> list[str]:
    notes = []
    for number in _items_with_prefix("issue", "closed", "agent:"):
        ok, message = merge_pr.clear_issue_claims(number)
        notes.append(message if ok else f"issue #{number}: {message}")
    for number in _items_with_prefix("pr", "merged", "reviewer:"):
        ok, message = merge_pr.clear_review_claims(number)
        notes.append(message if ok else f"PR #{number} reviewer: {message}")
    for number in _items_with_prefix("pr", "merged", "merger:"):
        if retain_merger_pr is not None and int(number) == int(retain_merger_pr):
            notes.append(f"kept merger claim on PR #{number}: current close-out incomplete")
            continue
        if merger_claim_still_needed(repo_root, number):
            notes.append(f"kept merger claim on PR #{number}: close-out incomplete")
            continue
        ok, message = merge_pr.clear_merger_claims(number)
        notes.append(message if ok else f"PR #{number} merger: {message}")
    return notes


def sweep(repo_root: str, include_labels: bool = True,
          retain_merger_pr: int | None = None) -> tuple[bool, str]:
    if not refresh_origin(repo_root):
        notes = ["fetch failed; keeping worktrees and local branches"]
        if include_labels:
            notes = notes + clear_stale_claim_labels(
                repo_root, retain_merger_pr=retain_merger_pr
            )
        return True, "; ".join(notes)
    notes = (
        prune_orphan_worktrees(repo_root)
        + prune_retained_copies(repo_root)
        + delete_merged_local_branches(repo_root)
    )
    if include_labels:
        notes = notes + clear_stale_claim_labels(
            repo_root, retain_merger_pr=retain_merger_pr
        )
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
