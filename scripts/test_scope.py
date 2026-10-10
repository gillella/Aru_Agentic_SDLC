"""Conservative Factory preflight selection; not a lifecycle or consumer gate."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# Always cover scope, reviewer independence and runner trust, even for ordinary stories.
INVARIANTS = (
    "tests/test_scope_contract.py",
    "tests/test_enforce_touches.py",
    "tests/test_review_authority.py",
    "tests/test_runner_policy.py",
    "tests/test_verify_workflow_permissions.py",
)
SHARED = {
    "common", "policy", "touches", "consumer", "manifest", "report", "merge_state",
    "merge_pr", "merge_authority", "review_authority", "init_project", "test_scope",
}


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", *args], cwd=root, stderr=subprocess.PIPE
    )


def safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in value.split("/")
    ) and not any(char in value for char in "\n\r\0\\") and not value.startswith("-")


def changed_paths(raw: bytes) -> set[str]:
    """Parse Git's NUL-delimited name/status records, including both rename ends."""
    fields = raw.decode("utf-8", errors="strict").split("\0")
    if fields.pop() != "":
        raise ValueError("diff is not NUL terminated")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        count = 2 if re.fullmatch(r"[RC]\d{1,3}", status) else 1
        if not re.fullmatch(r"[AMDT]|[RC]\d{1,3}", status):
            raise ValueError("malformed diff status")
        values = fields[index + 1:index + 1 + count]
        if len(values) != count or not all(safe_path(path) for path in values):
            raise ValueError("unsafe or truncated diff paths")
        paths.update(values)
        index += 1 + count
    return paths


def collect_changes(root: Path, base: str, expected_head: str | None = None) -> set[str]:
    if expected_head:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise ValueError("expected head must be a full commit SHA")
        if git(root, "rev-parse", "HEAD").decode().strip() != expected_head:
            raise ValueError("checkout differs from the expected PR head")
    # Resolve a commit before constructing revision syntax; no option-like caller input.
    base_sha = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise ValueError("base commit evidence malformed")
    paths = changed_paths(git(root, "diff", "--name-status", "-z", "--find-renames",
                              f"{base_sha}...HEAD", "--"))
    paths |= changed_paths(git(root, "diff", "HEAD", "--name-status", "-z",
                               "--find-renames", "--"))
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked:
        for value in untracked.decode("utf-8", errors="strict").split("\0")[:-1]:
            if not safe_path(value):
                raise ValueError("unsafe untracked path")
            paths.add(value)
    return paths


def full(paths: set[str], reason: str) -> dict:
    return {"mode": "full", "changed_paths": sorted(paths), "tests": ["tests"],
            "rationale": [reason]}


def ordinary(path: str) -> bool:
    value = PurePosixPath(path)
    if path == "tests/conftest.py":
        return False
    if value.parts[0] not in {"scripts", "tests"} or value.suffix != ".py":
        return False
    return value.stem not in SHARED and not any(
        token in path.lower() for token in ("auth", "merge", "secret", "security", "verif", "scope")
    )


def python_graph(root: Path) -> tuple[dict[str, set[str]], set[str]]:
    # Inventory source trees without importing or depending on any optional adapter.
    # Hidden runtime/environment/worktree directories are never repository source.
    sources = sorted(path for directory in root.iterdir()
                     if directory.is_dir() and not directory.name.startswith(".")
                     and not directory.is_symlink()
                     for path in directory.rglob("*.py")
                     if not any(part.startswith(".") for part in path.relative_to(root).parts))
    paths = {path.relative_to(root).as_posix(): path for path in sources}
    graph: dict[str, set[str]] = {}
    for relative, path in paths.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        imports = set()
        references = {node.value for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                if name in {"__import__", "import_module", "run_path", "spec_from_file_location"}:
                    if not node.args or not isinstance(node.args[0], ast.Constant):
                        raise ValueError("dynamic dependency target cannot be determined")
                    if name in {"__import__", "import_module"}:
                        imports.add(str(node.args[0].value))
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
                imports.update(alias.name for alias in node.names)
        # Include literal module/file references used by subprocess and dynamic imports.
        # Match actual imports and precise file references, never generic prose/stems.
        graph[relative] = {
            target for target in paths if target != relative and (
                PurePosixPath(target).stem in imports
                or any(name.endswith("." + PurePosixPath(target).stem) for name in imports)
                or PurePosixPath(target).name in references
                or any(target in reference.split() for reference in references)
            )
        }
    tests = {path for path in paths if path.startswith("tests/test_")}
    return graph, tests


def caller_closure(graph: dict[str, set[str]], paths: set[str]) -> set[str]:
    affected = set(paths)
    while True:
        callers = {path for path, imports in graph.items() if imports & affected}
        if callers <= affected:
            return affected
        affected |= callers


def select(root: Path, paths: set[str], *, release: bool = False) -> dict:
    if release:
        return full(paths, "release verification always runs full regression and migration/rollback coverage")
    if not paths or not all(safe_path(path) for path in paths):
        return full(paths, "empty or unsafe diff evidence")
    if any(not ordinary(path) for path in paths):
        return full(paths, "shared, sensitive, release/policy or unrecognized changed path")
    if any(not (root / path).is_file() or (root / path).is_symlink() for path in paths):
        return full(paths, "deleted/renamed-away path requires broad regression")
    try:
        graph, tests = python_graph(root)
    except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
        return full(paths, f"unreadable dependency evidence: {type(exc).__name__}")
    if not set(INVARIANTS) <= tests:
        return full(paths, "mandatory governance tests missing")
    affected = caller_closure(graph, paths)
    if any(not ordinary(path) for path in affected - tests):
        return full(paths, "affected caller is shared or sensitive")
    behavior_tests = tests & affected
    if not behavior_tests:
        return full(paths, "no proven affected behavior tests")
    # Every changed production module must reach a test; one unrelated edited test is insufficient.
    for path in paths - tests:
        reached = caller_closure(graph, {path})
        if not reached & tests:
            return full(paths, f"no test coverage identified for {path}")
    return {"mode": "focused", "changed_paths": sorted(paths),
            "tests": sorted(behavior_tests | set(INVARIANTS)),
            "rationale": ["actual changed behavior plus transitive callers and changed/new tests",
                          "mandatory scope, review independence and runner trust invariants"]}


def plan(root: Path, base: str, expected_head: str | None, release: bool) -> dict:
    try:
        paths = collect_changes(root, base, expected_head)
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError) as exc:
        # A mismatched exact head is not repaired by running broader tests.
        if expected_head:
            actual = git(root, "rev-parse", "HEAD").decode().strip()
            if actual != expected_head or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
                raise ValueError("exact-head checkout evidence failed") from exc
        return full(set(), f"unreadable diff evidence: {type(exc).__name__}")
    return select(root, paths, release=release)


def execute(document: dict, root: Path) -> int:
    tests = document.get("tests")
    if document.get("mode") not in {"full", "focused"} or not isinstance(tests, list) or not tests:
        raise ValueError("invalid or empty test plan")
    if not all(isinstance(path, str) and safe_path(path) and (path == "tests" or path.startswith("tests/"))
               and (root / path).exists() for path in tests):
        raise ValueError("unsafe or missing selected test")
    if document["mode"] == "full" and tests != ["tests"]:
        raise ValueError("full regression cannot be narrowed")
    if document["mode"] == "focused" and not set(INVARIANTS) <= set(tests):
        raise ValueError("focused plan lacks mandatory invariants")
    return subprocess.call([sys.executable, "-m", "pytest", "-q", *tests], cwd=root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("ARU_TEST_BASE", "origin/main"))
    parser.add_argument("--expected-head", default=os.environ.get("ARU_EXPECTED_HEAD"))
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--execute-plan", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.execute_plan:
            recorded = json.loads(args.execute_plan.read_text())
            current = plan(root, args.base, args.expected_head, args.release)
            if recorded != current:
                raise ValueError("test plan is stale or differs from actual base/head/diff evidence")
            return execute(current, root)
        document = plan(root, args.base, args.expected_head, args.release)
        print(json.dumps(document, indent=2, sort_keys=True), flush=True)
        if args.plan_file:
            args.plan_file.write_text(json.dumps(document), encoding="utf-8")
        return execute(document, root) if args.run else 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
