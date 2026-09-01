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
    raw = value[:-3] if value.endswith("/**") else value
    path = PurePosixPath(raw)
    return bool(raw and raw != "." and ".." not in path.parts)


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
