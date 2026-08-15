#!/usr/bin/env python3
"""Execute issue-sourced acceptance-criteria `verify:` commands.

Trust boundary
--------------
Issue bodies are untrusted input (ARU-SOFTWARE-FACTORY.md §3.6). A `verify:`
command turns that text into a live execution vector. This module is the
boundary that keeps the vector from becoming a shell:

- Commands are parsed as data and executed as argv (`shell=False`).
- The raw command string is rejected if it contains shell metacharacters.
- argv[0] must be a bare name on an explicit runner allowlist.
- `python` / `python3` may only run `-m unittest`, or the explicit verifier
  `scripts/verify_citations.py`.
- Each command is bounded by VERIFY_TIMEOUT_SECONDS; timeouts are failed evidence.
- No `python -c`, no other `-m` modules, no absolute paths, no `env`/`bash`
  prefixes, no path traversal, no arbitrary `scripts/*.py`.

Today the board is authored by trusted operators, so the practical risk is
low. The allowlist exists so that opening the factory to external issues
cannot silently start executing attacker-controlled shells. Do not widen it
without a dedicated security review.

Criteria without an extractable command keep today's checkbox behaviour.
Backticked `verify:` / indented `verify:` lines are commands; same-line
prose such as `verify: artifact comment...` is not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from common import VERIFICATION_EVIDENCE_SCHEMA, sanitize_command

ALLOWED_RUNNERS = frozenset({"python3", "python", "pytest"})
ALLOWED_PYTHON_SCRIPTS = frozenset({
    "scripts/verify_citations.py",
})
PYTHON_UNITTEST_FLAGS = frozenset({"-v", "-q", "-b", "-f"})
PYTHON_SCRIPT_FLAGS = frozenset({"-q", "-v", "--check"})
PYTEST_FLAGS = frozenset({"-q", "-v", "--tb=short", "--quiet"})
VERIFY_TIMEOUT_SECONDS = 120
UNITTEST_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
SHELL_META_RE = re.compile(r"""[;&|`$()<>\n\r*?\[\]{}!#\\]|&&|\|\|""")
AC_HEADING_RE = (
    r"^\s*#{1,4}\s*(?:acceptance\s+criteria(?:\s*[/:]\s*expected\s+behavior)?"
    r"|expected\s+behavior(?:\s*[/:]\s*predicates)?)\s*$"
)
CHECKBOX_RE = re.compile(
    r"^(\s*)[-*]\s*\[(?P<tick>[ xX])\]\s*(?P<text>.*)$"
)
FENCED_VERIFY_RE = re.compile(
    r"\((?:verify|verify_cmd):\s*`([^`]+)`\)"
    r"|\b(?:verify|verify_cmd)\s*:\s*`([^`]+)`",
    re.IGNORECASE,
)
INDENTED_VERIFY_RE = re.compile(
    r"^\s+verify(?:_cmd)?\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)


class CommandRejected(ValueError):
    """A `verify:` command was present but must not be executed."""


class Criterion(NamedTuple):
    text: str
    ticked: bool
    command: Optional[str]
    argv: Optional[List[str]]
    rejected: Optional[str]


def acceptance_section(issue_body: str) -> str:
    """Returns the Acceptance Criteria section, or empty if none exists."""
    if not issue_body:
        return ""
    parts = re.split(AC_HEADING_RE, issue_body, flags=re.IGNORECASE | re.MULTILINE)
    if len(parts) < 2:
        return ""
    return re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]


def _is_safe_relpath(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    parts = path.split("/")
    if ".." in parts or "" in parts or parts[0] not in {"tests", "scripts", "docs"}:
        return False
    return True


def _safe_script_arg(arg: str) -> bool:
    if not arg or arg.startswith("/") or arg.startswith("~") or arg.startswith("-"):
        return False
    return ".." not in arg.split("/")


def _validate_unittest_targets(args: List[str]) -> None:
    if not args:
        raise CommandRejected("unittest is missing a target")
    for arg in args:
        if arg in PYTHON_UNITTEST_FLAGS or arg == "discover":
            continue
        if UNITTEST_MODULE.match(arg) or _is_safe_relpath(arg):
            continue
        raise CommandRejected(f"unittest target not allowed: {arg}")


def _validate_python_argv(argv: List[str]) -> None:
    if len(argv) < 2:
        raise CommandRejected("python invocation is missing arguments")
    if argv[1] == "-m":
        if len(argv) < 3 or argv[2] != "unittest":
            raise CommandRejected("python -m is limited to unittest")
        _validate_unittest_targets(argv[3:])
        return
    if argv[1].startswith("-"):
        raise CommandRejected("python flags other than -m unittest are not allowed")
    if argv[1] not in ALLOWED_PYTHON_SCRIPTS:
        raise CommandRejected(
            f"python script {argv[1]!r} is not an allowlisted verifier"
        )
    for arg in argv[2:]:
        if arg in PYTHON_SCRIPT_FLAGS:
            continue
        if arg.startswith("-") or not _safe_script_arg(arg):
            raise CommandRejected(f"python script argument not allowed: {arg}")


def _validate_pytest_argv(argv: List[str]) -> None:
    if len(argv) < 2:
        raise CommandRejected("pytest is missing a target")
    for arg in argv[1:]:
        if arg in PYTEST_FLAGS:
            continue
        if arg.startswith("-") or not _is_safe_relpath(arg):
            raise CommandRejected(f"pytest argument not allowed: {arg}")


def validate_command(text: str) -> List[str]:
    """Parses a verify command into argv, or raises CommandRejected."""
    stripped = (text or "").strip()
    if not stripped:
        raise CommandRejected("empty command")
    if SHELL_META_RE.search(stripped):
        raise CommandRejected("shell metacharacters are not allowed")
    try:
        argv = shlex.split(stripped, posix=True)
    except ValueError as exc:
        raise CommandRejected(f"cannot parse command: {exc}") from exc
    if not argv:
        raise CommandRejected("empty command")
    runner = argv[0]
    if runner != os.path.basename(runner) or runner not in ALLOWED_RUNNERS:
        raise CommandRejected(f"runner {runner!r} is not allowlisted")
    if runner in {"python3", "python"}:
        _validate_python_argv(argv)
    else:
        _validate_pytest_argv(argv)
    return argv


def _extract_command(blob: str, continuation: List[str]) -> Optional[str]:
    match = FENCED_VERIFY_RE.search(blob)
    if match:
        return (match.group(1) or match.group(2) or "").strip() or None
    for line in continuation:
        indented = INDENTED_VERIFY_RE.match(line)
        if not indented:
            continue
        raw = indented.group(1).strip().strip("`")
        return raw or None
    return None


def _criteria_blocks(section: str) -> List[Tuple[str, bool, List[str]]]:
    blocks: List[Tuple[str, bool, List[str]]] = []
    current: Optional[Tuple[str, bool, List[str]]] = None
    for line in section.splitlines():
        checkbox = CHECKBOX_RE.match(line)
        if checkbox:
            if current is not None:
                blocks.append(current)
            current = (
                checkbox.group("text").strip(),
                checkbox.group("tick").lower() == "x",
                [],
            )
            continue
        if current is not None and line.strip() and not line.strip().startswith("#"):
            current[2].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def parse_criteria(issue_body: str) -> List[Criterion]:
    """Parses Acceptance Criteria checkboxes and optional verify commands."""
    parsed: List[Criterion] = []
    for text, ticked, continuation in _criteria_blocks(acceptance_section(issue_body)):
        blob = "\n".join([text, *continuation])
        command = _extract_command(blob, continuation)
        if command is None:
            parsed.append(Criterion(text, ticked, None, None, None))
            continue
        try:
            argv = validate_command(command)
        except CommandRejected as exc:
            parsed.append(Criterion(text, ticked, command, None, str(exc)))
            continue
        parsed.append(Criterion(text, ticked, command, argv, None))
    return parsed


def _run_verify(
    argv: List[str],
    cwd: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    check: bool = False,
    timeout: int = VERIFY_TIMEOUT_SECONDS,
) -> Tuple[int, str, str]:
    """Runs one issue-sourced command with a bounded timeout and no shell."""
    del check
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        code, stdout, stderr = result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        code, stdout, stderr = 124, "", f"timed out after {timeout}s"
    except Exception as exc:
        code, stdout, stderr = 1, "", str(exc)
    if evidence is not None:
        evidence.append({
            "command": sanitize_command(list(argv)),
            "duration_seconds": round(max(0.0, time.monotonic() - started), 3),
            "exit_code": code,
            "status": "passed" if code == 0 else "failed",
        })
    return code, stdout, stderr


def run_parsed(
    criteria: List[Criterion],
    cwd: Optional[str] = None,
    run_cmd_fn: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    """Executes allowlisted commands; never runs a rejected command."""
    runner = run_cmd_fn or _run_verify
    records: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    for item in criteria:
        if item.rejected:
            errors.append(("rejected", item.rejected))
            continue
        if not item.argv:
            continue
        code, _, _ = runner(item.argv, check=False, cwd=cwd, evidence=records)
        if code != 0:
            errors.append(("failed", item.text))
    return {"criteria": criteria, "records": records, "errors": errors}


def run_issue(
    issue_body: str,
    cwd: Optional[str] = None,
    run_cmd_fn: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    """Parses an issue body and executes its verify commands."""
    return run_parsed(parse_criteria(issue_body), cwd=cwd, run_cmd_fn=run_cmd_fn)


def merge_into_evidence(
    evidence: Optional[Dict[str, Any]],
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Appends runner records to an aru.verification.v1 evidence object."""
    merged = dict(evidence or {})
    commands = list(merged.get("commands") or [])
    commands.extend(records)
    status = merged.get("status") or "not_run"
    if commands:
        status = (
            "failed"
            if any(record.get("exit_code") != 0 for record in commands)
            else "passed"
        )
    merged["commands"] = commands
    merged["schema"] = VERIFICATION_EVIDENCE_SCHEMA
    merged["status"] = status
    return merged


def evaluate_issue(
    issue_body: str,
    cwd: Optional[str] = None,
    execute: bool = True,
    run_cmd_fn: Optional[Callable[..., Tuple[int, str, str]]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Returns (ok, message, run_result) for merge-gate callers."""
    parsed = parse_criteria(issue_body)
    rejected = [item for item in parsed if item.rejected]
    if rejected:
        return False, f"illegal verify: command: {rejected[0].rejected}", {
            "criteria": parsed, "records": [], "errors": [("rejected", rejected[0].rejected)],
        }
    if execute:
        result = run_parsed(parsed, cwd=cwd, run_cmd_fn=run_cmd_fn)
        failed = [item for item in result["errors"] if item[0] == "failed"]
        if failed:
            return False, f"verify: command failed for: {failed[0][1]}", result
    else:
        result = {"criteria": parsed, "records": [], "errors": []}
    pending = [item.text for item in parsed if item.argv is None and not item.ticked]
    if pending:
        return False, "unticked acceptance criteria without verify: commands", result
    if execute:
        return True, "acceptance criteria passed", result
    return True, "verify: commands validated; execution deferred to merge", result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run allowlisted acceptance-criteria verify: commands."
    )
    parser.add_argument("--body-file", help="Issue body markdown file")
    parser.add_argument("--body", help="Issue body markdown text")
    parser.add_argument("--cwd", default=".", help="Checkout to execute in")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result")
    args = parser.parse_args()
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as handle:
            body = handle.read()
    else:
        body = args.body or ""
    ok, message, result = evaluate_issue(body, cwd=args.cwd)
    payload = {
        "ok": ok,
        "message": message,
        "errors": result["errors"],
        "records": result["records"],
        "evidence": merge_into_evidence(None, result["records"]),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(message)
        for kind, detail in result["errors"]:
            print(f"  {kind}: {detail}", file=sys.stderr)
    if not ok and any(kind == "rejected" for kind, _ in result["errors"]):
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
