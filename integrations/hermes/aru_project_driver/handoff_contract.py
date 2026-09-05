"""Typed GitHub-owned dependency contracts; no instructions or executable fields."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from .config import DriverError

MARKER = "aru-driver-dependency:v1"
PATTERN = re.compile(r"^[ \t]*<!-- aru-driver-dependency:v1 (\{[^\r\n]+\}) -->[ \t\r]*$",
                     re.MULTILINE)


def number(value: object) -> bool:
    return type(value) is int and value > 0


def path(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and not any(part in {".", ".."} for part in value.split("/"))
            and "\\" not in value and "\x00" not in value
            and str(PurePosixPath(value)) == value)


def ref(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 128
            and not value.startswith(("/", ".")) and ".." not in value
            and "//" not in value and "\\" not in value and "\x00" not in value
            and not any(char.isspace() for char in value)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is not None)


def parse(body: str, origin: str) -> dict | None:
    if MARKER not in body:
        return None
    matches = PATTERN.findall(body)
    if len(matches) != 1 or body.count(MARKER) != 1:
        raise DriverError("source issue must contain exactly one complete Driver dependency marker")
    try:
        data = json.loads(matches[0])
    except ValueError as exc:
        raise DriverError("source dependency contract is not valid JSON") from exc
    validate(data, origin)
    return data


def validate(data: dict, origin: str) -> None:
    required = {"origin", "target", "issue", "source_pr", "source_head", "conditions"}
    if not isinstance(data, dict) or set(data) != required:
        raise DriverError("dependency contract contains unsupported fields")
    if data.get("origin") != origin:
        raise DriverError("dependency contract origin does not match the source project")
    target = data.get("target")
    if (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target)
            or target == origin or not number(data.get("issue"))):
        raise DriverError("dependency requires another literal project and a positive target issue")
    if not number(data["source_pr"]):
        raise DriverError("source_pr must be a positive PR number")
    if not isinstance(data["source_head"], str) or not re.fullmatch(r"[a-f0-9]{40}", data["source_head"]):
        raise DriverError("source_head must be the full lowercase PR head")
    conditions = data.get("conditions")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 12:
        raise DriverError("dependency requires one to twelve explicit all-of conditions")
    for condition in conditions:
        _condition(condition, origin, target, data["issue"])
    if not any(c["kind"] == "issue_done" and c["issue"] == data["issue"] for c in conditions):
        raise DriverError("dependency must include the target issue's GitHub Done condition")


def _condition(item: dict, origin: str, target: str, issue: int) -> None:
    fields = {
        "issue_done": {"kind", "repo", "issue"},
        "pr_merged": {"kind", "repo", "pr", "head"},
        "release_contains_pr": {"kind", "repo", "tag", "pr", "head"},
        "artifact_matches_release": {"kind", "repo", "ref", "path", "release_repo", "tag", "release_path"},
    }
    if not isinstance(item, dict) or item.get("kind") not in fields or set(item) != fields[item["kind"]]:
        raise DriverError("unsupported or incomplete dependency condition")
    kind = item["kind"]
    if item["repo"] != (origin if kind == "artifact_matches_release" else target):
        raise DriverError("dependency condition escapes its explicit origin/target route")
    if kind == "issue_done" and (not number(item["issue"]) or item["issue"] != issue):
        raise DriverError("Done condition must name the exact receiving issue")
    if kind in {"pr_merged", "release_contains_pr"}:
        if not number(item["pr"]) or not isinstance(item["head"], str) or not re.fullmatch(r"[a-f0-9]{40}", item["head"]):
            raise DriverError("merged PR condition requires a positive PR and full expected head")
    if "tag" in item and not ref(item["tag"]):
        raise DriverError("release condition requires a literal tag")
    if kind == "artifact_matches_release":
        if (item["release_repo"] != target or not path(item["path"]) or not path(item["release_path"])
                or not ref(item["ref"])):
            raise DriverError("artifact condition requires bounded paths and explicit consumer ref")


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
