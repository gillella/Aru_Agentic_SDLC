"""Dependency-free parser and matcher for the Aru ``touches:`` contract."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable


class TouchesError(ValueError):
    """The declared write boundary is missing, ambiguous, or unsafe."""


def safe_declared_path(value: str) -> bool:
    if "\\" in value or value.startswith(("/", "~", "-")):
        return False
    if unmatchable_reason(value) is not None:
        return False
    raw = value[:-3] if value.endswith("/**") else value
    path = PurePosixPath(raw)
    return bool(raw and raw != "." and ".." not in path.parts)


def unmatchable_reason(value: str) -> str | None:
    """Why this rule can never match a file, or None when it can.

    `path_allowed` matches a rule either exactly or, for a rule ending `/**`,
    as a prefix. A rule ending in a bare separator therefore matches nothing at
    all: no file is named `docs/`, because that is a directory.

    Refusing it here rather than leaving it to the server check is the whole
    point. `touches: docs/` used to pass validation, the issue reached Ready, an
    agent wrote the change, and only then did the governed check refuse every
    file in it. The cost was never the typo -- it was that it surfaced after the
    work rather than at the gate whose job is validating the contract.
    """
    if value.endswith("/**"):
        return None
    if value.endswith("/"):
        return (
            f"{value!r} matches no file: a trailing '/' names a directory, and rules "
            f"match a path exactly or by '/**' prefix. Declare {value + '**'!r} to "
            f"cover everything beneath it, or name the file itself."
        )
    return None


def parse_touches(body: str) -> list[str]:
    inline = re.findall(r"(?im)^\s*touches:\s*(.+?)\s*$", body or "")
    sections = re.findall(
        r"(?ims)^###\s+touches:\s*$\n(.*?)(?=^#{1,3}\s+|\Z)", body or ""
    )
    declarations = [*inline, *(section.strip() for section in sections)]
    if len(declarations) != 1:
        raise TouchesError("issue must contain exactly one touches: declaration")
    declaration = declarations[0]
    if len(declaration.splitlines()) != 1:
        raise TouchesError("touches: declaration must be a single line")
    paths = [part.strip() for part in declaration.split(",") if part.strip()]
    if not paths:
        raise TouchesError("touches: must declare at least one path")
    # Named before the generic refusal: an operator who wrote `docs/` needs to
    # be told to write `docs/**`, not that their path is "unsafe".
    for path in paths:
        reason = unmatchable_reason(path)
        if reason is not None:
            raise TouchesError(f"touches: {reason}")
    if any(not safe_declared_path(path) for path in paths):
        raise TouchesError("touches: contains an unsafe path")
    return paths


def path_allowed(path: str, declared: Iterable[str]) -> bool:
    candidate = PurePosixPath(path).as_posix()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if not safe_declared_path(candidate):
        return False
    for rule in declared:
        prefix = rule[:-3].rstrip("/") if rule.endswith("/**") else None
        if candidate == rule or (
            prefix and (candidate == prefix or candidate.startswith(prefix + "/"))
        ):
            return True
    return False
