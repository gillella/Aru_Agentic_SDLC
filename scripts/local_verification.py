"""Validate, execute, and bind exact-head local verification evidence."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import Any, Callable

from common import KernelError, gh_json, run

VERIFICATION_SECTION_RE = re.compile(
    r"(?ims)^\s*##\s+Verification\s*$\n(?P<section>.*?)(?=^\s*##\s+\S|\Z)"
)
LOCAL_VERIFICATION_MARKER_RE = re.compile(
    r"<!--\s*aru-local-verification:v1\s+(.*?)-->", re.DOTALL
)
LOCAL_VERIFICATION_MARKER_PREFIX_RE = re.compile(
    r"<!--\s*aru-local-verification:", re.IGNORECASE
)
CLOSING_DIRECTIVE_RE = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+\s*$"
)
LOCAL_VERIFICATION_KEYS = {"commands", "head", "results"}
FORBIDDEN_TARGETS = {"", ".", "./", ".\\", "all", "root", "src", "backend", "tests"}
FILE_TARGET_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".mts", ".cts")
PYTEST_NO_EXECUTION_SHORT_FLAGS = {"-V", "-h"}
PYTEST_NO_EXECUTION_LONG_OPTION_FAMILIES = (
    ("co", "collect", "collectonly"),
    ("fixture", "fixtures", "fixturepertest", "fixturespertest"),
    ("marker", "markers"),
    ("help",),
    ("setuponly",),
    ("setupplan",),
    ("version",),
)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _strip_existing_local_verification(body: str) -> str:
    stripped = LOCAL_VERIFICATION_MARKER_RE.sub("", body)
    if LOCAL_VERIFICATION_MARKER_PREFIX_RE.search(stripped):
        raise KernelError("local verification evidence is malformed")
    return stripped.rstrip()


def verification_commands_from_body(body: str) -> list[str]:
    if not isinstance(body, str) or not body.strip():
        raise KernelError("PR body is missing")
    cleaned = _strip_existing_local_verification(body)
    match = VERIFICATION_SECTION_RE.search(cleaned)
    if match is None:
        raise KernelError("Verification section is missing")
    commands: list[str] = []
    for raw_line in match.group("section").splitlines():
        line = raw_line.strip()
        if not line.startswith(("- ", "* ")):
            continue
        entry = line[2:].strip()
        if re.match(r"^\[[ xX]\]\s+", entry):
            continue
        if entry.startswith("`") and entry.endswith("`") and len(entry) >= 2:
            entry = entry[1:-1].strip()
        if entry:
            commands.append(entry)
    if not commands:
        raise KernelError("Verification section must list at least one focused command")
    return commands


def _contains_shell_control(command: str) -> bool:
    return any(marker in command for marker in ("&&", "||", ";", "|", ">", "<", "\n", "\r"))


def _target_base(token: str) -> str:
    base = token.split("::", 1)[0].strip()
    while base.endswith(("/", "\\")):
        base = base[:-1]
    return base


def _has_forbidden_target_syntax(token: str) -> bool:
    base = _target_base(token).lower()
    return (
        not base
        or any(mark in token for mark in ("*", "?", "[", "]", "{", "}"))
        or token.endswith("/...")
        or token.endswith("\\...")
        or token == "..."
        or token.startswith("//...")
        or base in FORBIDDEN_TARGETS
    )


def _explicit_file_target(token: str, *, suffixes: tuple[str, ...] = FILE_TARGET_EXTENSIONS) -> bool:
    base = _target_base(token)
    return not _has_forbidden_target_syntax(token) and base.endswith(suffixes)


def _explicit_module_target(token: str) -> bool:
    return (
        "." in token
        and "/" not in token
        and "\\" not in token
        and not token.endswith(".")
        and not _has_forbidden_target_syntax(token)
    )


def _explicit_package_target(token: str) -> bool:
    base = _target_base(token)
    return not _has_forbidden_target_syntax(token) and base not in FORBIDDEN_TARGETS


def _explicit_name_target(token: str) -> bool:
    return (
        bool(re.fullmatch(r"[A-Za-z0-9_:.@/-]+", token))
        and not _has_forbidden_target_syntax(token)
        and token not in {"--", "test"}
    )


def _non_option_targets(tokens: list[str], option_values: set[str]) -> list[str]:
    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            targets.extend(part for part in tokens[index + 1 :] if part)
            break
        if token.startswith("-"):
            index += 2 if token in option_values and index + 1 < len(tokens) else 1
            continue
        targets.append(token)
        index += 1
    return targets


def _raise_broad(command: str) -> None:
    raise KernelError(f"Verification command is broad or full-suite: {command}")


def _raise_unsupported(command: str) -> None:
    raise KernelError(f"unsupported local verification command: {command}")


def _raise_no_execution(command: str) -> None:
    raise KernelError(f"Verification command must execute tests: {command}")


def _canonical_tool(tokens: list[str]) -> tuple[str, list[str]]:
    if len(tokens) >= 3 and tokens[0] in {"python", "python3"} and tokens[1] == "-m":
        return tokens[2], tokens[3:]
    return tokens[0], tokens[1:]


def _normalized_option_name(token: str) -> str:
    option = token.split("=", 1)[0].lstrip("-").strip().lower()
    return re.sub(r"[^a-z0-9]", "", option)


def _is_pytest_no_execution_flag(token: str) -> bool:
    if token in PYTEST_NO_EXECUTION_SHORT_FLAGS:
        return True
    if not token.startswith("--"):
        return False
    normalized = _normalized_option_name(token)
    return any(
        normalized == alias or alias.startswith(normalized)
        for aliases in PYTEST_NO_EXECUTION_LONG_OPTION_FAMILIES
        for alias in aliases
    )


def _pytest_targets(command: str, args: list[str]) -> None:
    if any(_is_pytest_no_execution_flag(token) for token in args if token.startswith("-")):
        _raise_no_execution(command)
    targets = _non_option_targets(args, {"-k", "-m", "-c", "-o", "--maxfail", "--rootdir", "--confcutdir"})
    if not targets or not all(_explicit_file_target(target, suffixes=(".py",)) for target in targets):
        _raise_broad(command)


def _unittest_targets(command: str, args: list[str]) -> None:
    targets = _non_option_targets(args, {"-k", "-f", "-b", "-c"})
    if not targets or targets[0] == "discover" or not all(_explicit_module_target(target) for target in targets):
        _raise_broad(command)


def _compileall_targets(command: str, args: list[str]) -> None:
    targets = _non_option_targets(args, {"-i", "-x"})
    if not targets or not all(_explicit_file_target(target, suffixes=(".py",)) for target in targets):
        _raise_broad(command)


def _ruff_targets(command: str, args: list[str]) -> None:
    if args[:1] != ["check"]:
        _raise_unsupported(command)
    targets = _non_option_targets(args[1:], {"--config", "--select", "--ignore", "--extend-select", "--extend-ignore"})
    if not targets or not all(_explicit_file_target(target, suffixes=(".py",)) for target in targets):
        _raise_broad(command)


def _generic_lint_targets(command: str, args: list[str], *, allow_modules: bool = False) -> None:
    targets = _non_option_targets(args, {"-c", "--config", "-m", "-p", "--project", "--package", "--module"})
    if not targets:
        _raise_broad(command)
    for target in targets:
        valid = _explicit_file_target(target) or (allow_modules and _explicit_module_target(target))
        if not valid:
            _raise_broad(command)


def _go_test_targets(command: str, args: list[str]) -> None:
    selector_flags = {"-run", "-list"}
    selectors = [args[index + 1] for index, token in enumerate(args[:-1]) if token in selector_flags]
    targets = _non_option_targets(args, selector_flags | {"-count", "-timeout", "-shuffle", "-tags"})
    if not selectors or not all(_explicit_name_target(value) for value in selectors):
        _raise_broad(command)
    if not targets or not all(_explicit_package_target(target) for target in targets):
        _raise_broad(command)


def _cargo_test_targets(command: str, args: list[str]) -> None:
    scoped = False
    test_target = False
    selectors: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            selectors.extend(part for part in args[index + 1 :] if part)
            break
        if token in {"-p", "--package", "--test", "--bin", "--example"}:
            if index + 1 >= len(args) or not _explicit_name_target(args[index + 1]):
                _raise_broad(command)
            scoped = True
            test_target = test_target or token in {"--test", "--bin", "--example"}
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        selectors.append(token)
        index += 1
    if not scoped or (not test_target and not selectors) or not all(_explicit_name_target(token) for token in selectors):
        _raise_broad(command)


def _javascript_test_targets(command: str, tool: str, args: list[str]) -> None:
    if tool == "yarn":
        if not args or args[0] != "test":
            _raise_unsupported(command)
        passthrough = args[1:]
    else:
        if len(args) < 2 or args[0] != "test" or args[1] != "--":
            _raise_broad(command)
        passthrough = args[2:]
    targets = _non_option_targets(passthrough, {"-t", "--testNamePattern", "--runTestsByPath"})
    if not targets or not all(_explicit_file_target(target, suffixes=FILE_TARGET_EXTENSIONS) for target in targets):
        _raise_broad(command)


def _validate_supported_command(command: str, tokens: list[str]) -> None:
    tool, args = _canonical_tool(tokens)
    validator = {
        "pytest": lambda: _pytest_targets(command, args),
        "unittest": lambda: _unittest_targets(command, args),
        "compileall": lambda: _compileall_targets(command, args),
        "ruff": lambda: _ruff_targets(command, args),
        "flake8": lambda: _generic_lint_targets(command, args),
        "eslint": lambda: _generic_lint_targets(command, args),
        "pyright": lambda: _generic_lint_targets(command, args),
        "mypy": lambda: _generic_lint_targets(command, args, allow_modules=True),
        "npm": lambda: _javascript_test_targets(command, tool, args),
        "pnpm": lambda: _javascript_test_targets(command, tool, args),
        "yarn": lambda: _javascript_test_targets(command, tool, args),
    }.get(tool)
    if validator is not None:
        validator()
        return
    if tokens[:2] == ["go", "test"]:
        _go_test_targets(command, tokens[2:])
        return
    if tokens[:2] == ["cargo", "test"]:
        _cargo_test_targets(command, tokens[2:])
        return
    _raise_unsupported(command)


def _prepared_command(command: str) -> dict[str, object]:
    if _contains_shell_control(command):
        raise KernelError("local verification command must not chain shell operations")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise KernelError("local verification command is malformed") from exc
    if not tokens:
        raise KernelError("local verification command is malformed")
    _validate_supported_command(command, tokens)
    return {"argv": tokens, "command": command}


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return run(argv, check=False)


def _execute_commands(prepared: list[dict[str, object]], runner: Runner) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for item in prepared:
        argv = [str(part) for part in item["argv"]]
        result = runner(argv)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise KernelError(f"local verification command failed: {item['command']}: {detail}")
        results.append({"command": item["command"], "argv": argv, "returncode": 0})
    return results


def validate_local_verification_commands(commands: list[str]) -> list[str]:
    if not isinstance(commands, list) or not commands:
        raise KernelError("local verification evidence is missing commands")
    normalized: list[str] = []
    for item in commands:
        if not isinstance(item, str) or not item.strip():
            raise KernelError("local verification command is malformed")
        normalized.append(str(_prepared_command(item.strip())["command"]))
    return normalized


def bind_local_verification(
    body: str,
    head: str,
    *,
    runner: Runner = _default_runner,
) -> tuple[str, list[str]]:
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise KernelError("local verification head is malformed")
    commands = verification_commands_from_body(body)
    prepared = [_prepared_command(command) for command in commands]
    results = _execute_commands(prepared, runner)
    cleaned = _strip_existing_local_verification(body)
    marker = (
        "<!-- aru-local-verification:v1 "
        f"{json.dumps({'commands': commands, 'head': head, 'results': results}, sort_keys=True)} -->"
    )
    return cleaned.rstrip() + "\n\n" + marker + "\n", commands


def _valid_results(commands: list[str], results: Any) -> bool:
    if not isinstance(results, list) or len(results) != len(commands):
        return False
    for command, item in zip(commands, results):
        if not isinstance(item, dict) or set(item) != {"argv", "command", "returncode"}:
            return False
        if item.get("command") != command or item.get("returncode") != 0:
            return False
        argv = item.get("argv")
        if not isinstance(argv, list) or any(not isinstance(part, str) for part in argv):
            return False
        if list(argv) != shlex.split(command):
            return False
    return True


def local_verification_status(body: Any, head: str) -> dict[str, Any]:
    if not isinstance(body, str) or not body.strip():
        return {"state": "failure", "checks": []}
    try:
        section_commands = verification_commands_from_body(body)
    except KernelError:
        return {"state": "failure", "checks": []}
    matches = LOCAL_VERIFICATION_MARKER_RE.findall(body)
    if not matches:
        return {"state": "pending", "checks": section_commands}
    if len(matches) != 1:
        return {"state": "failure", "checks": []}
    try:
        payload = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return {"state": "failure", "checks": []}
    if not isinstance(payload, dict) or set(payload) != LOCAL_VERIFICATION_KEYS:
        return {"state": "failure", "checks": []}
    payload_head = payload.get("head")
    commands = payload.get("commands")
    results = payload.get("results")
    if not isinstance(payload_head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", payload_head):
        return {"state": "failure", "checks": []}
    if not isinstance(commands, list) or any(not isinstance(item, str) for item in commands):
        return {"state": "failure", "checks": []}
    if commands != section_commands or not _valid_results(commands, results):
        return {"state": "failure", "checks": []}
    try:
        checks = validate_local_verification_commands(commands)
    except KernelError:
        return {"state": "failure", "checks": commands}
    if payload_head.lower() != head.lower():
        return {"state": "pending", "checks": checks}
    return {"state": "success", "checks": checks}


def _closing_directives(body: str) -> list[str]:
    return [match.group(0).strip() for match in CLOSING_DIRECTIVE_RE.finditer(body or "")]


def _preserve_closing_directive(body: str, current_body: str) -> str:
    current = _closing_directives(current_body)
    if len(current) != 1:
        raise KernelError("pull request body must contain exactly one closing directive")
    provided = _closing_directives(body)
    if provided and provided != current:
        raise KernelError("verification refresh closing directive conflicts with the existing issue link")
    if provided:
        return body
    return body.rstrip() + f"\n\n{current[0]}\n"


def pull_request_head(number: int) -> str:
    pr = gh_json(["pr", "view", str(number), "--json", "number,headRefOid"])
    head = pr.get("headRefOid") if isinstance(pr, dict) else None
    if pr.get("number") != number or not isinstance(head, str) or len(head) != 40:
        raise KernelError(f"pull request #{number} is unavailable")
    return head


def refresh_verification(
    number: int,
    body: str,
    *,
    runner: Runner = _default_runner,
) -> dict[str, object]:
    pr = gh_json(["pr", "view", str(number), "--json", "number,headRefOid,body"])
    head = pr.get("headRefOid") if isinstance(pr, dict) else None
    current_body = pr.get("body") if isinstance(pr, dict) else None
    if pr.get("number") != number or not isinstance(head, str) or len(head) != 40:
        raise KernelError(f"pull request #{number} is unavailable")
    if not isinstance(current_body, str) or not current_body.strip():
        raise KernelError("pull request body is unavailable")
    updated_body, checks = bind_local_verification(
        _preserve_closing_directive(body, current_body),
        head,
        runner=runner,
    )
    run(["gh", "pr", "edit", str(number), "--body", updated_body])
    return {"pr": number, "head": head, "checks": checks}
