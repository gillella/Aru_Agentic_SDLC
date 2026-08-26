"""Complete governed-board inventory for picker auto-triage."""

import json

from common import (
    get_repo_projects,
    run_cmd,
    select_governed_projects,
)


def stage_expected_ready_for_triage(
    issues: list[dict], expected_ready_issue: int | None,
) -> list[dict] | None:
    """Treat one expected Ready issue as Backlog for post-write requalification."""
    ready_numbers = {
        issue["number"]
        for issue in issues
        if "status:ready" in {
            str(label.get("name") or "").lower()
            for label in issue.get("labels", [])
        }
    }
    expected = {expected_ready_issue} if expected_ready_issue is not None else set()
    if ready_numbers != expected:
        return None
    if expected_ready_issue is None:
        return issues
    staged_issues = []
    for issue in issues:
        if issue["number"] != expected_ready_issue:
            staged_issues.append(issue)
            continue
        status_labels = [
            label for label in issue.get("labels", [])
            if str(label.get("name") or "").lower().startswith("status:")
        ]
        if [str(label.get("name") or "").lower() for label in status_labels] != [
            "status:ready"
        ]:
            return None
        staged = dict(issue)
        staged["labels"] = [
            label for label in issue.get("labels", []) if label not in status_labels
        ] + [{"name": "status:backlog"}]
        staged_issues.append(staged)
    return staged_issues


_OMITTED = object()


def _board_items(owner: str, number: int) -> list | None:
    """Read one complete board page, or ``None`` when it cannot be trusted.

    A saturated page is indistinguishable from a truncated one, and a
    ``totalCount`` that disagrees with the rows is a partial answer, so both
    refuse rather than report a short board as the whole board.
    """
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
    return items


def governed_board_inventory(
    repo_slug: str,
    open_numbers: set[int],
    *,
    projects: list[dict] | None | object = _OMITTED,
    require_complete: bool = True,
) -> tuple[dict[int, str], int] | None:
    """Return open-issue statuses and the board's literal Ready count.

    Callers that already loaded repository project metadata can pass it so a
    cycle does not repeat the same Projects query.  ``require_complete=False``
    is for status diagnostics: missing open issues are then reported as board
    orphans instead of making the single inventory unreadable.
    """
    available = get_repo_projects(repo_slug) if projects is _OMITTED else projects
    if available is None:
        return None
    governed = select_governed_projects(available, repo_slug)
    if len(governed) != 1:
        return None
    project = governed[0]
    owner = (project.get("owner") or {}).get("login")
    number = project.get("number")
    if not owner or not isinstance(number, int):
        return None
    items = _board_items(owner, number)
    if items is None:
        return None
    statuses: dict[int, str] = {}
    ready_count = 0
    for item in items:
        # A malformed row is unusable evidence, not an absent one: callers
        # promise a fail-closed None here, and reading fields off a non-object
        # would raise past every guard below instead.
        if not isinstance(item, dict):
            return None
        status = item.get("status")
        if isinstance(status, str) and status.lower() == "ready":
            ready_count += 1
        content = item.get("content") or {}
        if not isinstance(content, dict):
            return None
        issue_number = content.get("number")
        repository = content.get("repository") or item.get("repository")
        # A non-integer number is skipped rather than indexed: draft items
        # legitimately carry none, and the completeness check below still
        # refuses the snapshot if a real open issue went missing this way.
        if (not isinstance(issue_number, int) or issue_number not in open_numbers
                or repository != repo_slug):
            continue
        if issue_number in statuses or not isinstance(status, str) or not status:
            return None
        statuses[issue_number] = status
    if require_complete and set(statuses) != open_numbers:
        return None
    return statuses, ready_count
