#!/usr/bin/env python3
"""The declared Project every governed board is copied from.

A board layout is presentation, never a gate: nothing here refuses a merge. What
makes it reliable instead is that ``init_project.provision_github()`` is the only
path that creates a governed Project, so a repository cannot skip the template.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import policy
from common import PROJECT_AUTH, KernelError, run

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")

PROJECT_QUERY = """
query($owner:String!,$number:Int!){user(login:$owner){projectV2(number:$number){
  id views(first:50){nodes{id name layout filter}}
  fields(first:50){nodes{... on ProjectV2SingleSelectField{name options{name}}}}}}}
"""

LINKED_QUERY = """
query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
  projectsV2(first:20){nodes{id number title closed}}}}
"""

CREATE_VIEW = """
mutation($project:ID!,$name:String!,$layout:ProjectV2ViewLayout!){
  createProjectV2View(input:{projectId:$project,name:$name,layout:$layout}){
    projectV2View{id}}}
"""

UPDATE_VIEW = """
mutation($view:ID!,$filter:String!){
  updateProjectV2View(input:{viewId:$view,filter:$filter}){
    projectV2View{id}}}
"""

ADD_ITEM = """
mutation($project:ID!,$content:ID!){
  addProjectV2ItemById(input:{projectId:$project,contentId:$content}){
    item{id}}}
"""


def _graphql(query: str, directory: Path, **variables: object) -> dict:
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        argv += ["-F", f"{key}={value}"]
    result = run(argv, cwd=directory, auth=PROJECT_AUTH)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KernelError("GitHub returned malformed JSON for a Project query") from exc
    if not isinstance(payload, dict):
        raise KernelError("GitHub returned malformed JSON for a Project query")
    if payload.get("errors"):
        messages = "; ".join(
            err.get("message", "unknown error")
            for err in payload["errors"]
            if isinstance(err, dict)
        )
        raise KernelError(f"GitHub GraphQL error: {messages}")
    return payload


def declared() -> tuple[str, int]:
    """The declared template as ``(owner, number)``.

    Fails closed: a missing or malformed declaration refuses provisioning rather
    than falling back to a bare board.
    """
    section = policy.load().get("board_template")
    if not isinstance(section, dict):
        raise KernelError("[board_template] is missing from the policy")
    owner, number = section.get("owner"), section.get("number")
    if not isinstance(owner, str) or not owner.strip():
        raise KernelError("[board_template] needs a non-empty owner")
    # bool is an int subclass; `number = true` must not read as project 1.
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise KernelError("[board_template] needs a positive project number")
    return owner.strip(), number


def state(owner: str, number: int, directory: Path) -> dict[str, object]:
    """Id, views and Status options of one Project; refuses an unreadable one."""
    payload = _graphql(PROJECT_QUERY, directory, owner=owner, number=number)
    project = ((payload.get("data") or {}).get("user") or {}).get("projectV2")
    if not isinstance(project, dict) or not project.get("id"):
        raise KernelError(f"project {owner}/{number} is missing or unreadable")
    views = [
        {"name": node.get("name"), "layout": node.get("layout"), "filter": node.get("filter")}
        for node in ((project.get("views") or {}).get("nodes") or [])
        if isinstance(node, dict) and node.get("name")
    ]
    status = [
        [option.get("name") for option in (node.get("options") or [])]
        for node in ((project.get("fields") or {}).get("nodes") or [])
        if isinstance(node, dict) and node.get("name") == "Status"
    ]
    if len(status) != 1:
        raise KernelError(f"project {owner}/{number} has no unique Status field")
    return {"id": project["id"], "views": views, "statuses": tuple(status[0])}


def assert_statuses(where: str, statuses: tuple[str, ...]) -> None:
    """Asserted, never rewritten: a drifted template must surface once here
    rather than propagate a status nothing reads into every repository."""
    if tuple(statuses) != STATUSES:
        raise KernelError(
            f"{where} declares statuses {list(statuses)}; the kernel reads exactly {list(STATUSES)}"
        )


def linked_project(slug: str, directory: Path) -> int:
    """The number of the one Project linked to ``slug``."""
    owner, _, name = slug.partition("/")
    payload = _graphql(LINKED_QUERY, directory, owner=owner, name=name)
    repository = (payload.get("data") or {}).get("repository")
    nodes = ((repository or {}).get("projectsV2") or {}).get("nodes") or []
    numbers = [node.get("number") for node in nodes if isinstance(node, dict)]
    if len(numbers) != 1:
        raise KernelError(f"{slug} must have exactly one linked Project, found {len(numbers)}")
    return int(numbers[0])


def linked_project_id(slug: str, directory: Path) -> str | None:
    """The ID of the one open Project linked to ``slug``, or None if absent."""
    owner, _, name = slug.partition("/")
    payload = _graphql(LINKED_QUERY, directory, owner=owner, name=name)
    repository = (payload.get("data") or {}).get("repository")
    nodes = ((repository or {}).get("projectsV2") or {}).get("nodes") or []
    open_nodes = [
        node for node in nodes
        if isinstance(node, dict) and not node.get("closed", False)
    ]
    if not open_nodes:
        return None
    if len(open_nodes) > 1:
        raise KernelError(f"{slug} has multiple linked open Projects, found {len(open_nodes)}")
    project_id = open_nodes[0].get("id")
    if not isinstance(project_id, str) or not project_id:
        raise KernelError(f"{slug} linked Project has no readable id")
    return project_id


def add_item(project_id: str, content_id: str, directory: Path) -> str:
    """Add an issue or pull request to a Project; returns the item ID."""
    payload = _graphql(ADD_ITEM, directory, project=project_id, content=content_id)
    item = ((payload.get("data") or {}).get("addProjectV2ItemById") or {}).get("item")
    if not isinstance(item, dict) or not item.get("id"):
        raise KernelError("addProjectV2ItemById returned no item ID")
    return str(item["id"])


def add_pr_to_board(slug: str, pr_node_id: str, directory: Path) -> str | None:
    """Add a pull request to the repository's linked Project board.

    Returns the item ID, or None if no Project is linked or adding failed.
    A failure to add is reported to stderr and never refuses the caller.
    """
    try:
        project_id = linked_project_id(slug, directory)
    except KernelError as exc:
        sys.stderr.write(f"note: could not read linked Project for {slug}: {exc}\n")
        return None
    if project_id is None:
        sys.stderr.write(f"note: {slug} has no linked Project\n")
        return None
    try:
        return add_item(project_id, pr_node_id, directory)
    except KernelError as exc:
        sys.stderr.write(f"note: could not add PR to Project Board: {exc}\n")
        return None


def missing_views(
    present: list[dict[str, object]], template_views: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Template views absent from ``present``, in the template's own order."""
    have = {view.get("name") for view in present}
    return [view for view in template_views if view.get("name") not in have]


def add_views(
    project_id: str, views: list[dict[str, object]], directory: Path
) -> list[str]:
    """Create ``views`` on a Project; returns the names actually created.

    Additive by construction: nothing is renamed or deleted, so bringing a board
    up to the template can never destroy a layout someone is using.
    """
    added = []
    for view in views:
        name, layout = view.get("name"), view.get("layout")
        if not isinstance(name, str) or not isinstance(layout, str):
            continue
        created = _graphql(CREATE_VIEW, directory, project=project_id, name=name, layout=layout)
        view_id = (
            ((created.get("data") or {}).get("createProjectV2View") or {})
            .get("projectV2View", {})
            .get("id")
        )
        view_filter = view.get("filter")
        if view_id and isinstance(view_filter, str) and view_filter:
            _graphql(UPDATE_VIEW, directory, view=view_id, filter=view_filter)
        added.append(name)
    return added


def sync(slug: str, directory: Path, *, check: bool = False) -> dict[str, object]:
    """Bring the board linked to ``slug`` up to the declared template.

    Does not recreate the Project: the existing board keeps its items, its id and
    any view added by hand. With ``check`` it reports the difference and writes
    nothing.
    """
    owner, number = declared()
    template = state(owner, number, directory)
    assert_statuses(f"board template {owner}/{number}", template["statuses"])
    target_number = linked_project(slug, directory)
    target_owner = slug.partition("/")[0]
    target = state(target_owner, target_number, directory)
    assert_statuses(f"board {target_owner}/{target_number}", target["statuses"])
    pending = missing_views(target["views"], template["views"])
    added = [] if check else add_views(target["id"], pending, directory)
    return {
        "repository": slug,
        "template": f"{owner}/{number}",
        "project": f"{target_owner}/{target_number}",
        "missing": [view["name"] for view in pending],
        "added": added,
        "checked": check,
    }
