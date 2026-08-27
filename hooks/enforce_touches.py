#!/usr/bin/env python3
"""Pre-push write-budget enforcement for declared issue paths."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import PurePosixPath


class Refusal(RuntimeError):
    pass


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode:
        raise Refusal((result.stderr or result.stdout or "command failed").strip())
    return result.stdout.strip()


def safe_path(value: str) -> bool:
    if "\\" in value or value.startswith(("/", "~", "-")):
        return False
    raw = value[:-3] if value.endswith("/**") else value
    path = PurePosixPath(raw)
    return bool(raw and raw != "." and ".." not in path.parts)


def parse_touches(body: str) -> list[str]:
    matches = re.findall(r"(?im)^\s*touches:\s*(.+?)\s*$", body or "")
    if len(matches) != 1:
        raise Refusal("issue must contain exactly one touches: declaration")
    paths = [part.strip() for part in matches[0].split(",") if part.strip()]
    if not paths or any(not safe_path(path) for path in paths):
        raise Refusal("touches: contains an empty or unsafe path")
    return paths


def allowed(path: str, declared: list[str]) -> bool:
    candidate = PurePosixPath(path).as_posix()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if not safe_path(candidate):
        return False
    for rule in declared:
        if candidate == rule:
            return True
        if rule.endswith("/**"):
            prefix = rule[:-3].rstrip("/")
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
    return False


def issue_number(branch: str) -> int:
    match = re.search(r"(?:^|/)issue-(\d+)-", branch)
    if not match:
        raise Refusal("branch name does not identify an issue")
    return int(match.group(1))


def issue_body(number: int) -> str:
    raw = run(["gh", "issue", "view", str(number), "--json", "body,state,labels"])
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed issue data") from exc
    if record.get("state") != "OPEN":
        raise Refusal("issue is not open")
    labels = [
        label.get("name")
        for label in record.get("labels", [])
        if isinstance(label, dict)
    ]
    if not any(name in {"status:in-progress", "status:in-review"} for name in labels):
        raise Refusal("issue is not In Progress or In Review")
    if len([name for name in labels if isinstance(name, str) and name.startswith("agent:")]) != 1:
        raise Refusal("issue does not have one exclusive claimant")
    return str(record.get("body") or "")


def changed_paths(diff_range: str) -> list[str]:
    output = run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            diff_range,
            "--",
        ]
    )
    return [line for line in output.splitlines() if line]


def check(
    paths: list[str],
    number: int | None = None,
    branch: str | None = None,
) -> list[str]:
    branch = branch or run(["git", "-c", "core.fsmonitor=false", "branch", "--show-current"])
    if branch in {"main", "master"}:
        raise Refusal("implementation writes are not allowed on the default branch")
    number = number or issue_number(branch)
    declared = parse_touches(issue_body(number))
    return [path for path in paths if not allowed(path, declared)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--range", dest="diff_range")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--issue", type=int)
    parser.add_argument("--branch")
    args = parser.parse_args()
    if not args.diff_range and not args.path:
        parser.error("provide --range or --path")
    try:
        paths = list(args.path)
        if args.diff_range:
            paths.extend(changed_paths(args.diff_range))
        violations = check(sorted(set(paths)), args.issue, args.branch)
    except Refusal as exc:
        parser.error(str(exc))
    if violations:
        for path in violations:
            print(f"refused: {path} is outside touches:", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
