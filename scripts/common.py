#!/usr/bin/env python3
"""Small shared primitives for the Aru minimal kernel."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")
STATUS_PREFIX = "status:"
AGENT_PREFIX = "agent:"
REVIEW_PREFIX = "review:"
REVIEWER_PREFIX = "reviewer:"
REVIEWER_ACTOR_PREFIX = "reviewer-actor:"
REVIEW_REGISTRATION_PREFIX = "reviewer-registered:"
REVIEW_BINDING_PREFIX = "reviewer-binding:"
AUTHOR_PREFIX = "author:"
AUTHOR_FAMILY_PREFIX = "author-family:"
EXTERNAL_REVIEWERS = ("coderabbit", "sourcery", "codeant")
CODING_REVIEWERS = (
    "claude-code",
    "openai-codex",
    "xai-cursor",
    "google-antigravity",
)
REVIEWER_CONFIG_ENV = "ARU_CODING_REVIEWERS"
REVIEW_AUTHORITIES = EXTERNAL_REVIEWERS + CODING_REVIEWERS
PROBE_PROMPT = "Reply exactly OK"
CodingCandidate = tuple[str, str, str, str | None]
ProbeRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
REVIEW_UNAVAILABLE_RE = re.compile(
    r"(?:^\s*(?:error|unavailable)\b|\b(?:quota exhausted|quota exceeded|"
    r"rate[ -]?limit(?:ed|ing)?|reviews? paused|provider outage|service outage|"
    r"unsupported bot(?:-authored)? pr|cannot review|unable to review|"
    r"payment required|insufficient credits?|capacity exhausted|"
    r"cost (?:limit|quota|cap) (?:reached|exceeded))\b)",
    re.IGNORECASE,
)
# Compatibility name for the external-service evidence paths.
REVIEW_SERVICES = EXTERNAL_REVIEWERS
ZERO_SHA = "0" * 40
REPOSITORY_AUTH = "repository"
PROJECT_AUTH = "project"
GITHUB_APP_RUNNER_ENV = "ARU_GITHUB_APP_RUNNER"
_GH_TOKEN_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_REPOSITORY_COMMANDS = {"api", "issue", "label", "pr", "repo"}


class KernelError(RuntimeError):
    """A fail-closed authority or command error."""


def review_evidence_unavailable(record: dict[str, Any]) -> bool:
    text = "\n".join(
        str(record.get(key) or "")
        for key in ("body", "description", "name", "context")
    )
    return bool(REVIEW_UNAVAILABLE_RE.search(text))


def configured_coding_reviewers(
    value: str | None = None,
) -> dict[str, tuple[tuple[str, str | None], ...]]:
    raw = os.environ.get(REVIEWER_CONFIG_ENV, "") if value is None else value
    if not raw.strip():
        raise KernelError(f"{REVIEWER_CONFIG_ENV} is missing")
    configured: dict[str, list[tuple[str, str | None]]] = {}
    identities: set[str] = set()
    subscriptions: set[str] = set()
    for entry in raw.split(","):
        family, separator, candidate = entry.strip().partition(":")
        identity, marker, subscription = candidate.partition("@")
        if (
            not separator
            or family not in CODING_REVIEWERS
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identity)
            or identity in identities
        ):
            raise KernelError(f"{REVIEWER_CONFIG_ENV} is malformed or ambiguous")
        if family == "claude-code":
            if not marker or not re.fullmatch(r"[1-9][0-9]*", subscription):
                raise KernelError(f"{REVIEWER_CONFIG_ENV} Claude subscription is malformed")
            if subscription in subscriptions:
                raise KernelError(f"{REVIEWER_CONFIG_ENV} repeats a Claude subscription")
            subscriptions.add(subscription)
        elif marker or configured.get(family):
            raise KernelError(f"{REVIEWER_CONFIG_ENV} non-Claude reviewer is ambiguous")
        identities.add(identity)
        configured.setdefault(family, []).append((identity, subscription or None))
    return {family: tuple(candidates) for family, candidates in configured.items()}


def configured_reviewer_family(identity: str) -> str | None:
    if not os.environ.get(REVIEWER_CONFIG_ENV, "").strip():
        return None
    for family, candidates in configured_coding_reviewers().items():
        if any(candidate == identity for candidate, _subscription in candidates):
            return family
    return None


def normalized_identity(value: str) -> str:
    identity = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not identity:
        raise KernelError("review identity is empty")
    return identity[:80]


def registered_coding_actors() -> dict[str, str]:
    records = gh_json(["label", "list", "--limit", "1000", "--json", "name"])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise KernelError("coding reviewer identity bindings are unavailable")
    bindings: dict[str, str] = {}
    for item in records:
        name = str(item.get("name") or "")
        if not name.startswith(REVIEW_BINDING_PREFIX):
            continue
        values = name[len(REVIEW_BINDING_PREFIX) :].split("=", 1)
        if len(values) != 2:
            raise KernelError("coding reviewer identity binding is malformed")
        identity, actor = values[0].lower(), values[1].lower()
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identity)
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?",
                actor,
            )
            or identity in bindings
        ):
            raise KernelError("coding reviewer identity binding is malformed or ambiguous")
        bindings[identity] = actor
    return bindings


def coding_reviewer_candidates(
    *,
    author_identity: str,
    author_actor: str = "",
    reviewer_actors: dict[str, str] | None = None,
) -> list[CodingCandidate]:
    if not os.environ.get(REVIEWER_CONFIG_ENV, "").strip():
        actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
        if actors:
            raise KernelError(
                f"{REVIEWER_CONFIG_ENV} is missing while reviewer bindings exist"
            )
        return []
    author_identity = normalized_identity(author_identity)
    author_actor = author_actor.lower()
    actors = registered_coding_actors() if reviewer_actors is None else reviewer_actors
    configured = configured_coding_reviewers()
    candidates: list[CodingCandidate] = []
    for family in CODING_REVIEWERS:
        for identity, subscription in configured.get(family, ()):
            actor = str(actors.get(identity) or "").lower()
            if identity == author_identity or not actor or actor == author_actor:
                continue
            candidates.append((family, identity, actor, subscription))
    return candidates


def _reviewer_command(name: str) -> str | None:
    return shutil.which(name)


def _probe_ok(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 and result.stdout.strip() == "OK"


def probe_coding_candidate(candidate: CodingCandidate, runner: ProbeRunner) -> bool:
    family, _identity, _actor, subscription = candidate
    if family == "claude-code":
        executable = str(Path.home() / ".local" / "bin" / "claude-sub")
        return _probe_ok(runner([executable, str(subscription), "-p", PROBE_PROMPT]))
    command_names = {
        "openai-codex": "codex",
        "xai-cursor": "cursor-agent",
        "google-antigravity": "agy",
    }
    executable = _reviewer_command(command_names[family])
    if executable is None:
        return False
    arguments = {
        "openai-codex": [executable, "exec", "--skip-git-repo-check", PROBE_PROMPT],
        "xai-cursor": [executable, "-p", PROBE_PROMPT],
        "google-antigravity": [executable, "-p", PROBE_PROMPT],
    }[family]
    return _probe_ok(runner(arguments))


def agent_family(identity: str) -> str:
    value = normalized_identity(identity)
    configured = configured_reviewer_family(value)
    if configured:
        return configured
    aliases = {
        "claude-code": ("claude",),
        "openai-codex": ("codex", "openai"),
        "xai-cursor": ("cursor", "xai"),
        "google-antigravity": ("antigravity", "google", "agy"),
    }
    for family, needles in aliases.items():
        if any(needle in value for needle in needles):
            return family
    return "human-or-other"


def _graphql_query(args: list[str]) -> str:
    values = [part.split("=", 1)[1] for part in args if part.startswith("query=")]
    if len(values) != 1:
        raise KernelError("GraphQL query is missing or ambiguous")
    return values[0]


def _github_authority(args: list[str], auth: str | None) -> str:
    if auth not in {None, REPOSITORY_AUTH, PROJECT_AUTH}:
        raise KernelError(f"unsupported GitHub authority: {auth}")
    if not args:
        raise KernelError("GitHub command is missing")
    if args[:2] == ["api", "graphql"]:
        if auth is None:
            raise KernelError("GraphQL authority is ambiguous; declare repository or project")
        query = _graphql_query(args)
        has_project = bool(re.search(r"\b(?:projectsV2|ProjectV2|projectV2)\b", query))
        has_repository_data = bool(
            re.search(r"\b(?:pullRequest|reviewThreads|issues|refs|commit)\b", query)
        )
        if has_project and has_repository_data:
            raise KernelError("GraphQL query mixes repository and Project V2 authority")
        if auth == REPOSITORY_AUTH and has_project:
            raise KernelError("Project V2 GraphQL requires project authority")
        return auth
    inferred = PROJECT_AUTH if args[0] == "project" else None
    if args[0] in _REPOSITORY_COMMANDS:
        inferred = REPOSITORY_AUTH
    if inferred is None:
        raise KernelError(f"GitHub command authority is ambiguous: {args[0]}")
    if auth is not None and auth != inferred:
        raise KernelError(f"GitHub command requires {inferred} authority")
    return inferred


def _github_command(
    args: list[str], auth: str | None
) -> tuple[list[str], dict[str, str] | None]:
    authority = _github_authority(args, auth)
    if authority == PROJECT_AUTH:
        environment = os.environ.copy()
        for name in _GH_TOKEN_ENV:
            environment.pop(name, None)
        return ["gh", *args], environment
    runner = os.environ.get(GITHUB_APP_RUNNER_ENV)
    if not runner:
        return ["gh", *args], None
    runner_path = Path(runner).expanduser()
    if not runner_path.is_file() or not os.access(runner_path, os.X_OK):
        raise KernelError("configured GitHub App runner is not executable")
    return [str(runner_path), "--", "gh", *args], None


def _redact_diagnostic(value: str) -> str:
    redacted = value
    for name in _GH_TOKEN_ENV:
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_-]+|github_pat_[A-Za-z0-9_-]+)\b",
        "[REDACTED]",
        redacted,
    )
    return redacted


def run(
    argv: Iterable[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    input_text: str | None = None,
    auth: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(part) for part in argv]
    environment = None
    if command and command[0] == "gh":
        command, environment = _github_command(command[1:], auth)
    elif auth is not None:
        raise KernelError("GitHub authority was provided for a non-GitHub command")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    result = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        _redact_diagnostic(result.stdout or ""),
        _redact_diagnostic(result.stderr or ""),
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise KernelError(f"{command[0]} failed: {detail}")
    return result


def gh_json(
    args: Iterable[str],
    *,
    cwd: str | Path | None = None,
    auth: str | None = None,
) -> Any:
    result = run(["gh", *args], cwd=cwd, auth=auth)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KernelError("GitHub returned malformed JSON") from exc


def gh_paginated(endpoint: str, *, cwd: str | Path | None = None) -> list[dict[str, Any]]:
    pages = gh_json(["api", "--paginate", "--slurp", endpoint], cwd=cwd)
    if not isinstance(pages, list):
        raise KernelError("GitHub returned malformed paginated data")
    records: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise KernelError("GitHub returned malformed paginated page")
        records.extend(page)
    return records


def git(args: Iterable[str], *, cwd: str | Path | None = None) -> str:
    return run(["git", "-c", "core.fsmonitor=false", *args], cwd=cwd).stdout.strip()


def repo_root(cwd: str | Path | None = None) -> Path:
    return Path(git(["rev-parse", "--show-toplevel"], cwd=cwd)).resolve()


def primary_worktree(cwd: str | Path | None = None) -> Path:
    raw = git(["worktree", "list", "--porcelain"], cwd=cwd)
    first = raw.splitlines()[0] if raw else ""
    key, _, value = first.partition(" ")
    if key != "worktree" or not value:
        raise KernelError("unable to resolve the primary worktree")
    return Path(value).resolve()


def repo_slug(cwd: str | Path | None = None) -> str:
    data = gh_json(["repo", "view", "--json", "nameWithOwner"], cwd=cwd)
    slug = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(slug, str) or slug.count("/") != 1:
        raise KernelError("unable to resolve repository identity")
    return slug


def label_names(record: dict[str, Any]) -> list[str]:
    labels = record.get("labels", [])
    if not isinstance(labels, list):
        raise KernelError("GitHub returned malformed labels")
    names: list[str] = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if not isinstance(name, str):
            raise KernelError("GitHub returned malformed label")
        names.append(name)
    return names


def issue(number: int, *, cwd: str | Path | None = None) -> dict[str, Any]:
    data = gh_json(
        [
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,state,labels,assignees,url",
        ],
        cwd=cwd,
    )
    if not isinstance(data, dict) or data.get("number") != number:
        raise KernelError(f"issue #{number} is unavailable")
    return data


def list_issues(
    *,
    state: str = "open",
    label: str | None = None,
    cwd: str | Path | None = None,
) -> list[dict[str, Any]]:
    args = [
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        "200",
        "--json",
        "number,title,body,state,labels,assignees,url",
    ]
    if label:
        args.extend(["--label", label])
    data = gh_json(args, cwd=cwd)
    if not isinstance(data, list):
        raise KernelError("GitHub returned malformed issue inventory")
    return data


def status_of(record: dict[str, Any]) -> str | None:
    values = [name for name in label_names(record) if name.startswith(STATUS_PREFIX)]
    if len(values) > 1:
        raise KernelError("issue has contradictory status labels")
    if not values:
        return None
    slug = values[0][len(STATUS_PREFIX) :]
    for status in STATUSES:
        if slug == status.lower().replace(" ", "-"):
            return status
    raise KernelError(f"unsupported status label: {values[0]}")


def status_label(status: str) -> str:
    if status not in STATUSES:
        raise KernelError(f"unsupported status: {status}")
    return STATUS_PREFIX + status.lower().replace(" ", "-")


def parse_touches(body: str) -> list[str]:
    inline = re.findall(r"(?im)^\s*touches:\s*(.+?)\s*$", body or "")
    sections = re.findall(
        r"(?ims)^###\s+touches:\s*$\n(.*?)(?=^#{1,3}\s+|\Z)",
        body or "",
    )
    declarations = [*inline, *(section.strip() for section in sections)]
    if len(declarations) != 1:
        raise KernelError("issue must contain exactly one touches: declaration")
    declaration = declarations[0]
    if len(declaration.splitlines()) != 1:
        raise KernelError("touches: declaration must be a single line")
    paths = [part.strip() for part in declaration.split(",") if part.strip()]
    if not paths:
        raise KernelError("touches: must declare at least one path")
    if any(not safe_declared_path(path) for path in paths):
        raise KernelError("touches: contains an unsafe path")
    return paths


def safe_declared_path(value: str) -> bool:
    if "\\" in value or value.startswith(("/", "~", "-")):
        return False
    raw = value[:-3] if value.endswith("/**") else value
    path = PurePosixPath(raw)
    return bool(raw and raw != "." and ".." not in path.parts)


def path_allowed(path: str, declared: Iterable[str]) -> bool:
    candidate = PurePosixPath(path).as_posix()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    if not safe_declared_path(candidate):
        return False
    for rule in declared:
        prefix = rule[:-3].rstrip("/") if rule.endswith("/**") else None
        if candidate == rule or (prefix and (candidate == prefix or candidate.startswith(prefix + "/"))):
            return True
    return False


def acceptance_items(body: str) -> list[tuple[bool, str]]:
    match = re.search(
        r"(?ims)^#{2,3}\s+Acceptance Criteria\s*$\n(.*?)(?=^#{2,3}\s+|\Z)",
        body or "",
    )
    if not match:
        return []
    items = re.findall(r"(?im)^\s*-\s*\[([ xX])\]\s*(\S.*)$", match.group(1))
    return [(mark.lower() == "x", text.strip()) for mark, text in items]


def dependencies(body: str) -> list[int]:
    return sorted({int(value) for value in re.findall(r"(?im)^\s*depends-on:\s*#(\d+)\s*$", body or "")})


def contract_errors(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    items = acceptance_items(str(record.get("body") or ""))
    if not items or not any(not done for done, _ in items):
        errors.append("Acceptance Criteria must contain an unchecked item")
    try:
        parse_touches(str(record.get("body") or ""))
    except KernelError as exc:
        errors.append(str(exc))
    if record.get("state") != "OPEN":
        errors.append("issue is not open")
    return errors


def unresolved_dependencies(record: dict[str, Any], *, cwd: str | Path | None = None) -> list[int]:
    unresolved: list[int] = []
    for number in dependencies(str(record.get("body") or "")):
        if issue(number, cwd=cwd).get("state") != "CLOSED":
            unresolved.append(number)
    return unresolved


def ensure_label(
    name: str,
    *,
    color: str = "5319e7",
    description: str = "",
    cwd: str | Path | None = None,
) -> None:
    run(
        [
            "gh",
            "label",
            "create",
            name,
            "--color",
            color,
            "--description",
            description,
            "--force",
        ],
        cwd=cwd,
    )


def linked_project(*, cwd: str | Path | None = None) -> dict[str, Any]:
    slug = repo_slug(cwd)
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!,$name:String!){
      repository(owner:$owner,name:$name){
        projectsV2(first:20){
          nodes{id number title closed}
          pageInfo{hasNextPage}
        }
      }
    }
    """
    data = gh_json(
        ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}"],
        cwd=cwd,
        auth=PROJECT_AUTH,
    )
    connection = ((data.get("data") or {}).get("repository") or {}).get("projectsV2") or {}
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise KernelError("linked Project Board is unavailable")
    if (connection.get("pageInfo") or {}).get("hasNextPage"):
        raise KernelError("linked Project Board inventory is truncated")
    open_projects = [node for node in nodes if isinstance(node, dict) and not node.get("closed")]
    requested = os.environ.get("ARU_PROJECT_NUMBER")
    if requested:
        open_projects = [node for node in open_projects if str(node.get("number")) == requested]
    if len(open_projects) != 1:
        raise KernelError("expected exactly one linked open Project Board")
    return open_projects[0]


def board_edit(
    number: int,
    status: str,
    *,
    cwd: str | Path | None = None,
) -> list[str]:
    project = linked_project(cwd=cwd)
    slug = repo_slug(cwd)
    owner = slug.split("/", 1)[0]
    project_number = str(project["number"])
    items = gh_json(
        [
            "project",
            "item-list",
            project_number,
            "--owner",
            owner,
            "--limit",
            "1000",
            "--format",
            "json",
        ],
        cwd=cwd,
    )
    fields = gh_json(
        ["project", "field-list", project_number, "--owner", owner, "--format", "json"],
        cwd=cwd,
    )
    if not isinstance(items, dict) or not isinstance(fields, dict):
        raise KernelError("Project Board inventory is malformed")
    if isinstance(items.get("totalCount"), int) and items["totalCount"] != len(items.get("items", [])):
        raise KernelError("Project Board item inventory is truncated")
    if isinstance(fields.get("totalCount"), int) and fields["totalCount"] != len(fields.get("fields", [])):
        raise KernelError("Project Board field inventory is truncated")
    issue_items = [
        item
        for item in items.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("content"), dict)
        and item["content"].get("number") == number
        and item["content"].get("repository") == slug
    ]
    status_fields = [field for field in fields.get("fields", []) if field.get("name") == "Status"]
    if len(issue_items) != 1 or len(status_fields) != 1:
        raise KernelError("issue or Status field is ambiguous on the linked Project Board")
    options = [option for option in status_fields[0].get("options", []) if option.get("name") == status]
    if len(options) != 1:
        raise KernelError(f"Project Board has no unique {status!r} option")
    return [
        "project",
        "item-edit",
        "--id",
        str(issue_items[0]["id"]),
        "--project-id",
        str(project["id"]),
        "--field-id",
        str(status_fields[0]["id"]),
        "--single-select-option-id",
        str(options[0]["id"]),
    ]


def set_status(number: int, status: str, *, cwd: str | Path | None = None) -> None:
    record = issue(number, cwd=cwd)
    current = status_of(record)
    if current == status:
        return
    edit = board_edit(number, status, cwd=cwd)
    target = status_label(status)
    ensure_label(target, color="1d76db", description=f"Board status: {status}", cwd=cwd)
    args = ["gh", "issue", "edit", str(number), "--add-label", target]
    if current:
        args.extend(["--remove-label", status_label(current)])
    run(args, cwd=cwd)
    try:
        run(["gh", *edit], cwd=cwd)
    except KernelError:
        rollback = ["gh", "issue", "edit", str(number), "--remove-label", target]
        if current:
            rollback.extend(["--add-label", status_label(current)])
        run(rollback, cwd=cwd, check=False)
        raise


def json_print(data: Any) -> None:
    print(json.dumps(data, sort_keys=True))
