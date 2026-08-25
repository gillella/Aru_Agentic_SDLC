"""Complete governed-board inventory for picker auto-triage."""

import json

from common import (
    get_repo_projects,
    run_cmd,
    select_governed_projects,
)


def governed_board_inventory(
    repo_slug: str, open_numbers: set[int],
) -> tuple[dict[int, str], int] | None:
    """Return complete open-issue statuses and the board's literal Ready count."""
    governed = select_governed_projects(get_repo_projects(repo_slug) or [], repo_slug)
    if len(governed) != 1:
        return None
    project = governed[0]
    owner = (project.get("owner") or {}).get("login")
    number = project.get("number")
    if not owner or not isinstance(number, int):
        return None
    code, stdout, _stderr = run_cmd([
        "gh", "project", "item-list", str(number), "--owner", owner,
        "--limit", "1000", "--format", "json",
    ], check=False)
    if code != 0:
        return None
    try:
        payload = json.loads(stdout)
        items, total = payload["items"], payload["totalCount"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(items, list) or total != len(items) or len(items) >= 1000:
        return None
    statuses: dict[int, str] = {}
    ready_count = 0
    for item in items:
        status = item.get("status")
        if isinstance(status, str) and status.lower() == "ready":
            ready_count += 1
        content = item.get("content") or {}
        issue_number = content.get("number")
        repository = content.get("repository") or item.get("repository")
        if issue_number not in open_numbers or repository != repo_slug:
            continue
        if issue_number in statuses or not isinstance(status, str) or not status:
            return None
        statuses[issue_number] = status
    return (statuses, ready_count) if set(statuses) == open_numbers else None
