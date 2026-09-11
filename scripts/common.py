#!/usr/bin/env python3
"""Small shared primitives for the Aru minimal kernel."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from review_risk import review_risk_tier as review_risk_tier

from touches import (
    TouchesError,
    parse_touches as _parse_touches,
    path_allowed as _path_allowed,
    safe_declared_path as _safe_declared_path,
)

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")
STATUS_PREFIX = "status:"
AGENT_PREFIX = "agent:"
ZERO_SHA = "0" * 40
REPOSITORY_AUTH = "repository"
PROJECT_AUTH = "project"
GITHUB_APP_RUNNER_ENV = "ARU_GITHUB_APP_RUNNER"
QUOTA_STOP_MESSAGE = "GitHub GraphQL quota exhausted; stop and wait for the budget to reset"
_QUOTA_RE = re.compile(
    r"(?:HTTP\s*429|RATE_LIMITED|rate[_ -]?limit(?:ed|ing)?|"
    r"secondary rate limit|resource[- ]limits? exceeded|"
    r"MAX_NODE_LIMIT_EXCEEDED|API rate limit exceeded)",
    re.IGNORECASE,
)
_LINKED_PROJECT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_GH_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_REPOSITORY_COMMANDS = {"api", "issue", "label", "pr", "repo"}
_REMOTE_URL_RE = re.compile(r"^remote\.(.+)\.url (.+)$", re.MULTILINE)
_GITHUB_REMOTE_RE = re.compile(
    r"\A(?:[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?github\.com(?::\d+)?/|(?:[^@/]*@)?github\.com:)"
    r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?\Z",
    re.IGNORECASE,
)


class KernelError(RuntimeError):
    """A fail-closed authority or command error."""


class StatusPreconditionError(KernelError):
    """A fail-closed precondition error before issue or card mutation has begun."""


def canonical_github_actor(value: str) -> str:
    actor = value.strip().lower()
    match = re.fullmatch(r"app/([a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?)", actor)
    if match:
        return f"{match.group(1)}[bot]"
    return actor


def same_github_actor(left: str, right: str) -> bool:
    if not left.strip() or not right.strip():
        return False
    return canonical_github_actor(left) == canonical_github_actor(right)


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
        has_repository_data = bool(re.search(r"\b(?:pullRequest|reviewThreads|issues|refs|commit)\b", query))
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


def checkout_repository(cwd: str | Path | None = None) -> str:
    """Return the governed OWNER/REPO this checkout speaks for, or fail closed.

    Remote URLs can embed credentials, so no remote value reaches a diagnostic.
    """
    try:
        listing = git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=cwd)
    except KernelError:
        listing = ""
    remotes = dict(_REMOTE_URL_RE.findall(listing))
    urls = [remotes["origin"]] if "origin" in remotes else list(remotes.values())
    slugs = set()
    for match in filter(None, (_GITHUB_REMOTE_RE.match(url.strip()) for url in urls)):
        slug = match.group("slug")
        if ".." not in slug and not slug.endswith("/."):
            slugs.add(slug)
    if len(slugs) > 1:
        raise KernelError("governed repository identity is ambiguous across checkout remotes")
    if not slugs:
        raise KernelError("unable to resolve the governed repository identity from the checkout")
    return slugs.pop()


def _github_command(args: list[str], auth: str | None, cwd: str | Path | None = None) -> tuple[list[str], dict[str, str] | None]:
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
    return [str(runner_path), "--repo", checkout_repository(cwd), "--", "gh", *args], None


def _redact_diagnostic(value: str) -> str:
    redacted = value
    for name in _GH_TOKEN_ENV:
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_-]+|github_pat_[A-Za-z0-9_-]+)\b", "[REDACTED]", redacted)
    return redacted


def run(
    argv: Iterable[str], *, cwd: str | Path | None = None, check: bool = True,
    input_text: str | None = None, auth: str | None = None, timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(part) for part in argv]
    environment = None
    if command and command[0] == "gh":
        command, environment = _github_command(command[1:], auth, cwd)
    elif auth is not None:
        raise KernelError("GitHub authority was provided for a non-GitHub command")
    try:
        result = subprocess.run(command, cwd=cwd, env=environment, input=input_text, text=True, capture_output=True, check=False, **({"timeout": timeout} if timeout is not None else {}))
    except subprocess.TimeoutExpired as exc:
        raise KernelError("bounded command timed out") from exc
    result = subprocess.CompletedProcess(result.args, result.returncode, _redact_diagnostic(result.stdout or ""), _redact_diagnostic(result.stderr or ""))
    if result.returncode:
        _raise_if_quota(result.stdout, result.stderr)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise KernelError(f"{command[0]} failed: {detail}")
    return result


def _raise_if_quota(*parts: str) -> None:
    if any(_QUOTA_RE.search(part or "") for part in parts):
        raise KernelError(QUOTA_STOP_MESSAGE)


def _raise_if_graphql_quota(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("errors"), list):
        return
    blobs: list[str] = []
    for error in data["errors"]:
        if isinstance(error, dict):
            blobs.append(str(error.get("type") or ""))
            blobs.append(str(error.get("message") or ""))
        else:
            blobs.append(str(error))
    _raise_if_quota(*blobs)


def gh_json(args: Iterable[str], *, cwd: str | Path | None = None, auth: str | None = None, timeout: float | None = None) -> Any:
    result = run(["gh", *args], cwd=cwd, auth=auth, **({"timeout": timeout} if timeout is not None else {}))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KernelError("GitHub returned malformed JSON") from exc
    _raise_if_graphql_quota(data)
    return data


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


def default_branch_name(cwd: str | Path | None = None) -> str:
    data = gh_json(["repo", "view", "--json", "defaultBranchRef"], cwd=cwd)
    ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
    branch = ref.get("name") if isinstance(ref, dict) else None
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or re.search(r"[\x00-\x20~^:?*\\[]", branch)
    ):
        raise KernelError("unable to resolve a safe default branch")
    return branch


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
    args = ["issue", "view", str(number), "--json", "number,title,body,state,labels,assignees,url"]
    data = gh_json(args, cwd=cwd)
    if not isinstance(data, dict) or data.get("number") != number:
        raise KernelError(f"issue #{number} is unavailable")
    return data


def list_issues(*, state: str = "open", label: str | None = None, cwd: str | Path | None = None) -> list[dict[str, Any]]:
    # gh routes --label through the search index, which lags fresh labels; filter locally.
    args = ["issue", "list", "--state", state, "--limit", "1000", "--json", "number,title,body,state,labels,assignees,url"]
    data = gh_json(args, cwd=cwd)
    if not isinstance(data, list) or len(data) >= 1000:
        raise KernelError("GitHub returned malformed or truncated issue inventory")
    return [record for record in data if label is None or label in label_names(record)]


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
    try:
        return _parse_touches(body)
    except TouchesError as exc:
        raise KernelError(str(exc)) from exc


def safe_declared_path(value: str) -> bool:
    return _safe_declared_path(value)


def path_allowed(path: str, declared: Iterable[str]) -> bool:
    return _path_allowed(path, declared)


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
    declarations = [line for line in (body or "").splitlines() if re.match(r"(?i)^\s*depends-on\b", line)]
    matches = [re.fullmatch(r"depends-on: #([1-9]\d*)", line) for line in declarations]
    if any(match is None for match in matches):
        raise KernelError("depends-on declarations must each match 'depends-on: #N'")
    return sorted({int(match.group(1)) for match in matches if match is not None})

def contract_errors(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    items = acceptance_items(str(record.get("body") or ""))
    if not items or not any(not done for done, _ in items):
        errors.append("Acceptance Criteria must contain an unchecked item")
    try:
        parse_touches(str(record.get("body") or ""))
    except KernelError as exc:
        errors.append(str(exc))
    try:
        dependencies(str(record.get("body") or ""))
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

def ensure_label(name: str, *, color: str = "5319e7", description: str = "", cwd: str | Path | None = None) -> None:
    run(["gh", "label", "create", name, "--color", color, "--description", description, "--force"], cwd=cwd)


def linked_project(*, cwd: str | Path | None = None, refresh: bool = False) -> dict[str, Any]:
    slug = repo_slug(cwd)
    requested = os.environ.get("ARU_PROJECT_NUMBER") or ""
    key = (slug, requested)
    cached = _LINKED_PROJECT_CACHE.get(key)
    if cached is not None and not refresh:
        return dict(cached)
    if refresh:
        _LINKED_PROJECT_CACHE.pop(key, None)
    owner, name = slug.split("/", 1)
    query = (
        "query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){ "
        "projectsV2(first:20){ nodes{id number title closed} pageInfo{hasNextPage} } } }"
    )
    data = gh_json(
        ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}"],
        cwd=cwd,
        auth=PROJECT_AUTH,
    )
    if not isinstance(data, dict) or data.get("errors"):
        raise KernelError("linked Project Board query returned a GraphQL error")
    root = data.get("data")
    repository = root.get("repository") if isinstance(root, dict) else None
    connection = repository.get("projectsV2") if isinstance(repository, dict) else None
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise KernelError("linked Project Board is unavailable")
    if page_info.get("hasNextPage") is not False:
        raise KernelError("linked Project Board inventory is truncated")
    for node in nodes:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str) or not node["id"]
            or type(node.get("number")) is not int or node["number"] <= 0
            or not isinstance(node.get("title"), str) or not node["title"]
            or type(node.get("closed")) is not bool
        ):
            raise KernelError("linked Project Board inventory is malformed")
    open_projects = [node for node in nodes if not node["closed"]]
    if requested:
        open_projects = [node for node in open_projects if str(node["number"]) == requested]
    if len(open_projects) != 1:
        raise KernelError("expected exactly one linked open Project Board")
    _LINKED_PROJECT_CACHE[key] = dict(open_projects[0])
    return dict(open_projects[0])


def _project_card_identity(
    number: int, *, cwd: str | Path | None = None, refresh_project: bool = False,
) -> tuple[str, str]:
    project = linked_project(cwd=cwd, refresh=True) if refresh_project else linked_project(cwd=cwd)
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise KernelError("linked Project Board identity is unavailable")
    slug = repo_slug(cwd)
    issue_record = gh_json(["api", f"repos/{slug}/issues/{number}"], cwd=cwd)
    if (
        not isinstance(issue_record, dict)
        or issue_record.get("number") != number
        or "pull_request" in issue_record
        or not isinstance(issue_record.get("node_id"), str)
        or not issue_record["node_id"]
    ):
        raise KernelError(f"issue #{number} Project identity is unavailable")
    return project_id, issue_record["node_id"]


_PROJECT_CARD_QUERY = (
    "query($issue:ID!,$project:ID!){ issueNode:node(id:$issue){ ... on Issue{ "
    "projectItems(first:20){ nodes{ id project{id} fieldValueByName(name:\"Status\"){ "
    "... on ProjectV2ItemFieldSingleSelectValue{name} } } pageInfo{hasNextPage} } } } "
    "projectNode:node(id:$project){ ... on ProjectV2{ field(name:\"Status\"){ "
    "... on ProjectV2SingleSelectField{ id name options{id name} } } } } }"
)


def _project_card_snapshot(
    number: int, *, cwd: str | Path | None = None, refresh_project: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    project_id, node_id = _project_card_identity(number, cwd=cwd, refresh_project=refresh_project)
    data = gh_json(
        ["api", "graphql", "-f", f"query={_PROJECT_CARD_QUERY}", "-F", f"issue={node_id}", "-F", f"project={project_id}"],
        cwd=cwd,
        auth=PROJECT_AUTH,
    )
    if not isinstance(data, dict) or data.get("errors"):
        raise KernelError("Project Board card snapshot returned a GraphQL error")
    root = data.get("data")
    issue_node = root.get("issueNode") if isinstance(root, dict) else None
    project_node = root.get("projectNode") if isinstance(root, dict) else None
    connection = issue_node.get("projectItems") if isinstance(issue_node, dict) else None
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise KernelError("Project Board item inventory is malformed")
    if page_info.get("hasNextPage") is not False:
        raise KernelError("Project Board item inventory is truncated")
    matches: list[dict[str, Any]] = []
    for item in nodes:
        item_project = item.get("project") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(item_project, dict)
            or not isinstance(item_project.get("id"), str) or not item_project["id"]
        ):
            raise KernelError("Project Board item inventory is malformed")
        if item_project["id"] == project_id:
            matches.append(item)
    if not matches:
        raise KernelError(f"issue #{number} is not a member of the linked Project Board")
    if len(matches) != 1:
        raise KernelError(f"issue #{number} Project Board card is ambiguous")
    status_field = project_node.get("field") if isinstance(project_node, dict) else None
    return project_id, matches[0], status_field if isinstance(status_field, dict) else None


def _validated_status_field(status_field: Any) -> dict[str, Any]:
    if (
        not isinstance(status_field, dict)
        or status_field.get("name") != "Status"
        or not isinstance(status_field.get("id"), str)
        or not status_field["id"]
        or not isinstance(status_field.get("options"), list)
    ):
        raise KernelError("issue or Status field is ambiguous on the linked Project Board")
    options = status_field["options"]
    if any(
        not isinstance(opt, dict)
        or not isinstance(opt.get("id"), str)
        or not opt["id"]
        or not isinstance(opt.get("name"), str)
        for opt in options
    ):
        raise KernelError("Project Board Status options are malformed")
    option_ids = [opt["id"] for opt in options]
    option_names = [opt["name"] for opt in options]
    if len(option_ids) != len(set(option_ids)) or len(option_names) != len(set(option_names)):
        raise KernelError("Project Board Status options are malformed")
    return status_field


def _item_status(item: dict[str, Any], status_field: Any) -> str | None:
    valid_field = _validated_status_field(status_field)
    field_value = item.get("fieldValueByName")
    if field_value is None:
        return None
    if not isinstance(field_value, dict) or not isinstance(field_value.get("name"), str):
        raise KernelError("Project Board Status field value is malformed")
    name = field_value["name"]
    matches = [opt for opt in valid_field["options"] if opt["name"] == name]
    if len(matches) != 1:
        raise KernelError("Project Board Status field value is malformed")
    return name


def project_item_status(number: int, *, cwd: str | Path | None = None) -> str | None:
    _project_id, item, status_field = _project_card_snapshot(number, cwd=cwd)
    return _item_status(item, status_field)


def project_item_evidence(number: int, *, cwd: str | Path | None = None) -> dict[str, Any]:
    """Read fresh linked-Project and card identity for authorization comparisons."""
    project_id, item, status_field = _project_card_snapshot(number, cwd=cwd, refresh_project=True)
    return {
        "project_id": project_id,
        "item_id": item["id"],
        "status_field_id": _validated_status_field(status_field)["id"],
        "status": _item_status(item, status_field),
    }


def board_edit(
    number: int,
    status: str,
    *,
    expected_current: str | None = None,
    cwd: str | Path | None = None,
) -> list[str]:
    try:
        project_id, item, status_field = _project_card_snapshot(number, cwd=cwd)
        valid_field = _validated_status_field(status_field)
        if expected_current is not None:
            current_status = _item_status(item, valid_field)
            if current_status != expected_current:
                raise StatusPreconditionError(
                    f"issue #{number} Project card status ({current_status!r}) "
                    f"does not equal expected {expected_current!r}"
                )
        options = [option for option in valid_field["options"] if option["name"] == status]
        if len(options) != 1:
            raise KernelError(f"Project Board has no unique {status!r} option")
    except StatusPreconditionError:
        raise
    except KernelError as exc:
        if expected_current is not None:
            raise StatusPreconditionError(str(exc)) from exc
        raise
    return [
        "project", "item-edit",
        "--id", str(item["id"]),
        "--project-id", project_id,
        "--field-id", str(valid_field["id"]),
        "--single-select-option-id", str(options[0]["id"]),
    ]


def _preflight_status_transition(
    number: int,
    status: str,
    expected_current: str | None,
    cwd: str | Path | None,
) -> tuple[str | None, list[str] | None]:
    try:
        record = issue(number, cwd=cwd)
        current = status_of(record)
        if expected_current is not None:
            project_status = project_item_status(number, cwd=cwd)
            if current != expected_current or project_status != expected_current:
                raise StatusPreconditionError(
                    f"issue #{number} status ({current!r}) and Project card status "
                    f"({project_status!r}) must both equal expected {expected_current!r}"
                )
        if current == status:
            board_edit(number, status, expected_current=status, cwd=cwd)
            return current, None
        edit = board_edit(number, status, expected_current=expected_current, cwd=cwd)
        final_current = status_of(issue(number, cwd=cwd))
        if expected_current is not None and final_current != expected_current:
            raise StatusPreconditionError(
                f"issue #{number} status ({final_current!r}) does not equal expected {expected_current!r}"
            )
        if final_current == status:
            board_edit(number, status, expected_current=status, cwd=cwd)
            return final_current, None
        return final_current, edit
    except StatusPreconditionError:
        raise
    except KernelError as exc:
        if expected_current is not None:
            raise StatusPreconditionError(str(exc)) from exc
        raise


def set_status(
    number: int,
    status: str,
    *,
    expected_current: str | None = None,
    pre_mutation_check: Callable[[], None] | None = None,
    cwd: str | Path | None = None,
) -> None:
    current, edit = _preflight_status_transition(number, status, expected_current, cwd)
    if edit is None:
        return
    if pre_mutation_check is not None:
        pre_mutation_check()
    target = status_label(status)
    # Create the target label only after pre_mutation_check clears; under
    # expected_current a label failure stays a precondition failure (zero-rollback).
    try:
        ensure_label(target, color="1d76db", description=f"Board status: {status}", cwd=cwd)
    except KernelError as exc:
        if not isinstance(exc, StatusPreconditionError) and expected_current is not None:
            raise StatusPreconditionError(str(exc)) from exc
        raise
    args = ["gh", "issue", "edit", str(number), "--add-label", target]
    if current:
        args.extend(["--remove-label", status_label(current)])
    run(args, cwd=cwd)
    try:
        run(["gh", *edit], cwd=cwd)
    except KernelError as item_error:
        rollback = ["gh", "issue", "edit", str(number), "--remove-label", target]
        if current:
            rollback.extend(["--add-label", status_label(current)])
        try:
            run(rollback, cwd=cwd)
        except KernelError as rollback_error:
            raise KernelError(
                f"{rollback_error}; original item-edit failure: {item_error}"
            ) from rollback_error
        raise
    settled_issue = status_of(issue(number, cwd=cwd))
    settled_project = project_item_status(number, cwd=cwd)
    if settled_issue != status or settled_project != status:
        raise KernelError(
            f"issue #{number} status transition did not settle: issue "
            f"{settled_issue!r}, Project card {settled_project!r}, expected "
            f"{status!r}; stop and reconcile GitHub authority"
        )


def json_print(data: Any) -> None:
    print(json.dumps(data, sort_keys=True))
