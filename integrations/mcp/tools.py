"""Tool definitions: one per supported kernel lifecycle command.

Each tool is a thin call into the existing helper, invoked as a subprocess with
its own flags. Nothing here reads or writes GitHub directly, so a tool cannot
authorize a transition the command line would refuse.

Project bootstrap (`init_project.py`) is deliberately absent: creating
repositories, Projects and rulesets is an operator action, not an agent one.
"""

from __future__ import annotations

from typing import Any

INT = {"type": "integer"}
STR = {"type": "string"}
BOOL = {"type": "boolean"}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# name -> (script, description, schema, {json field: cli flag}, emits --json)
TOOLS: dict[str, dict[str, Any]] = {
    "aru_next_work": {
        "script": "fetch_next_work.py",
        "description": (
            "Select the one next unit of work for an agent identity: its oldest open pull "
            "request first, otherwise the highest-priority unblocked Ready issue. Read-only; "
            "it never claims, promotes or repairs the board."
        ),
        "schema": _schema({"agent": STR}, ["agent"]),
        "args": {"agent": "--agent"},
        "json": True,
    },
    "aru_triage": {
        "script": "triage_backlog.py",
        "description": (
            "Validate Backlog issues and promote complete, unblocked ones to Ready. "
            "Promotes one issue per run."
        ),
        "schema": _schema({"all": BOOL}, []),
        "args": {"all": "--all"},
        "json": True,
    },
    "aru_claim": {
        "script": "claim_issue.py",
        "description": (
            "Acquire exclusive ownership of a Ready issue, or release it with 'release'. "
            "Refuses on a claim race."
        ),
        "schema": _schema({"issue": INT, "agent": STR, "release": BOOL}, ["issue", "agent"]),
        "args": {"issue": "--issue", "agent": "--agent", "release": "--release"},
        "json": False,
    },
    "aru_create_branch": {
        "script": "create_branch.py",
        "description": (
            "Create the issue's branch and its isolated worktree. Requires the issue to be "
            "In Progress and claimed by this exact agent."
        ),
        "schema": _schema({"issue": INT, "type": {**STR, "enum": ["feat", "fix", "docs"]}, "agent": STR},
                          ["issue", "type", "agent"]),
        "args": {"issue": "--issue", "type": "--type", "agent": "--agent"},
        "json": False,
    },
    "aru_open_pr": {
        "script": "create_pr.py",
        "description": (
            "Open the governed pull request for a claimed issue. The helper appends the "
            "closing directive itself; do not include one in the body."
        ),
        "schema": _schema({"issue": INT, "title": STR, "body": STR, "agent": STR}, ["issue", "title", "body"]),
        "args": {"issue": "--issue", "title": "--title", "body": "--body", "agent": "--agent"},
        "json": True,
    },
    "aru_check_ci": {
        "script": "check_ci.py",
        "description": (
            "Read exact-head governed verification state for a pull request. Fails closed on "
            "a missing, stale or failing required server check."
        ),
        "schema": _schema({"pr": INT}, ["pr"]),
        "args": {"pr": "--pr"},
        "json": True,
    },
    "aru_pr_feedback": {
        "script": "fetch_pr_feedback.py",
        "description": "Read unresolved review findings on a pull request.",
        "schema": _schema({"pr": INT}, ["pr"]),
        "args": {"pr": "--pr"},
        "json": True,
    },
    "aru_merge": {
        "script": "merge_pr.py",
        "description": (
            "Evaluate or perform the governed merge at an exact head. Use dry_run to evaluate "
            "every gate without merging, and finalize to close out a merge GitHub already "
            "confirmed. Refuses unless every gate passes."
        ),
        "schema": _schema(
            {"pr": INT, "expected_head": STR, "dry_run": BOOL, "finalize": BOOL},
            ["pr", "expected_head"],
        ),
        "args": {"pr": "--pr", "expected_head": "--expected-head", "dry_run": "--dry-run", "finalize": "--finalize"},
        "json": True,
    },
    "aru_cleanup": {
        "script": "cleanup_worktrees.py",
        "description": (
            "Remove eligible Factory worktrees. Retains locked, dirty, open and ambiguous ones; "
            "use dry_run to see what would go."
        ),
        "schema": _schema({"dry_run": BOOL}, []),
        "args": {"dry_run": "--dry-run"},
        "json": True,
    },
    "aru_revert": {
        "script": "revert_merge.py",
        "description": "Create the governed reverse gear for a merged pull request. Requires a separate approved revert issue.",
        "schema": _schema({"pr": INT, "revert_issue": INT, "agent": STR}, ["pr", "revert_issue", "agent"]),
        "args": {"pr": "--pr", "revert_issue": "--revert-issue", "agent": "--agent"},
        "json": False,
    },
    "aru_report": {
        "script": "report.py",
        "description": (
            "Read-only delivery report over a time window: merged pull requests, rework after "
            "review, claim-to-merge duration, and observed blocks by gate. Names the gates it "
            "cannot observe, so a low block count is not proof of a clean run."
        ),
        "schema": _schema({"repo": STR, "since": STR, "limit": INT}, []),
        "args": {"repo": "--repo", "since": "--since", "limit": "--limit"},
        "json": True,
    },
}


def descriptors() -> list[dict[str, Any]]:
    """The MCP tools/list payload."""
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
        for name, spec in sorted(TOOLS.items())
    ]


# The JSON Schema types this surface declares, mapped onto the Python types a
# JSON decoder actually produces. `bool` is excluded from "integer" on purpose:
# Python's bool subclasses int, so `true` would otherwise pass as an issue
# number and reach the helper as `--issue True`.
_TYPE_PREDICATES = {
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
}


def _check_value(name: str, key: str, value: Any, declared: dict[str, Any]) -> None:
    """Refuse a value the declared schema does not actually admit.

    Nothing upstream validates these. The server hands through whatever the
    client sent, so without this check a declared `type` or `enum` is only
    advertising: a wrong value reaches the helper's argparse, which exits 2 with
    a usage string the calling agent cannot act on. `null` is refused here too
    rather than skipped, because dropping a value the caller believed it passed
    is the same failure as dropping an unknown key.
    """
    expected = declared.get("type")
    predicate = _TYPE_PREDICATES.get(expected)
    if predicate is None:
        raise ValueError(f"{name}.{key} declares unsupported type {expected!r}")
    if not predicate(value):
        raise ValueError(f"{name}.{key} must be {expected}, got {type(value).__name__}")
    choices = declared.get("enum")
    if choices is not None and value not in choices:
        raise ValueError(f"{name}.{key} must be one of {sorted(choices)}, got {value!r}")


def build_argv(name: str, arguments: dict[str, Any]) -> list[str]:
    """Validate tool arguments and map them onto the helper's own flags.

    Unknown keys are refused rather than dropped: silently ignoring an argument
    an agent believed it was passing is how a caller ends up merging the wrong
    pull request. Declared types and enums are enforced here for the same
    reason, since this is the only gate between the client and the helper.
    """
    spec = TOOLS[name]
    unknown = set(arguments) - set(spec["args"])
    if unknown:
        raise ValueError(f"unknown argument(s) for {name}: {sorted(unknown)}")
    missing = [key for key in spec["schema"]["required"] if key not in arguments]
    if missing:
        raise ValueError(f"missing required argument(s) for {name}: {missing}")
    properties = spec["schema"]["properties"]
    argv: list[str] = []
    for key, flag in spec["args"].items():
        if key not in arguments:
            continue
        value = arguments[key]
        _check_value(name, key, value, properties[key])
        # A false boolean must omit its flag: every one of these maps to a
        # store_true option, so passing the bare flag would invert the caller.
        if isinstance(value, bool):
            if value:
                argv.append(flag)
            continue
        argv += [flag, str(value)]
    if spec["json"]:
        argv.append("--json")
    return argv
