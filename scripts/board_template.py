#!/usr/bin/env python3
"""The declared Project every governed board is copied from.

A board layout is presentation, never a gate: nothing here refuses a merge. What
makes it reliable instead is that ``init_project.provision_github()`` is the only
path that creates a governed Project, so a repository cannot skip the template.
"""

from __future__ import annotations

import json
from pathlib import Path

import policy
from common import PROJECT_AUTH, KernelError, run

STATUSES = ("Backlog", "Ready", "In Progress", "In Review", "Done")

PROJECT_QUERY = """
query($owner:String!,$number:Int!){user(login:$owner){projectV2(number:$number){
  id views(first:50){nodes{name layout}}
  fields(first:50){nodes{... on ProjectV2SingleSelectField{name options{name}}}}}}}
"""

LINKED_QUERY = """
query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
  projectsV2(first:20){nodes{number title}}}}
"""

CREATE_VIEW = """
mutation($project:ID!,$name:String!,$layout:ProjectV2ViewLayout!){
  createProjectV2View(input:{projectId:$project,name:$name,layout:$layout}){
    projectV2View{id}}}
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
        {"name": node.get("name"), "layout": node.get("layout")}
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
        _graphql(CREATE_VIEW, directory, project=project_id, name=name, layout=layout)
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
