#!/usr/bin/env python3
"""Post-merge janitor: orphan worktrees, merged local branches, stale claims.

Invoked from merge_pr close-out and as
``python3 scripts/cleanup_worktrees.py``. Idempotent. Never ``rm -rf`` a path
that is not a git worktree of this repository. Dirty trees are left untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from common import run_cmd  # noqa: E402
import merge_pr  # noqa: E402

ISSUE_BRANCH_PREFIXES = ("feat/", "fix/", "chore/", "docs/")
REVIEW_BRANCH_PREFIX = "review-pr-"
RETAINED_DIR = os.path.join(".worktrees", ".retained")
RETAIN_MANIFEST = ".aru-retained-clean"
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


def gitdir_target(path: str) -> str | None:
    marker = os.path.join(path, ".git")
    try:
        with open(marker, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    if not text.startswith("gitdir: "):
        return None
    target = text.split(": ", 1)[1]
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(marker), target)
    return os.path.realpath(target)


def _retain_rel(path: str, full: str) -> str:
    rel = os.path.relpath(full, path).replace(os.sep, "/")
    return "" if rel == "." else rel


def _record_retain_entry(full: str, rel: str) -> dict:
    info = os.lstat(full)
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return {"type": "symlink", "target": os.readlink(full)}
    if stat.S_ISDIR(mode):
        return {"type": "dir", "mode": stat.S_IMODE(mode)}
    if not stat.S_ISREG(mode):
        raise OSError(f"unsupported retain path type: {rel}")
    digest = hashlib.sha256()
    fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = None
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    finally:
        if fd is not None:
            os.close(fd)
    return {"type": "file", "sha256": digest.hexdigest(), "mode": stat.S_IMODE(mode)}


def retain_manifest_payload(path: str) -> dict:
    entries = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        rel_dir = _retain_rel(path, dirpath)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            entry = _record_retain_entry(full, rel)
            if entry["type"] == "symlink":
                dirnames.remove(name)
            entries[rel] = entry
        for name in sorted(filenames):
            if name in {RETAIN_MANIFEST, RETAIN_MANIFEST + ".tmp"}:
                continue
            full = os.path.join(dirpath, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            entries[rel] = _record_retain_entry(full, rel)
    return {"entries": entries}


def _is_plain_dir(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def _physically_contained(path: str, parent: str) -> bool:
    """True when ``path`` is a real directory inside a non-symlinked parent."""
    if not _is_plain_dir(parent) or not _is_plain_dir(path):
        return False
    real_parent = os.path.realpath(parent)
    real_path = os.path.realpath(path)
    try:
        return os.path.commonpath([real_path, real_parent]) == real_parent
    except ValueError:
        return False


def _unlink_exact_inode(path: str, info: os.stat_result) -> None:
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
        os.unlink(path)


def write_retain_manifest(path: str) -> None:
    payload = retain_manifest_payload(path)
    dest = os.path.join(path, RETAIN_MANIFEST)
    tmp = dest + ".tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    info = os.fstat(fd)
    try:
        if not stat.S_ISREG(info.st_mode):
            raise OSError("retain manifest temp is not a regular file")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        current = os.lstat(tmp)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("retain manifest temp was replaced")
        os.replace(tmp, dest)
        tmp = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            _unlink_exact_inode(tmp, info)


def remove_retain_manifest(path: str) -> None:
    dest = os.path.join(path, RETAIN_MANIFEST)
    if os.path.lexists(dest):
        os.unlink(dest)


def _is_retain_manifest_path(path: str) -> bool:
    rel = path.replace("\\", "/").lstrip("/")
    if rel.endswith("/"):
        rel = rel[:-1]
    names = {RETAIN_MANIFEST, RETAIN_MANIFEST + ".tmp"}
    return rel in names or any(rel.endswith("/" + name) for name in names)


def porcelain_dirty_except_manifest(status: str | None, base_path: str | None = None) -> bool | None:
    if status is None:
        return None
    filtered = []
    for line in status.splitlines():
        path = line[3:] if len(line) > 2 else ""
        if _is_retain_manifest_path(path):
            continue
        filtered.append(line)
    return porcelain_blocks_prune("\n".join(filtered), base_path)


def retain_manifest_allows_prune(path: str) -> bool | None:
    dest = os.path.join(path, RETAIN_MANIFEST)
    try:
        with open(dest, encoding="utf-8") as handle:
            recorded = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(recorded, dict) or not isinstance(recorded.get("entries"), dict):
        return None
    try:
        current = retain_manifest_payload(path)["entries"]
    except OSError:
        return None
    return current == recorded["entries"]


def owned_worktree(path: str, repo_root: str) -> bool:
    admin_dir = gitdir_target(path)
    if not admin_dir:
        return False
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


def _is_empty_or_cache_dir(path: str) -> bool:
    """True if directory is physically empty or contains only empty dirs / cache files."""
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            return False
        for root, dirs, files in os.walk(path):
            for d in dirs:
                if os.path.islink(os.path.join(root, d)):
                    return False
            for f in files:
                full_file = os.path.join(root, f)
                if os.path.islink(full_file):
                    return False
                rel = os.path.relpath(full_file, path)
                if not _known_cache_path(rel):
                    return False
        return True
    except OSError:
        return False


def _known_cache_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").strip("/")
    if normalized.endswith(".pyc"):
        return True
    return any(part in KNOWN_CACHE_NAMES for part in normalized.split("/"))


def porcelain_blocks_prune(status: str | None, base_path: str | None = None) -> bool | None:
    """None if unreadable, True if real dirt, False if clean, cache-only, or empty ignored dirs."""
    if status is None:
        return None
    for line in status.splitlines():
        if not line:
            continue
        xy = line[:2]
        path = line[3:] if len(line) > 2 else ""
        if xy == "!!":
            if _known_cache_path(path):
                continue
            candidate = os.path.join(base_path, path.strip()) if base_path else path.strip()
            if _is_empty_or_cache_dir(candidate):
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


def stale_merged_branch(repo_root: str, branch: str) -> bool | None:
    """True when pruneable, False when in-flight, None when lookup failed.

    A new unpushed worktree is an ancestor of the default tip, so ancestor
    checks alone would delete in-flight trees. A failed ``ls-remote`` is not
    absence and is not treated as in-flight. Fast-forward leftovers still
    match the default SHA, so SHA inequality is not used as a gate.
    """
    if not branch or not branch_has_upstream(repo_root, branch):
        return False
    absent = remote_branch_absent(repo_root, branch)
    if absent is None:
        return None
    if absent is not True:
        return False
    return merged_into_default(repo_root, branch)


def prune_orphan_worktrees(repo_root: str) -> tuple[bool, list[str]]:
    notes = []
    failed = False
    code, porcelain, err = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        return False, [f"could not list worktrees: {err.strip()}"]
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
        blocked = porcelain_blocks_prune(dirty_status(path), path)
        if blocked is None:
            notes.append(f"skipped {path}: status unreadable")
            failed = True
            continue
        if blocked:
            notes.append(f"skipped dirty {path}")
            continue
        branch = branch_name(fields)
        stale = stale_merged_branch(repo_root, branch)
        if stale is None:
            notes.append(f"could not inspect remote for {path}")
            failed = True
            continue
        if not stale:
            notes.append(f"kept in-flight {path} ({branch})")
            continue
        rm_code, _, rm_err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if rm_code == 0:
            notes.append(f"removed {path}")
            if os.path.exists(path) and _is_empty_or_cache_dir(path):
                import shutil
                shutil.rmtree(path, ignore_errors=True)
        else:
            notes.append(f"could not remove {path}: {rm_err.strip()}")
            failed = True
    return (not failed), notes


def inspect_retained_root(repo_root: str) -> tuple[str | None, str | None]:
    """Return ``(root, error)``. Absent is ``(None, None)``; unsafe is an error."""
    retained = os.path.join(repo_root, RETAINED_DIR)
    try:
        info = os.lstat(retained)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not inspect retained root {retained}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return None, f"skipped retained root {retained}: symlink"
    if not stat.S_ISDIR(info.st_mode):
        return None, None
    worktrees = os.path.join(repo_root, ".worktrees")
    if not _physically_contained(retained, worktrees):
        return None, f"skipped retained root {retained}: not physically contained"
    return retained, None


def _retained_child_deletable(path: str, retained: str, repo_root: str) -> bool:
    worktrees = os.path.join(repo_root, ".worktrees")
    return (
        _physically_contained(path, retained)
        and _physically_contained(retained, worktrees)
        and under_worktrees(path, repo_root)
    )


def claim_retained_directory(path: str, retained: str) -> str | None:
    """Rename ``path`` to a unique sibling so a second janitor cannot claim it.

    Rename does not revoke an already-open cwd or directory handle on the
    same inode. Exclusive deletion is ``_remove_claimed_retained``, which
    verifies each entry against the snapshot and leaves surprises in place.
    """
    if not _physically_contained(path, retained):
        return None
    claimed = os.path.join(retained, f".deleting-{uuid.uuid4().hex}")
    try:
        os.rename(path, claimed)
    except OSError:
        return None
    if not _physically_contained(claimed, retained):
        try:
            os.rename(claimed, path)
        except OSError:
            pass
        return None
    return claimed


def _retained_still_clean(path: str, deregistered: bool) -> tuple[bool | None, str]:
    if deregistered:
        allowed = retain_manifest_allows_prune(path)
        if allowed is True:
            return True, ""
        reason = (
            "cleanliness unverifiable" if allowed is None else "dirty after retention"
        )
        return False, f"skipped retained {path}: {reason}"
    blocked = porcelain_blocks_prune(dirty_status(path), path)
    if blocked is None:
        return None, f"skipped retained {path}: status unreadable"
    if blocked:
        return False, f"skipped retained {path}: dirty"
    return True, ""


def _verified_unlink(full: str, rel: str, expected: dict) -> bool:
    """Remove one entry only if it still matches what the snapshot recorded.

    The check and the unlink are adjacent here, so the only writable window is
    per-entry rather than the whole tree walk. A file created or modified after
    the snapshot has no matching record and is left in place.
    """
    recorded = expected.get(rel)
    if recorded is None:
        return False
    try:
        actual = _record_retain_entry(full, rel)
    except OSError:
        return False
    if actual != recorded:
        return False
    try:
        if actual["type"] == "dir":
            os.rmdir(full)
        else:
            os.unlink(full)
    except OSError:
        return False
    return True


def _remove_claimed_retained(path: str, expected: dict) -> tuple[bool, str]:  # noqa: C901, PLR0912
    """Delete a validated retained tree, verifying each entry as it is removed.

    ``shutil.rmtree`` separates validation from deletion by an entire tree walk,
    so a write landing in that window is destroyed silently — the race reported
    on this PR. Verifying immediately before each unlink makes such a write fail
    closed instead: the unexpected entry matches no record, it is left alone,
    every directory above it survives with it, and the tree is reported as kept.
    Re-checking the whole tree and then calling ``rmtree`` cannot achieve this,
    however many times it is repeated.

    ``expected`` is required rather than optional: an unverified deletion path
    left available is one call away from reinstating the race.
    """
    # Git metadata is never agent output and is deliberately absent from the
    # snapshot (retain_manifest_payload does not descend into .git), so it is
    # removed wholesale rather than entry-verified.
    git_dir = os.path.join(path, ".git")
    try:
        if os.path.isdir(git_dir) and not os.path.islink(git_dir):
            shutil.rmtree(git_dir)
    except OSError as exc:
        return False, f"could not remove retained {path}: {exc}"

    survivors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        rel_dir = _retain_rel(path, dirpath)
        if rel_dir == ".git" or rel_dir.startswith(".git/"):
            continue
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(dirpath, name)
            if name in {RETAIN_MANIFEST, RETAIN_MANIFEST + ".tmp"}:
                try:
                    os.unlink(full)
                except OSError:
                    survivors.append(rel)
                continue
            if not _verified_unlink(full, rel, expected):
                survivors.append(rel)
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel == ".git":
                continue
            if not _verified_unlink(os.path.join(dirpath, name), rel, expected):
                survivors.append(rel)
    if survivors:
        return False, (
            f"kept retained {path}: {len(survivors)} entr"
            f"{'y' if len(survivors) == 1 else 'ies'} appeared or changed after "
            f"validation ({', '.join(survivors[:3])}); not deleted"
        )
    try:
        os.rmdir(path)
    except OSError as exc:
        return False, f"could not remove retained {path}: {exc}"
    return True, f"removed retained {path}"


def _prune_claimed_retained(
    path: str, retained: str, repo_root: str, deregistered: bool,
) -> tuple[bool, str]:
    claimed = claim_retained_directory(path, retained)
    if claimed is None:
        return False, f"skipped retained {path}: could not claim exclusive ownership"
    if not _retained_child_deletable(claimed, retained, repo_root):
        return False, f"skipped retained {claimed}: not physically contained"
    clean, note = _retained_still_clean(claimed, deregistered)
    if clean is not True:
        return False, note
    claimed2 = claim_retained_directory(claimed, retained)
    if claimed2 is None:
        return False, f"kept retained {claimed}: could not re-claim after validation"
    if not _retained_child_deletable(claimed2, retained, repo_root):
        return False, f"skipped retained {claimed2}: not physically contained"
    clean, note = _retained_still_clean(claimed2, deregistered)
    if clean is not True:
        return False, note
    # Snapshot at the last possible moment, then verify each entry against it as
    # it is unlinked. Renaming again would only move the validation-to-delete
    # window; entry-level verification is what closes it, because a write that
    # lands after this point cannot match a record that predates it.
    try:
        expected = retain_manifest_payload(claimed2)["entries"]
    except OSError as exc:
        return False, f"kept retained {claimed2}: could not snapshot for verified delete: {exc}"
    return _remove_claimed_retained(claimed2, expected)


def prune_retained_copies(repo_root: str) -> tuple[bool, list[str]]:
    notes = []
    failed = False
    retained, err = inspect_retained_root(repo_root)
    if err:
        return False, [err]
    if retained is None:
        return True, notes
    for name in sorted(os.listdir(retained)):
        path = os.path.join(retained, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            notes.append(f"skipped retained {path}: symlink")
            failed = True
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if not owned_worktree(path, repo_root):
            notes.append(f"skipped retained {path}: not a worktree of this repo")
            continue
        admin_dir = gitdir_target(path)
        deregistered = bool(admin_dir) and not os.path.isdir(admin_dir)
        clean, note = _retained_still_clean(path, deregistered)
        if clean is not True:
            notes.append(note)
            if (not deregistered) and note.endswith(": dirty"):
                continue
            failed = True
            continue
        if not _retained_child_deletable(path, retained, repo_root):
            notes.append(f"skipped retained {path}: not physically contained")
            failed = True
            continue
        ok, note = _prune_claimed_retained(path, retained, repo_root, deregistered)
        notes.append(note)
        if not ok:
            failed = True
    return (not failed), notes


def local_ref_exists(repo_root: str, branch: str) -> bool:
    if not branch:
        return False
    code, _, _ = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False, cwd=repo_root,
    )
    return code == 0


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


def delete_merged_local_branches(repo_root: str) -> tuple[bool, list[str]]:
    notes = []
    failed = False
    attached = attached_branches(repo_root)
    code, listing, err = run_cmd(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return False, [f"could not list local branches: {err.strip()}"]
    for name in listing.splitlines():
        name = name.strip()
        if not is_janitor_branch(name) or name in attached:
            continue
        stale = stale_merged_branch(repo_root, name)
        if stale is None:
            notes.append(f"could not inspect remote for {name}")
            failed = True
            continue
        if not stale:
            continue
        rm_code, _, rm_err = run_cmd(
            ["git", "branch", "-D", name], check=False, cwd=repo_root
        )
        if rm_code == 0:
            notes.append(f"deleted local branch {name}")
        else:
            notes.append(f"could not delete {name}: {rm_err.strip()}")
            failed = True
    return (not failed), notes


def _paginated_items(path: str, repo_root: str):
    return merge_pr._gh_json(
        ["gh", "api", "--paginate", path], cwd=repo_root
    )


def _repo_slug(repo_root: str) -> str | None:
    data = merge_pr._gh_json(
        ["gh", "repo", "view", "--json", "nameWithOwner"], cwd=repo_root
    )
    if not isinstance(data, dict):
        return None
    slug = data.get("nameWithOwner")
    if isinstance(slug, str) and "/" in slug:
        return slug
    return None


def _items_with_prefix(
    kind: str, state: str, prefix: str, repo_root: str,
) -> tuple[list[int], str | None]:
    slug = _repo_slug(repo_root)
    if not slug:
        return [], "unreadable"
    rest_state = "closed" if state == "merged" else state
    collection = "issues" if kind == "issue" else "pulls"
    path = f"repos/{slug}/{collection}?state={rest_state}&per_page=100"
    data = _paginated_items(path, repo_root)
    if not isinstance(data, list):
        return [], "unreadable"
    found = []
    for item in data:
        if not isinstance(item, dict):
            return [], "unreadable"
        if kind == "issue" and item.get("pull_request"):
            continue
        if kind == "pr" and state == "merged" and not item.get("merged_at"):
            continue
        labels = item.get("labels") or []
        names = []
        for label in labels:
            if isinstance(label, dict):
                names.append(label.get("name") or "")
            elif isinstance(label, str):
                names.append(label)
        if any(name.startswith(prefix) for name in names):
            found.append(int(item["number"]))
    return found, None


def linked_issues_unfinished(pr: dict, repo_root: str) -> bool:
    """True unless every Closes #N issue is closed with status:done."""
    nums = merge_pr.linked_issues(pr.get("body") or "")
    if not nums:
        return False
    for num in nums:
        issue = merge_pr._gh_json(
            ["gh", "issue", "view", str(num), "--json", "state,labels"],
            cwd=repo_root,
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
    ], cwd=repo_root)
    if not isinstance(pr, dict) or not merge_pr.is_merged(pr):
        return True
    if linked_issues_unfinished(pr, repo_root):
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
    if local_ref_exists(repo_root, branch):
        return True
    absent = remote_branch_absent(repo_root, branch)
    return absent is not True


def _record_claim_scan(notes: list[str], scan: str | None, kind: str, prefix: str) -> bool:
    if scan is None:
        return True
    if scan == "truncated":
        notes.append(
            f"{kind} claim scan hit the page limit; older {prefix} labels may remain"
        )
        return False
    notes.append(
        f"{kind} claim scan failed; older {prefix} labels may remain"
    )
    return False


def _append_claim_result(
    notes: list[str], ok: bool, success_note: str, failure_note: str
) -> bool:
    notes.append(success_note if ok else failure_note)
    return ok


def clear_stale_claim_labels(
    repo_root: str, retain_merger_pr: int | None = None
) -> tuple[bool, list[str]]:
    notes = []
    failed = False
    issues, issue_scan = _items_with_prefix("issue", "closed", "agent:", repo_root)
    if not _record_claim_scan(notes, issue_scan, "closed-issue", "agent:"):
        failed = True
    for number in issues:
        ok, message = merge_pr.clear_issue_claims(number, cwd=repo_root)
        if not _append_claim_result(
            notes, ok, message, f"issue #{number}: {message}"
        ):
            failed = True
    reviewers, review_scan = _items_with_prefix("pr", "merged", "reviewer:", repo_root)
    if not _record_claim_scan(notes, review_scan, "merged-PR reviewer", "reviewer:"):
        failed = True
    for number in reviewers:
        ok, message = merge_pr.clear_review_claims(number, cwd=repo_root)
        if not _append_claim_result(
            notes, ok, message, f"PR #{number} reviewer: {message}"
        ):
            failed = True
    mergers, merge_scan = _items_with_prefix("pr", "merged", "merger:", repo_root)
    if not _record_claim_scan(notes, merge_scan, "merged-PR merger", "merger:"):
        failed = True
    for number in mergers:
        if retain_merger_pr is not None and int(number) == int(retain_merger_pr):
            notes.append(
                f"kept merger claim on PR #{number}: current close-out incomplete"
            )
            continue
        if merger_claim_still_needed(repo_root, number):
            notes.append(f"kept merger claim on PR #{number}: close-out incomplete")
            continue
        ok, message = merge_pr.clear_merger_claims(number, cwd=repo_root)
        if not _append_claim_result(
            notes, ok, message, f"PR #{number} merger: {message}"
        ):
            failed = True
    return (not failed), notes


def sweep(repo_root: str, include_labels: bool = True,
          retain_merger_pr: int | None = None) -> tuple[bool, str]:
    if not refresh_origin(repo_root):
        notes = ["fetch failed; keeping worktrees and local branches"]
        if include_labels:
            _, label_notes = clear_stale_claim_labels(
                repo_root, retain_merger_pr=retain_merger_pr
            )
            notes.extend(label_notes)
        return False, "; ".join(notes)
    orphan_ok, orphan_notes = prune_orphan_worktrees(repo_root)
    retained_ok, retained_notes = prune_retained_copies(repo_root)
    branch_ok, branch_notes = delete_merged_local_branches(repo_root)
    notes = orphan_notes + retained_notes + branch_notes
    ok = orphan_ok and retained_ok and branch_ok
    if include_labels:
        label_ok, label_notes = clear_stale_claim_labels(
            repo_root, retain_merger_pr=retain_merger_pr
        )
        notes.extend(label_notes)
        ok = ok and label_ok
    if not notes:
        return True, "already clean"
    return ok, "; ".join(notes)


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
