#!/usr/bin/env python3
"""Report dead-code and repository-cleanup candidates without deleting anything."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


EXIT_CLEAN = 0
EXIT_ERROR = 1
EXIT_FINDINGS = 3

VULTURE_FINDING = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): (?P<message>.+?) "
    r"\((?P<confidence>\d+)% confidence\)$"
)

CommandRunner = Callable[[Sequence[str], Path], Tuple[int, str, str]]


def run_command(command: Sequence[str], cwd: Path) -> Tuple[int, str, str]:
    """Run a read-only inspection command."""
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        return EXIT_ERROR, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def tracked_files(repo_dir: Path, runner: CommandRunner = run_command) -> List[Path]:
    """Return repository-relative tracked files, failing closed on Git errors."""
    code, output, error = runner(["git", "ls-files", "-z"], repo_dir)
    if code != 0:
        raise RuntimeError(error.strip() or "git ls-files failed")
    return [Path(item) for item in output.split("\0") if item]


def discover_python_targets(files: Sequence[Path], include_tests: bool = False) -> List[str]:
    """Select tracked Python files for Vulture, excluding tests by default."""
    selected = []
    for path in files:
        if path.suffix != ".py":
            continue
        if not include_tests and path.parts and path.parts[0] in {"tests", "test"}:
            continue
        selected.append(path.as_posix())
    return sorted(selected)


def parse_vulture_output(output: str) -> List[Dict[str, Any]]:
    """Parse Vulture's stable pyflakes-style findings, preserving unknown lines."""
    findings = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = VULTURE_FINDING.match(line)
        if match:
            findings.append(
                {
                    "path": match.group("path"),
                    "line": int(match.group("line")),
                    "message": match.group("message"),
                    "confidence": int(match.group("confidence")),
                    "raw": line,
                }
            )
        else:
            findings.append({"raw": line})
    return findings


def run_vulture(
    repo_dir: Path,
    targets: Sequence[str],
    min_confidence: int,
    runner: CommandRunner = run_command,
) -> Dict[str, Any]:
    """Run Vulture and classify its documented exit codes."""
    if not targets:
        return {"status": "clean", "findings": [], "targets": []}

    command = [
        sys.executable,
        "-m",
        "vulture",
        *targets,
        "--min-confidence",
        str(min_confidence),
    ]
    code, output, error = runner(command, repo_dir)
    result = {
        "status": "clean",
        "findings": parse_vulture_output(output),
        "targets": list(targets),
    }
    if code == EXIT_CLEAN:
        return result
    if code == EXIT_FINDINGS:
        result["status"] = "findings"
        return result

    result["status"] = "error"
    result["error"] = error.strip() or output.strip() or f"Vulture exited {code}"
    result["exit_code"] = code
    return result


def unreferenced_prompts(repo_dir: Path, files: Sequence[Path]) -> List[str]:
    """Find tracked prompt files whose repository-relative path is never referenced."""
    prompt_files = sorted(path for path in files if path.parts and path.parts[0] == "prompts")
    if not prompt_files:
        return []

    searchable: Dict[Path, str] = {}
    for path in files:
        full_path = repo_dir / path
        try:
            if full_path.stat().st_size > 2 * 1024 * 1024:
                continue
            searchable[path] = full_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    unused = []
    for prompt in prompt_files:
        needle = prompt.as_posix()
        if not any(needle in content for path, content in searchable.items() if path != prompt):
            unused.append(needle)
    return unused


def registered_worktrees(repo_dir: Path, runner: CommandRunner = run_command) -> List[Path]:
    """Return resolved paths registered in Git's worktree inventory."""
    code, output, error = runner(["git", "worktree", "list", "--porcelain"], repo_dir)
    if code != 0:
        raise RuntimeError(error.strip() or "git worktree list failed")
    paths = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.removeprefix("worktree ")).expanduser().resolve())
    return paths


def orphaned_worktree_dirs(
    repo_dir: Path,
    runner: CommandRunner = run_command,
) -> List[str]:
    """Find visible `.worktrees/` children absent from Git's registered inventory."""
    worktree_root = repo_dir / ".worktrees"
    if not worktree_root.is_dir():
        return []
    registered = set(registered_worktrees(repo_dir, runner))
    orphans = []
    for candidate in sorted(worktree_root.iterdir(), key=lambda item: item.name):
        if candidate.name.startswith(".") or not candidate.is_dir():
            continue
        if candidate.resolve() not in registered:
            orphans.append(os.path.relpath(candidate, repo_dir))
    return orphans


def scan_repository(
    repo_dir: Path,
    min_confidence: int = 60,
    include_tests: bool = False,
    python_targets: Optional[Sequence[str]] = None,
    runner: CommandRunner = run_command,
) -> Dict[str, Any]:
    """Collect all report-only cleanup evidence for a repository."""
    repo_dir = repo_dir.expanduser().resolve()
    report: Dict[str, Any] = {
        "repo_dir": str(repo_dir),
        "vulture": {"status": "skipped", "findings": [], "targets": []},
        "unreferenced_prompts": [],
        "orphaned_worktrees": [],
        "errors": [],
    }
    if not repo_dir.is_dir():
        report["errors"].append(f"Repository directory does not exist: {repo_dir}")
        report["status"] = "error"
        return report

    try:
        files = tracked_files(repo_dir, runner)
        targets = list(python_targets) if python_targets else discover_python_targets(files, include_tests)
        report["vulture"] = run_vulture(repo_dir, targets, min_confidence, runner)
        report["unreferenced_prompts"] = unreferenced_prompts(repo_dir, files)
        report["orphaned_worktrees"] = orphaned_worktree_dirs(repo_dir, runner)
    except RuntimeError as exc:
        report["errors"].append(str(exc))

    if report["vulture"].get("status") == "error" and report["vulture"].get("error"):
        report["errors"].append(report["vulture"].get("error", "Vulture failed"))

    has_findings = bool(
        report["vulture"].get("status") == "findings"
        or report["vulture"].get("findings")
        or report["unreferenced_prompts"]
        or report["orphaned_worktrees"]
    )
    report["status"] = "error" if report["errors"] else ("findings" if has_findings else "clean")
    return report


def print_human(report: Dict[str, Any]) -> None:
    """Print a concise, auditable report."""
    print(f"Codebase prune scan: {report['repo_dir']}")
    print("\nVulture candidates:")
    findings = report["vulture"].get("findings", [])
    if findings:
        for finding in findings:
            print(f"  - {finding['raw']}")
    else:
        print("  none")

    print("\nUnreferenced prompts:")
    for path in report["unreferenced_prompts"]:
        print(f"  - {path}")
    if not report["unreferenced_prompts"]:
        print("  none")

    print("\nOrphaned worktree directories:")
    for path in report["orphaned_worktrees"]:
        print(f"  - {path}")
    if not report["orphaned_worktrees"]:
        print("  none")

    for error in report["errors"]:
        print(f"\nERROR: {error}", file=sys.stderr)
    print("\nNo files were deleted. Review every candidate before opening cleanup work.")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=".", help="Repository to inspect (default: current directory)")
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=60,
        choices=range(0, 101),
        metavar="0..100",
        help="Minimum Vulture confidence to report (default: 60)",
    )
    parser.add_argument("--include-tests", action="store_true", help="Include tracked tests in Vulture analysis")
    parser.add_argument(
        "--python-target",
        action="append",
        dest="python_targets",
        help="Explicit Vulture target relative to the repository; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = scan_repository(
        Path(args.repo_dir),
        min_confidence=args.min_confidence,
        include_tests=args.include_tests,
        python_targets=args.python_targets,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    if report["status"] == "error":
        return EXIT_ERROR
    if report["status"] == "findings":
        return EXIT_FINDINGS
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
