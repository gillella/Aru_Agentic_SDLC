#!/usr/bin/env python3
"""Validate and publish an approved PRD decomposition as governed issues."""

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from common import get_repo_slug, paths_overlap, run_cmd


class PlanError(ValueError):
    """The decomposition cannot be published safely."""


class PublicationError(RuntimeError):
    """Publication stopped after a GitHub lifecycle mutation failed."""


@dataclass(frozen=True)
class PreparedIssue:
    key: str
    title: str
    summary: str
    issue_type: str
    priority: str
    touches: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[dict[str, str], ...]
    decision_boundaries: tuple[str, ...]
    non_goals: tuple[str, ...]
    verification: tuple[str, ...]
    epic: str | None
    phase: str | None
    parallel_eligible: bool = False


@dataclass(frozen=True)
class PreparedPlan:
    source_prd: int
    epics: tuple[dict[str, str], ...]
    issues: tuple[PreparedIssue, ...]


KEY_RE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
TYPE_RE = re.compile(r"(?:feat|fix|chore|docs)\Z")
PRIORITY_RE = re.compile(r"p[0-3]\Z")
GLOB_CHARS = frozenset("*?[")
VERIFY_PLACEHOLDERS = frozenset({
    "command", "commands", "cmd", "n/a", "na", "none", "null",
    "placeholder", "test", "test command", "tbd", "todo", "...",
})


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise PlanError(f"{field} must be a single line")
    return value.strip()


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = "list" if allow_empty else "non-empty list"
        raise PlanError(f"{field} must be a {suffix} of strings")
    result = tuple(_required_string(item, f"{field}[]") for item in value)
    return result


def _normalize_repo_path(raw: Any, field: str) -> str:
    value = _required_string(raw, field).replace("\\", "/")
    if value.startswith("/"):
        raise PlanError(f"{field} is not a safe repository-relative path: {raw!r}")
    value = value.rstrip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PlanError(f"{field} is not a safe repository-relative path: {raw!r}")
    return str(path)


def resolve_repo_root(raw_root: str) -> Path:
    root = Path(raw_root).expanduser().resolve()
    code, out, err = run_cmd(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
    )
    if code != 0:
        raise PlanError(f"repository root is not a Git worktree: {err or out}")
    if Path(out).resolve() != root:
        raise PlanError(f"repository root must equal git --show-toplevel: {root}")
    return root


def repository_inventory(repo_root: Path) -> tuple[str, ...]:
    code, out, err = run_cmd(
        ["git", "-C", str(repo_root), "ls-files"],
        check=False,
    )
    if code != 0:
        raise PlanError(f"could not inspect repository files: {err or out}")
    paths = tuple(sorted(line.strip() for line in out.splitlines() if line.strip()))
    if not paths:
        raise PlanError("repository inventory is empty")
    return paths


def derive_touches(
    change_targets: Any,
    repo_root: Path,
    inventory: tuple[str, ...],
    *,
    issue_key: str,
) -> tuple[str, ...]:
    """Derive canonical touches from structured, repository-checked evidence."""
    if not isinstance(change_targets, list) or not change_targets:
        raise PlanError(f"issue {issue_key}: change_targets must be a non-empty list")

    inventory_set = set(inventory)
    touches: list[str] = []
    for index, target in enumerate(change_targets):
        field = f"issue {issue_key}: change_targets[{index}]"
        if not isinstance(target, dict):
            raise PlanError(f"{field} must be an object")
        path = _normalize_repo_path(target.get("path"), f"{field}.path")
        kind = target.get("kind")
        if kind not in {"existing", "new"}:
            raise PlanError(f"{field}.kind must be 'existing' or 'new'")

        if kind == "new":
            if any(char in path for char in GLOB_CHARS):
                raise PlanError(f"{field}: new paths cannot contain glob syntax: {path}")
            if path in inventory_set:
                raise PlanError(f"{field}: path already exists and cannot be marked new: {path}")
            if any(candidate.startswith(path + "/") for candidate in inventory):
                raise PlanError(
                    f"{field}: path is an existing directory and cannot be marked new: {path}"
                )
            parent = PurePosixPath(path).parent
            parent_text = "" if str(parent) == "." else str(parent)
            ancestor_known = not parent_text
            ancestor = parent
            while not ancestor_known and str(ancestor) != ".":
                ancestor_text = str(ancestor)
                ancestor_known = (repo_root / ancestor_text).is_dir() and any(
                    candidate.startswith(ancestor_text + "/") for candidate in inventory
                )
                ancestor = ancestor.parent
            if not ancestor_known:
                raise PlanError(f"{field}: parent directory is not in the repository: {parent_text}")
            canonical_paths = [path]
        elif any(char in path for char in GLOB_CHARS):
            canonical_paths = [
                candidate for candidate in inventory if fnmatch.fnmatch(candidate, path)
            ]
            if not canonical_paths:
                raise PlanError(f"{field}: glob matches no tracked repository path: {path}")
        elif path in inventory_set:
            canonical_paths = [path]
        elif any(candidate.startswith(path + "/") for candidate in inventory):
            canonical_paths = [
                candidate for candidate in inventory if candidate.startswith(path + "/")
            ]
        else:
            raise PlanError(f"{field}: path is not present in the repository: {path}")

        for canonical in canonical_paths:
            if canonical not in touches:
                touches.append(canonical)
    return tuple(touches)


def _acceptance_criteria(value: Any, issue_key: str) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"issue {issue_key}: acceptance_criteria must be a non-empty list")
    result = []
    for index, criterion in enumerate(value):
        if not isinstance(criterion, dict):
            raise PlanError(f"issue {issue_key}: acceptance_criteria[{index}] must be an object")
        result.append({
            "predicate": _required_string(
                criterion.get("predicate"),
                f"issue {issue_key}: acceptance_criteria[{index}].predicate",
            ),
            "verify": _required_string(
                criterion.get("verify"),
                f"issue {issue_key}: acceptance_criteria[{index}].verify",
            ),
        })
        if result[-1]["verify"].strip("`'\" ").lower() in VERIFY_PLACEHOLDERS:
            raise PlanError(
                f"issue {issue_key}: acceptance_criteria[{index}].verify is a placeholder"
            )
        if "`" in result[-1]["verify"]:
            raise PlanError(
                f"issue {issue_key}: acceptance_criteria[{index}].verify cannot contain backticks"
            )
    return tuple(result)


def _find_cycle_edge(issues: dict[str, PreparedIssue]) -> tuple[str, str] | None:
    state: dict[str, int] = {}

    def visit(key: str) -> tuple[str, str] | None:
        state[key] = 1
        for dependency in issues[key].depends_on:
            if state.get(dependency) == 1:
                return key, dependency
            if state.get(dependency, 0) == 0:
                edge = visit(dependency)
                if edge:
                    return edge
        state[key] = 2
        return None

    for key in issues:
        if state.get(key, 0) == 0:
            edge = visit(key)
            if edge:
                return edge
    return None


def topological_order(issues: dict[str, PreparedIssue]) -> tuple[str, ...]:
    indegree = {key: len(issue.depends_on) for key, issue in issues.items()}
    dependents = {key: [] for key in issues}
    for key, issue in issues.items():
        for dependency in issue.depends_on:
            dependents[dependency].append(key)

    ready = [key for key in issues if indegree[key] == 0]
    ordered: list[str] = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for dependent in dependents[key]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(issues):
        edge = _find_cycle_edge(issues)
        detail = f" at edge {edge[0]} -> {edge[1]}" if edge else ""
        raise PlanError(f"dependency cycle detected{detail}")
    return tuple(ordered)


def _has_overlap(issue: PreparedIssue, others: dict[str, PreparedIssue]) -> bool:
    for other_key, other in others.items():
        if other_key == issue.key:
            continue
        if any(paths_overlap(left, right) for left in issue.touches for right in other.touches):
            return True
    return False


def prepare_plan(manifest: Any, repo_root: Path, inventory: tuple[str, ...]) -> PreparedPlan:
    if not isinstance(manifest, dict):
        raise PlanError("plan root must be a JSON object")
    source_prd = manifest.get("source_prd")
    if not isinstance(source_prd, int) or isinstance(source_prd, bool) or source_prd <= 0:
        raise PlanError("source_prd must be a positive GitHub issue number")

    raw_epics = manifest.get("epics", [])
    if not isinstance(raw_epics, list):
        raise PlanError("epics must be a list")
    epic_keys: set[str] = set()
    epics: list[dict[str, str]] = []
    for index, epic in enumerate(raw_epics):
        if not isinstance(epic, dict):
            raise PlanError(f"epics[{index}] must be an object")
        key = _required_string(epic.get("key"), f"epics[{index}].key")
        if not KEY_RE.fullmatch(key) or key in epic_keys:
            raise PlanError(f"epics[{index}].key is invalid or duplicated: {key}")
        epic_keys.add(key)
        title = _required_string(epic.get("title"), f"epics[{index}].title")
        if not title.lower().startswith("epic:"):
            raise PlanError(f"epics[{index}].title must start with 'epic:'")
        epics.append({
            "key": key,
            "title": title,
            "summary": _required_string(epic.get("summary"), f"epics[{index}].summary"),
            "phase": _required_string(epic.get("phase"), f"epics[{index}].phase"),
        })

    raw_issues = manifest.get("issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        raise PlanError("issues must be a non-empty list")
    issues: dict[str, PreparedIssue] = {}
    for index, raw in enumerate(raw_issues):
        if not isinstance(raw, dict):
            raise PlanError(f"issues[{index}] must be an object")
        key = _required_string(raw.get("key"), f"issues[{index}].key")
        if not KEY_RE.fullmatch(key) or key in issues or key in epic_keys:
            raise PlanError(f"issues[{index}].key is invalid or duplicated: {key}")
        issue_type = _required_string(raw.get("type"), f"issue {key}: type")
        priority = _required_string(raw.get("priority", "p2"), f"issue {key}: priority")
        if not TYPE_RE.fullmatch(issue_type):
            raise PlanError(f"issue {key}: unsupported type: {issue_type}")
        if not PRIORITY_RE.fullmatch(priority):
            raise PlanError(f"issue {key}: unsupported priority: {priority}")
        epic = raw.get("epic")
        if epic is not None and epic not in epic_keys:
            raise PlanError(f"issue {key}: unknown epic key: {epic}")
        dependencies = _string_list(
            raw.get("depends_on", []), f"issue {key}: depends_on", allow_empty=True
        )
        if len(set(dependencies)) != len(dependencies):
            raise PlanError(f"issue {key}: duplicate dependency key")
        title = _required_string(raw.get("title"), f"issue {key}: title")
        if not title.lower().startswith(f"{issue_type}:"):
            raise PlanError(f"issue {key}: title must start with '{issue_type}:'")
        issues[key] = PreparedIssue(
            key=key,
            title=title,
            summary=_required_string(raw.get("summary"), f"issue {key}: summary"),
            issue_type=issue_type,
            priority=priority,
            touches=derive_touches(
                raw.get("change_targets"), repo_root, inventory, issue_key=key
            ),
            depends_on=dependencies,
            acceptance_criteria=_acceptance_criteria(raw.get("acceptance_criteria"), key),
            decision_boundaries=_string_list(
                raw.get("decision_boundaries"), f"issue {key}: decision_boundaries"
            ),
            non_goals=_string_list(raw.get("non_goals"), f"issue {key}: non_goals"),
            verification=_string_list(
                raw.get("verification"), f"issue {key}: verification"
            ),
            epic=epic,
            phase=(
                _required_string(raw.get("phase"), f"issue {key}: phase")
                if raw.get("phase") is not None
                else None
            ),
        )

    for key, issue in issues.items():
        for dependency in issue.depends_on:
            if dependency not in issues:
                raise PlanError(f"issue {key}: unknown dependency key: {dependency}")
            if dependency == key:
                raise PlanError(f"dependency cycle detected at edge {key} -> {dependency}")

    order = topological_order(issues)
    finalized = []
    for key in order:
        issue = issues[key]
        finalized.append(PreparedIssue(
            **{
                **issue.__dict__,
                "parallel_eligible": not issue.depends_on and not _has_overlap(issue, issues),
            }
        ))
    return PreparedPlan(source_prd, tuple(epics), tuple(finalized))


def render_epic_body(epic: dict[str, str], source_prd: int) -> str:
    return f"""## Scope

{epic['summary']}

## Source PRD

PRD: #{source_prd}

## Phase

{epic['phase']}

## Lifecycle

This phase epic is a non-claimable planning container. Its generated child issues carry implementation scope and enter Backlog independently.
"""


def render_issue_body(
    issue: PreparedIssue,
    source_prd: int,
    dependency_numbers: dict[str, int],
    epic_number: int | None,
) -> str:
    dependencies = ", ".join(f"#{dependency_numbers[key]}" for key in issue.depends_on)
    acceptance = "\n".join(
        f"- [ ] {item['predicate']} (verify: `{item['verify']}`)"
        for item in issue.acceptance_criteria
    )
    decisions = "\n".join(f"- {item}" for item in issue.decision_boundaries)
    non_goals = "\n".join(f"- {item}" for item in issue.non_goals)
    verification = "\n".join(f"- `{item}`" for item in issue.verification)
    epic = epic_number or source_prd
    phase = f"\nPhase: {issue.phase}" if issue.phase else ""
    return f"""## User Story

{issue.summary}

## Acceptance Criteria

{acceptance}

## Decision Boundaries

{decisions}

## Non-Goals

{non_goals}

## Verification

{verification}

## Dependencies

depends-on: {dependencies}
touches: {', '.join(issue.touches)}
parallel-eligible: {'true' if issue.parallel_eligible else 'false'}

PRD: #{source_prd}
Epic: #{epic}{phase}
"""


def validate_source_prd(issue_number: int, repo_slug: str) -> None:
    code, out, err = run_cmd(
        [
            "gh", "issue", "view", str(issue_number), "--repo", repo_slug,
            "--json", "body,labels,state",
        ],
        check=False,
    )
    if code != 0:
        raise PublicationError(f"could not verify source PRD #{issue_number}: {err or out}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise PublicationError(f"source PRD #{issue_number} returned invalid JSON") from exc
    labels = {
        item.get("name", "") for item in data.get("labels", []) if isinstance(item, dict)
    }
    body = (data.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
    if data.get("state") != "OPEN" or "type:epic" not in labels:
        raise PublicationError(f"source PRD #{issue_number} must be an open type:epic issue")
    if not re.search(
        r"^[ \t]*-[ \t]*Readiness:[ \t]*`?READY_FOR_PLANNING`?[ \t]*$",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        raise PublicationError(f"source PRD #{issue_number} is not READY_FOR_PLANNING")
    if not re.search(
        r"^[ \t]*-[ \t]*Operator approval:[ \t]*`?APPROVED`?[ \t]*$",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        raise PublicationError(f"source PRD #{issue_number} is not operator-approved")


def validate_publication_labels(plan: PreparedPlan, repo_slug: str) -> None:
    required = {"status:backlog"}
    if plan.epics:
        required.update({"type:epic", "priority:p1"})
    for issue in plan.issues:
        required.update({f"type:{issue.issue_type}", f"priority:{issue.priority}"})
    code, out, err = run_cmd(
        ["gh", "label", "list", "--repo", repo_slug, "--limit", "1000", "--json", "name"],
        check=False,
    )
    if code != 0:
        raise PublicationError(f"could not verify governance labels: {err or out}")
    try:
        data = json.loads(out)
        if not isinstance(data, list):
            raise TypeError("label result is not a list")
        available = {
            item.get("name", "") for item in data if isinstance(item, dict)
        }
    except (json.JSONDecodeError, TypeError) as exc:
        raise PublicationError("governance label query returned invalid JSON") from exc
    missing = sorted(required - available)
    if missing:
        raise PublicationError(f"required governance labels are missing: {', '.join(missing)}")


def _board_helper_path() -> Path:
    sdlc_home = Path(os.environ.get("ARU_SDLC_HOME", Path(__file__).resolve().parents[1]))
    helper = sdlc_home / "scripts" / "update_issue_status.py"
    if not helper.is_file():
        raise PublicationError(f"board-status helper not found: {helper}")
    return helper


def _create_issue(repo_slug: str, title: str, body: str, labels: str) -> int:
    code, out, err = run_cmd(
        [
            "gh", "issue", "create", "--repo", repo_slug,
            "--title", title, "--body", body, "--label", labels,
        ],
        check=False,
    )
    if code != 0:
        raise PublicationError(f"issue creation failed: {err or out}")
    match = re.search(r"/issues/(\d+)(?:\s*)\Z", out)
    if not match:
        raise PublicationError(f"could not capture created issue number from: {out!r}")
    return int(match.group(1))


def _attach_to_board(issue_number: int, repo_root: Path) -> None:
    command = [
        sys.executable,
        str(_board_helper_path()),
        "--issue", str(issue_number),
        "--status", "Backlog",
        "--require-board",
    ]
    code, out, err = run_cmd(command, check=False, cwd=str(repo_root))
    if code != 0:
        raise PublicationError(
            f"issue #{issue_number} was created but board attachment failed: {err or out}"
        )


def publish_plan(plan: PreparedPlan, repo_root: Path, repo_slug: str) -> dict[str, int]:
    _board_helper_path()
    validate_source_prd(plan.source_prd, repo_slug)
    validate_publication_labels(plan, repo_slug)
    created: dict[str, int] = {}
    try:
        for epic in plan.epics:
            number = _create_issue(
                repo_slug,
                epic["title"],
                render_epic_body(epic, plan.source_prd),
                "type:epic,priority:p1",
            )
            created[epic["key"]] = number
            _attach_to_board(number, repo_root)

        for issue in plan.issues:
            number = _create_issue(
                repo_slug,
                issue.title,
                render_issue_body(
                    issue,
                    plan.source_prd,
                    created,
                    created.get(issue.epic) if issue.epic else None,
                ),
                f"type:{issue.issue_type},priority:{issue.priority}",
            )
            created[issue.key] = number
            _attach_to_board(number, repo_root)
    except PublicationError as exc:
        completed = ", ".join(f"{key}=#{value}" for key, value in created.items()) or "none"
        raise PublicationError(f"{exc}; created before stop: {completed}") from exc
    return created


def dry_run_payload(plan: PreparedPlan) -> dict[str, Any]:
    ordered_keys = [epic["key"] for epic in plan.epics] + [issue.key for issue in plan.issues]
    preview_numbers = {key: 900000 + index for index, key in enumerate(ordered_keys, 1)}
    return {
        "source_prd": plan.source_prd,
        "creation_order": ordered_keys,
        "epics": list(plan.epics),
        "issues": [
            {
                "key": issue.key,
                "title": issue.title,
                "touches": list(issue.touches),
                "depends_on": list(issue.depends_on),
                "parallel_eligible": issue.parallel_eligible,
                "epic": issue.epic,
                "rendered_body": render_issue_body(
                    issue,
                    plan.source_prd,
                    preview_numbers,
                    preview_numbers.get(issue.epic) if issue.epic else None,
                ),
            }
            for issue in plan.issues
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and publish a PRD decomposition as governed Backlog issues."
    )
    parser.add_argument("--plan", required=True, help="Path to the JSON decomposition manifest")
    parser.add_argument("--repo-root", default=".", help="Governed repository root")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print without GitHub writes")
    args = parser.parse_args()

    try:
        repo_root = resolve_repo_root(args.repo_root)
        with Path(args.plan).open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        plan = prepare_plan(manifest, repo_root, repository_inventory(repo_root))
        if args.dry_run:
            print(json.dumps(dry_run_payload(plan), indent=2, sort_keys=True))
            return 0

        os.chdir(repo_root)
        repo_slug = get_repo_slug()
        if not repo_slug:
            raise PublicationError("could not resolve governed GitHub repository identity")
        created = publish_plan(plan, repo_root, repo_slug)
        print(json.dumps({"created": created}, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, PublicationError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
