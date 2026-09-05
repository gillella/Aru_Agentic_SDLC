#!/usr/bin/env python3
"""Pre-push write-budget enforcement for declared issue paths."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType


class Refusal(RuntimeError):
    pass


_TOUCHES: ModuleType | None = None


def _canonical_touches() -> ModuleType:
    global _TOUCHES
    if _TOUCHES is not None:
        return _TOUCHES
    candidates: list[Path] = [Path(__file__).resolve().with_name("touches.py")]
    configured = os.environ.get("ARU_SDLC_HOME")
    if configured:
        candidates.append(Path(configured).expanduser() / "scripts" / "touches.py")
    root = Path(__file__).resolve().parents[1]
    candidates.extend((root / "scripts" / "touches.py", root / "lib" / "touches.py"))
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("aru_canonical_touches", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if all(
            hasattr(module, name)
            for name in (
                "TouchesError",
                "parse_touches",
                "path_allowed",
                "safe_declared_path",
            )
        ):
            _TOUCHES = module
            return module
    raise Refusal("canonical Aru touches parser is unavailable; set ARU_SDLC_HOME")


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode:
        raise Refusal((result.stderr or result.stdout or "command failed").strip())
    return result.stdout.strip()


def safe_path(value: str) -> bool:
    touches = _canonical_touches()
    return bool(touches.safe_declared_path(value))


def parse_touches(body: str) -> list[str]:
    touches = _canonical_touches()
    try:
        return list(touches.parse_touches(body))
    except touches.TouchesError as exc:
        raise Refusal(str(exc)) from exc


def allowed(path: str, declared: list[str]) -> bool:
    return bool(_canonical_touches().path_allowed(path, declared))


def linked_issue(body: str) -> int:
    matches = re.findall(
        r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\s*$",
        body or "",
    )
    numbers = sorted({int(value) for value in matches})
    if len(matches) != 1 or len(numbers) != 1:
        raise Refusal("pull request must contain exactly one closing issue directive")
    return numbers[0]


def repository_slug() -> str:
    raw = run(["gh", "repo", "view", "--json", "nameWithOwner"])
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed repository data") from exc
    slug = record.get("nameWithOwner") if isinstance(record, dict) else None
    if not isinstance(slug, str) or slug.count("/") != 1:
        raise Refusal("repository identity is unavailable")
    return slug


def pull_request(number: int) -> dict:
    raw = run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "number,state,isDraft,headRefOid,body",
        ]
    )
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed pull request data") from exc
    if (
        not isinstance(record, dict)
        or record.get("number") != number
        or record.get("state") != "OPEN"
        or record.get("isDraft") is not False
        or not isinstance(record.get("headRefOid"), str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", record["headRefOid"])
        or not isinstance(record.get("body"), str)
    ):
        raise Refusal("pull request is unavailable or not ready")
    return record


def pull_changed_paths(number: int) -> list[str]:
    endpoint = f"repos/{repository_slug()}/pulls/{number}/files?per_page=100"
    raw = run(["gh", "api", "--paginate", "--slurp", endpoint])
    try:
        pages = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refusal("GitHub returned malformed pull request files") from exc
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise Refusal("GitHub returned malformed pull request files")
    paths: list[str] = []
    for page in pages:
        for record in page:
            if not isinstance(record, dict) or not isinstance(record.get("filename"), str):
                raise Refusal("GitHub returned malformed pull request file data")
            paths.append(record["filename"])
            previous = record.get("previous_filename")
            if previous is not None:
                if not isinstance(previous, str):
                    raise Refusal("GitHub returned malformed pull request file data")
                paths.append(previous)
    paths = sorted(set(paths))
    if not paths:
        raise Refusal("pull request has no changed files")
    return paths


def check_pull_request(number: int, expected_head: str | None = None) -> tuple[list[str], int, str]:
    pr = pull_request(number)
    head = str(pr["headRefOid"])
    if expected_head is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head):
            raise Refusal("expected head is malformed")
        if expected_head.lower() != head.lower():
            raise Refusal("expected head does not match the current PR head")
    issue = linked_issue(str(pr["body"]))
    paths = pull_changed_paths(number)
    declared = parse_touches(issue_body(issue))
    violations = [path for path in paths if not allowed(path, declared)]
    refreshed = pull_request(number)
    if str(refreshed["headRefOid"]).lower() != head.lower():
        raise Refusal("pull request head changed during file collection")
    if linked_issue(str(refreshed["body"])) != issue:
        raise Refusal("pull request closing issue changed during file collection")
    if set(parse_touches(issue_body(issue))) != set(declared):
        raise Refusal("issue touches authorization changed during file collection")
    return violations, issue, head


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
    labels = [label.get("name") for label in record.get("labels", []) if isinstance(label, dict)]
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
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies-harder",
            "--diff-filter=ACDMRT",
            diff_range,
            "--",
        ]
    )
    if not output:
        return []
    if not output.endswith("\0"):
        raise Refusal("changed-path evidence is malformed")

    tokens = output[:-1].split("\0")
    paths: list[str] = []
    idx = 0
    n = len(tokens)
    while idx < n:
        status = tokens[idx]
        code = status[:1]
        if code in {"R", "C"}:
            if len(status) == 1 or not status[1:].isdigit():
                raise Refusal("changed-path evidence is malformed")
            arity = 2
        elif code in {"A", "D", "M", "T"}:
            if len(status) != 1:
                raise Refusal("changed-path evidence is malformed")
            arity = 1
        else:
            raise Refusal("changed-path evidence is malformed")

        if idx + 1 + arity > n:
            raise Refusal("changed-path evidence is malformed")

        for offset in range(1, 1 + arity):
            path = tokens[idx + offset]
            if not path:
                raise Refusal("changed-path evidence is malformed")
            paths.append(path)

        idx += 1 + arity

    return sorted(set(paths))


def check(
    paths: list[str],
    number: int | None = None,
    branch: str | None = None,
    default_branch: str | None = None,
) -> list[str]:
    branch = branch or run(["git", "-c", "core.fsmonitor=false", "branch", "--show-current"])
    if default_branch is None:
        raw = run(["gh", "repo", "view", "--json", "defaultBranchRef"])
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise Refusal("GitHub returned malformed default-branch data") from exc
        ref = record.get("defaultBranchRef") if isinstance(record, dict) else None
        default_branch = ref.get("name") if isinstance(ref, dict) else None
    if not isinstance(default_branch, str) or not re.fullmatch(
        r"(?!-)(?!.*(?:\.\.|//))[A-Za-z0-9._/-]+", default_branch
    ):
        raise Refusal("repository default branch is unavailable or unsafe")
    if branch == default_branch:
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
    parser.add_argument("--default-branch")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--expected-head")
    args = parser.parse_args()
    if args.pr is not None:
        if args.diff_range or args.path or args.issue or args.branch or args.default_branch:
            parser.error("--pr cannot be combined with local path arguments")
    elif not args.diff_range and not args.path:
        parser.error("provide --pr, --range, or --path")
    elif args.expected_head:
        parser.error("--expected-head requires --pr")
    try:
        if args.pr is not None:
            violations, _issue, _head = check_pull_request(args.pr, args.expected_head)
        else:
            paths = list(args.path)
            if args.diff_range:
                paths.extend(changed_paths(args.diff_range))
            violations = check(sorted(set(paths)), args.issue, args.branch, args.default_branch)
    except Refusal as exc:
        parser.error(str(exc))
    if violations:
        for path in violations:
            print(f"refused: {path} is outside touches:", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
