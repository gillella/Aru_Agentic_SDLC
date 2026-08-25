#!/usr/bin/env python3
# line-ceiling: 220
"""secure_json.py - private, locked, atomic JSON storage.

The generic durable-state primitive: operator-private permissions (0700
directories, 0600 files), symlink and non-regular-file refusal, exclusive
advisory locking, strict JSON decoding, and atomic replacement.

Extracted from slack_projects.py so durable state outlives the Slack runtime
(#411). Nothing here imports Slack, and nothing here is a registry, queue,
supervisor, or coordination authority - it is storage and only storage.

`RegistryError` keeps its historical name and message wording deliberately.
`delivery_increments.IncrementError` subclasses it, so this must remain the
same class object rather than a same-named copy; two distinct classes would
silently stop `except RegistryError` from catching what it was written to
catch. The wording is corrected when the Slack runtime is deleted, not here.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

# Credential-shaped payloads are refused before they can reach disk. Kept with
# the writer rather than the caller: a guard the caller has to remember is a
# guard that eventually gets forgotten.
SECRET_RE = re.compile(r"(?:xox[baprs]-|xapp-|Bearer\s+)\S+", re.IGNORECASE)


class RegistryError(RuntimeError):
    """The store is unavailable, invalid, or cannot satisfy a request."""


def _private_directory(path: Path) -> None:  # noqa: C901, PLR0912
    if path.is_symlink():
        raise RegistryError(f"unsafe registry directory: {path}")
    if path.exists():
        if not path.is_dir():
            raise RegistryError(f"unsafe registry directory: {path}")
        info = path.stat()
        if info.st_uid != os.getuid():
            raise RegistryError(f"registry directory is not owned by the current user: {path}")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise RegistryError(f"registry directory is writable by another user: {path}")
        if mode != 0o700:
            descriptor = -1
            try:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
                # Re-check the opened descriptor against the stat above: the
                # path could have been swapped between the two calls.
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise RegistryError(f"registry directory changed while securing it: {path}")
                if stat.S_IMODE(opened.st_mode) & 0o022:
                    raise RegistryError(f"registry directory is writable by another user: {path}")
                os.fchmod(descriptor, 0o700)
            except OSError as exc:
                raise RegistryError(f"cannot secure registry directory {path}: {exc}") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return
    path.mkdir(parents=True, mode=0o700)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
            raise RegistryError(f"unsafe registry directory after creation: {path}")
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise RegistryError(f"cannot secure registry directory {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RegistryError(f"not a regular file: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RegistryError(f"file must be private (0600): {path}")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    _private_directory(path.parent)
    lock_path = path.with_name(f"{path.name}.lock")
    if lock_path.exists():
        _private_file(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RegistryError(f"cannot open lock file {lock_path}: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_unlocked(path: Path, default: Any = None) -> Any:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise RegistryError(f"file does not exist: {path}")
    _private_file(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Malformed JSON raises rather than falling back to the default: a
        # corrupt store must be visible, not silently replaced by an empty one.
        raise RegistryError(f"cannot read {path}: {exc}") from exc


def _write_unlocked(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise RegistryError(f"refusing symlink: {path}")
    if path.exists():
        _private_file(path)
    temp_name: Optional[str] = None
    try:
        encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if SECRET_RE.search(encoded):
            raise RegistryError("refusing to persist credential-shaped data")
        # Write to a sibling temp file, fsync it, then rename: a crash leaves
        # either the old file or the new one, never a truncated one.
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
        os.chmod(path, 0o600)
        # fsync the directory too, so the rename itself is durable.
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise RegistryError(f"cannot write {path}: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def read_secure_json(path: Path, default: Any = None) -> Any:
    with _file_lock(path):
        return _read_unlocked(path, default)


def mutate_secure_json(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    """Lock, validate, update, and atomically replace one private JSON file."""
    with _file_lock(path):
        current = _read_unlocked(path, default)
        # The updater sees a deep copy, so a partial mutation cannot corrupt
        # the in-memory value the caller still holds if it raises midway.
        updated = updater(copy.deepcopy(current))
        _write_unlocked(path, updated)
        return updated


secure_read_json = read_secure_json
secure_mutate_json = mutate_secure_json
