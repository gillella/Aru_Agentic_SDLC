#!/usr/bin/env python3
# line-ceiling: 880
"""factory_loop_snapshot.py - deterministic read-only factory loop snapshot.

Emits a bounded, normalized read-only snapshot of live coordination state for
any governed repository root. Resolves repository identity and canonical
Project v2 board dynamically without hard-coded numbers or paths.

Never performs claims, label changes, board moves, branch creates, PR edits,
reviews, or merges. Exit codes: complete 0, error 1, waiting 2, blocked 3.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from common import (
    claimed_by,
    get_current_framework_version,
    get_framework_root,
    get_repo_projects,
    parse_touches,
    repository_owner_login,
    repository_trusted_logins,
    run_cmd,
    select_governed_projects,
    _redact_local_path,
)
from fetch_next_issue import (
    build_candidates,
    canonical_priority_display,
    is_epic,
    is_parallel_eligible,
    linked_issue_numbers_from_pr,
    needs_human,
    parse_dependencies,
    priority_rank,
)
from fetch_next_work import skill_for_issue
from fleet_status import _identity, list_worktrees, _worktree_rows
from github_inventory import (
    local_repo_slug,
    open_issues as rest_open_issues,
    open_pull_requests as rest_open_pull_requests,
    rich_open_pull_requests,
)
from picker_board_inventory import _board_items

SCHEMA_VERSION = "aru.factory_loop_snapshot.v1"

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_WAITING = 2
EXIT_BLOCKED = 3

REVIEW_SERVICE_LABELS = {
    "review:coderabbit": "coderabbit",
    "review:sourcery": "sourcery",
    "review:codeant": "codeant",
    "review:agent": "agent",
}


def _fail_closed(
    reason: str,
    summary: str,
    *,
    state: str = "error",
    code: int = EXIT_ERROR,
    slug: Optional[str] = None,
    root_dir: Optional[str] = None,
    board: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct fail-closed snapshot. Partial or broken state is never reported as complete."""
    repo_obj = None
    if slug:
        owner, _, name = slug.partition("/")
        repo_obj = {
            "slug": slug,
            "owner": owner,
            "name": name,
            "root_dir": _redact_local_path(root_dir or ""),
            "current_branch": "",
            "head_sha": "",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "exit_code": code,
        "summary": summary,
        "degraded": True,
        "errors": [reason],
        "repository": repo_obj,
        "board": board,
        "open_issues": [],
        "open_pull_requests": [],
        "claims": [],
        "dependencies": [],
        "touches_reservations": [],
        "worktrees": [],
        "worker_assignments": {},
        "tags_releases": {
            "latest_tag": None,
            "framework_version": None,
            "tags": [],
        },
        "claimable_work": [],
        "candidate_diagnostics": {
            "integrity_issues": [],
            "blocked": [],
            "conflicted": [],
            "missing_touches": [],
        },
    }


def _resolve_repo_details(target_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Inspect local git repository metadata without mutation."""
    slug = local_repo_slug(run_cmd)
    if not slug or "/" not in slug:
        return None, "Could not resolve repository slug from origin remote."

    owner, _, name = slug.partition("/")

    code_branch, branch_out, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    current_branch = branch_out.strip() if code_branch == 0 else ""

    code_head, head_out, _ = run_cmd(["git", "rev-parse", "HEAD"], check=False)
    head_sha = head_out.strip() if code_head == 0 else ""

    return {
        "slug": slug,
        "owner": owner,
        "name": name,
        "root_dir": _redact_local_path(target_dir),
        "current_branch": current_branch,
        "head_sha": head_sha,
    }, None


def _resolve_board_inventory(  # noqa: C901, PLR0912
    slug: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[int, str]], Optional[str]]:
    """Resolve the canonical Project v2 board and status counts."""
    available = get_repo_projects(slug)
    if available is None:
        return None, None, f"Could not query Project v2 boards for '{slug}'."

    governed = select_governed_projects(available, slug)
    if len(governed) != 1:
        return (
            None,
            None,
            f"Could not identify exactly one canonical Project v2 board for '{slug}' (found {len(governed)}).",
        )

    project = governed[0]
    owner = (project.get("owner") or {}).get("login")
    number = project.get("number")
    proj_id = project.get("id") or ""
    title = project.get("title") or ""

    if not owner or not isinstance(number, int):
        return None, None, f"Canonical Project v2 board for '{slug}' has missing owner or number."

    items = _board_items(owner, number)
    if items is None:
        return None, None, f"Could not read items for board #{number} ({owner})."

    status_counts: Dict[str, int] = {}
    issue_board_statuses: Dict[int, str] = {}

    for item in items:
        if not isinstance(item, dict):
            return None, None, f"Malformed item on board #{number}."
        content = item.get("content") or {}
        if not isinstance(content, dict):
            return None, None, f"Malformed item content on board #{number}."

        item_repo = content.get("repository") or item.get("repository")
        if item_repo != slug:
            continue

        raw_type = content.get("type") or item.get("type")
        if raw_type is not None:
            if not isinstance(raw_type, str) or raw_type.strip().lower() != "issue":
                continue

        issue_num = content.get("number")
        if not isinstance(issue_num, int) or isinstance(issue_num, bool) or issue_num <= 0:
            continue

        raw_status = item.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            return None, None, f"Board item for issue #{issue_num} has empty or missing status on board #{number}."

        status_clean = raw_status.strip()
        if issue_num in issue_board_statuses:
            return None, None, f"Duplicate board item for issue #{issue_num} on board #{number}."

        issue_board_statuses[issue_num] = status_clean
        status_counts[status_clean] = status_counts.get(status_clean, 0) + 1

    # Sort status count keys deterministically
    sorted_status_counts = {k: status_counts[k] for k in sorted(status_counts.keys())}

    board_info = {
        "id": proj_id,
        "number": number,
        "title": title,
        "owner": owner,
        "status_counts": sorted_status_counts,
    }
    return board_info, issue_board_statuses, None


def _normalize_issues(
    issues: List[Dict[str, Any]],
    board_statuses: Dict[int, str],
) -> List[Dict[str, Any]]:
    """Normalize issues with board statuses, dependencies, and touches."""
    rows = []
    for issue in issues:
        num = issue["number"]
        body = issue.get("body") or ""
        labels_list = issue.get("labels") or []
        names = sorted([lbl.get("name") for lbl in labels_list if isinstance(lbl, dict) and lbl.get("name")])

        lbl_status = _identity(names, "status:")
        b_status = board_statuses.get(num, "")
        drifted = bool(lbl_status) and bool(b_status) and lbl_status != b_status.lower().replace(" ", "-")

        deps = sorted(parse_dependencies(body))
        touches_paths = sorted(parse_touches(body))

        rows.append({
            "number": num,
            "title": issue.get("title") or "",
            "state": "OPEN",
            "board_status": b_status,
            "label_status": lbl_status or "",
            "labels": names,
            "agent": claimed_by(issue),
            "priority": canonical_priority_display(labels_list) or "",
            "dependencies": deps,
            "touches": touches_paths,
            "is_epic": is_epic(labels_list),
            "needs_human": needs_human(labels_list),
            "parallel_eligible": is_parallel_eligible(body, labels_list),
            "drifted": drifted,
        })
    return sorted(rows, key=lambda row: row["number"])


def _extract_ci_summary(pr: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and normalize CI rollup status."""
    if pr.get("statusCheckRollup") is None:
        return {"state": "UNAVAILABLE", "summary": "CI status rollup unavailable in REST inventory."}

    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return {"state": "UNAVAILABLE", "summary": "CI status rollup unavailable or invalid."}
    if not rollup:
        return {"state": "NONE", "summary": "No CI status checks reported on head commit."}

    states = []
    for check in rollup:
        if isinstance(check, dict):
            status = (check.get("status") or check.get("state") or "").upper()
            conclusion = (check.get("conclusion") or "").upper()
            states.append(conclusion or status or "UNKNOWN")

    if any(s in {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"} for s in states):
        return {"state": "FAILED", "summary": "One or more CI checks failed."}
    if any(s in {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"} for s in states):
        return {"state": "PENDING", "summary": "CI checks are in progress or pending."}
    if all(s in {"SUCCESS", "PASSED", "NEUTRAL", "SKIPPED"} for s in states) and states:
        return {"state": "PASSED", "summary": "All CI status checks passed."}
    return {"state": "UNKNOWN", "summary": f"CI status rollup: {', '.join(sorted(set(states)))}"}


def _extract_review_authority(pr_labels: List[str]) -> Dict[str, Any]:
    """Identify assigned review authority from labels matching exact-one contract."""
    recognized = sorted([lbl for lbl in pr_labels if lbl in REVIEW_SERVICE_LABELS])
    if len(recognized) == 0:
        return {
            "assigned": None,
            "state": "UNASSIGNED",
            "evidence": "No review:* label assigned.",
        }
    if len(recognized) > 1:
        return {
            "assigned": None,
            "state": "AMBIGUOUS",
            "evidence": f"Multiple review labels assigned: {', '.join(recognized)}.",
        }

    assigned = REVIEW_SERVICE_LABELS[recognized[0]]
    return {
        "assigned": assigned,
        "state": "ASSIGNED",
        "evidence": f"Assigned to {assigned} via label.",
    }


def _normalize_pull_requests(prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize pull requests with CI, review authority, and merge readiness summary."""
    rows = []
    for pr in prs:
        num = pr["number"]
        labels_list = pr.get("labels") or []
        names = sorted([lbl.get("name") for lbl in labels_list if isinstance(lbl, dict) and lbl.get("name")])

        author = _identity(names, "author:")
        merger = _identity(names, "merger:")
        draft = bool(pr.get("isDraft") or pr.get("draft"))
        linked = sorted(linked_issue_numbers_from_pr(pr))

        ci_info = _extract_ci_summary(pr)
        review_auth = _extract_review_authority(names)

        blockers = []
        if draft:
            blockers.append("PR is a draft.")
        if not author:
            blockers.append("Missing author:<id> label.")
        if ci_info["state"] == "UNAVAILABLE":
            blockers.append("CI check status is unavailable.")
        elif ci_info["state"] == "NONE":
            blockers.append("No CI checks reported on head commit.")
        elif ci_info["state"] != "PASSED":
            blockers.append(f"CI is {ci_info['state']}.")
        if review_auth["state"] == "AMBIGUOUS":
            blockers.append(f"Ambiguous review authority ({review_auth['evidence']})")
        elif review_auth["assigned"] is None:
            blockers.append("No review authority assigned.")

        rows.append({
            "number": num,
            "title": pr.get("title") or "",
            "branch": pr.get("headRefName") or (pr.get("head") or {}).get("ref") or "",
            "head_sha": pr.get("headRefOid") or (pr.get("head") or {}).get("sha") or "",
            "draft": draft,
            "author": author,
            "merger": merger,
            "labels": names,
            "linked_issues": linked,
            "ci": ci_info,
            "review_authority": review_auth,
            "unresolved_findings": {
                "count": 0,
                "items": [],
            },
            "merge_gate": {
                "ready": len(blockers) == 0,
                "blockers": blockers,
            },
        })
    return sorted(rows, key=lambda row: row["number"])


def _collect_tags_and_releases(target_dir: str = "") -> Dict[str, Any]:
    """Collect git tags and framework version."""
    code_tags, out_tags, _ = run_cmd(["git", "tag", "--list"], check=False)
    tags = sorted([t.strip() for t in out_tags.splitlines() if t.strip()]) if code_tags == 0 else []

    code_desc, out_desc, _ = run_cmd(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
        check=False,
    )
    latest_tag = out_desc.strip() if code_desc == 0 and out_desc.strip() else (tags[-1] if tags else None)

    framework_ver = get_current_framework_version(get_framework_root())

    return {
        "latest_tag": latest_tag,
        "framework_version": framework_ver,
        "tags": tags,
    }


def _collect_claims(
    issue_rows: List[Dict[str, Any]],
    pr_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build sorted claim list across issues, PRs, and merges."""
    claims = []
    for issue in issue_rows:
        if issue.get("agent"):
            claims.append({
                "type": "issue",
                "number": issue["number"],
                "agent": issue["agent"],
            })
    for pr in pr_rows:
        if pr.get("author"):
            claims.append({
                "type": "pr",
                "number": pr["number"],
                "agent": pr["author"],
            })
        if pr.get("merger"):
            claims.append({
                "type": "merge",
                "number": pr["number"],
                "agent": pr["merger"],
            })
    return sorted(claims, key=lambda c: (c["type"], c["number"], c["agent"]))


def _collect_dependencies(issue_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build issue dependency map noting unresolved prerequisites."""
    open_numbers = {i["number"] for i in issue_rows}
    dep_list = []
    for issue in issue_rows:
        deps = issue.get("dependencies") or []
        if deps:
            unresolved = sorted([d for d in deps if d in open_numbers])
            dep_list.append({
                "issue": issue["number"],
                "depends_on": sorted(deps),
                "unresolved": unresolved,
            })
    return sorted(dep_list, key=lambda d: d["issue"])


def _collect_touches_reservations(issue_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """List path reservations held by active in-flight issues."""
    locks = []
    for issue in issue_rows:
        label_status = (issue.get("label_status") or "").lower()
        board_status = (issue.get("board_status") or "").lower()
        in_flight = (
            bool(issue.get("agent"))
            or board_status in {"in progress", "in review"}
            or label_status in {"in-progress", "in-review"}
        )
        if in_flight:
            touches_list = issue.get("touches") or []
            if touches_list:
                locks.append({
                    "issue": issue["number"],
                    "agent": issue.get("agent") or "unknown",
                    "paths": sorted(touches_list),
                })
    return sorted(locks, key=lambda lock: lock["issue"])


def _collect_worker_assignments(
    claims: List[Dict[str, Any]],
    worktree_rows: List[Dict[str, Any]],
) -> Dict[str, Dict[str, List[Any]]]:
    """Group issues, PRs, and worktrees by owning agent."""
    assignments: Dict[str, Dict[str, List[Any]]] = {}

    for claim in claims:
        agent = claim["agent"]
        rec = assignments.setdefault(agent, {"issues": [], "prs": [], "worktrees": []})
        if claim["type"] == "issue" and claim["number"] not in rec["issues"]:
            rec["issues"].append(claim["number"])
        elif claim["type"] in {"pr", "merge"} and claim["number"] not in rec["prs"]:
            rec["prs"].append(claim["number"])

    for wt in worktree_rows:
        agent = wt.get("agent")
        if agent:
            rec = assignments.setdefault(agent, {"issues": [], "prs": [], "worktrees": []})
            if wt["path"] not in rec["worktrees"]:
                rec["worktrees"].append(wt["path"])

    for agent in assignments:
        assignments[agent]["issues"] = sorted(assignments[agent]["issues"])
        assignments[agent]["prs"] = sorted(assignments[agent]["prs"])
        assignments[agent]["worktrees"] = sorted(assignments[agent]["worktrees"])

    return {k: assignments[k] for k in sorted(assignments.keys())}


def _fetch_open_pull_requests(slug: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Fetch open pull requests attempting rich query first, falling back to REST inventory."""
    rich_prs = rich_open_pull_requests(run_cmd, slug)
    if rich_prs is not None:
        return rich_prs, None
    rest_prs = rest_open_pull_requests(run_cmd, slug)
    if rest_prs is not None:
        return rest_prs, None
    return None, f"Failed to list open pull requests from GitHub for '{slug}'."


def _collect_claimable_work(
    raw_issues: List[Dict[str, Any]],
    slug: str,
    repo_owner: Optional[str] = None,
    agent: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str], bool, bool]:
    """Evaluate Ready candidates and capture bounded diagnostics and integrity failures."""
    try:
        owner = repo_owner or repository_owner_login(slug)
    except Exception:
        owner = None
    try:
        trusted_logins = repository_trusted_logins(slug)
    except Exception:
        trusted_logins = None
    if not owner or trusted_logins is None:
        return (
            [],
            {
                "integrity_issues": [],
                "blocked": [],
                "conflicted": [],
                "missing_touches": [],
            },
            [f"Could not resolve trusted collaborator logins for repository '{slug}'. Candidate evaluation is degraded."],
            True,
            True,
        )

    try:
        build_res = build_candidates(
            raw_issues,
            agent=agent,
            repo_owner=owner,
            trusted_logins=trusted_logins,
        )
    except Exception as exc:
        return (
            [],
            {
                "integrity_issues": [],
                "blocked": [],
                "conflicted": [],
                "missing_touches": [],
            },
            [f"Candidate evaluation failed: {exc}"],
            True,
            True,
        )

    if not isinstance(build_res, dict):
        return (
            [],
            {
                "integrity_issues": [],
                "blocked": [],
                "conflicted": [],
                "missing_touches": [],
            },
            ["Candidate evaluation returned malformed result."],
            True,
            True,
        )

    candidates = build_res.get("candidates") or []
    claimable = []
    for cand in candidates:
        if isinstance(cand, dict):
            num = cand.get("number")
            labels = cand.get("labels") or []
            body = cand.get("body") or ""
            p_rank = canonical_priority_display(labels) or "P3"
            claimable.append({
                "issue": num,
                "priority": p_rank,
                "title": cand.get("title") or "",
                "touches": sorted(parse_touches(body)),
                "skill": skill_for_issue(cand),
            })

    def sort_key(item: Dict[str, Any]) -> Tuple[int, int]:
        r, _ = priority_rank([{"name": f"priority:{item['priority'].lower()}"}])
        return (r if r is not None else 99, item["issue"])

    sorted_claimable = sorted(claimable, key=sort_key)

    raw_integrity = build_res.get("integrity_issues") or []
    integrity_diag = [
        {"number": item["number"], "reason": item.get("reason", "unknown")}
        for item in raw_integrity
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    integrity_diag.sort(key=lambda x: x["number"])

    raw_blocked = build_res.get("blocked") or []
    blocked_diag = [
        {"number": item["number"], "blocked_by": sorted(set(item.get("blocked_by") or []))}
        for item in raw_blocked
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    blocked_diag.sort(key=lambda x: x["number"])

    raw_conflicted = build_res.get("conflicted") or []
    conflicted_diag = [
        {"number": item["number"], "conflict": sorted(set(item.get("conflict") or []))}
        for item in raw_conflicted
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    conflicted_diag.sort(key=lambda x: x["number"])

    raw_missing = build_res.get("missing_touches") or []
    missing_diag = sorted(set(n for n in raw_missing if isinstance(n, int)))

    diagnostics = {
        "integrity_issues": integrity_diag,
        "blocked": blocked_diag,
        "conflicted": conflicted_diag,
        "missing_touches": missing_diag,
    }

    errors: List[str] = []
    is_degraded = False
    is_blocked = False

    if integrity_diag:
        is_degraded = True
        is_blocked = True
        for item in integrity_diag:
            errors.append(f"Candidate evaluation integrity failure on issue #{item['number']}: {item['reason']}.")

    return sorted_claimable, diagnostics, errors, is_degraded, is_blocked


def evaluate_factory_loop_snapshot(  # noqa: C901, PLR0912, PLR0915
    repo_dir: str = ".",
    *,
    agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Report authoritative, deterministic read-only factory loop snapshot for repo_dir."""
    try:
        target = os.path.abspath(repo_dir)
    except (OSError, TypeError, ValueError) as exc:
        return _fail_closed(f"Could not resolve repo dir '{repo_dir}': {exc}", "ERROR: Invalid repo dir.")

    if not os.path.isdir(target):
        return _fail_closed(f"Directory does not exist: {target}", "ERROR: Invalid repo dir.")

    previous = os.getcwd()
    try:
        os.chdir(target)

        repo_details, err_repo = _resolve_repo_details(target)
        if err_repo or not repo_details:
            return _fail_closed(err_repo or "Repository resolution failed.", "ERROR: Repository resolution failed.")

        slug = repo_details["slug"]

        board_info, board_statuses, err_board = _resolve_board_inventory(slug)
        if err_board or board_info is None or board_statuses is None:
            return _fail_closed(
                err_board or "Governed board state is unusable.",
                "BLOCKED: Governed project board state is unusable.",
                state="blocked",
                code=EXIT_BLOCKED,
                slug=slug,
                root_dir=target,
            )

        raw_issues = rest_open_issues(run_cmd, slug)
        if raw_issues is None:
            return _fail_closed(
                f"Failed to list open issues from GitHub for '{slug}'.",
                "ERROR: Could not read open issues.",
                slug=slug,
                root_dir=target,
                board=board_info,
            )

        missing_board_issues = [
            issue["number"] for issue in raw_issues if issue["number"] not in board_statuses
        ]
        if missing_board_issues:
            missing_str = ", ".join(f"#{n}" for n in sorted(missing_board_issues))
            return _fail_closed(
                f"Open issue(s) {missing_str} missing from canonical Project v2 board #{board_info['number']}.",
                "BLOCKED: Governed project board state is unusable.",
                state="blocked",
                code=EXIT_BLOCKED,
                slug=slug,
                root_dir=target,
                board=board_info,
            )

        raw_prs, err_prs = _fetch_open_pull_requests(slug)
        if err_prs or raw_prs is None:
            return _fail_closed(
                err_prs or f"Failed to list open pull requests from GitHub for '{slug}'.",
                "ERROR: Could not read open PRs.",
                slug=slug,
                root_dir=target,
                board=board_info,
            )

        worktrees = list_worktrees()
        if worktrees is None:
            return _fail_closed(
                "Failed to read local git worktree state.",
                "ERROR: Could not read local worktrees.",
                slug=slug,
                root_dir=target,
                board=board_info,
            )

        open_numbers = {issue["number"] for issue in raw_issues}
        issue_rows = _normalize_issues(raw_issues, board_statuses)
        pr_rows = _normalize_pull_requests(raw_prs)
        worktree_rows = _worktree_rows(worktrees, open_numbers)

        # Sanitize local paths in worktrees
        sanitized_worktrees = []
        for wt in worktree_rows:
            sanitized_worktrees.append({
                "path": _redact_local_path(wt["path"]),
                "branch": wt["branch"],
                "issue": wt["issue"],
                "agent": wt["agent"],
                "stale": wt["stale"],
            })
        sanitized_worktrees.sort(key=lambda wt: (wt["path"], wt["branch"]))

        claims = _collect_claims(issue_rows, pr_rows)
        dependencies = _collect_dependencies(issue_rows)
        touches_reservations = _collect_touches_reservations(issue_rows)
        worker_assignments = _collect_worker_assignments(claims, sanitized_worktrees)
        tags_releases = _collect_tags_and_releases(target)
        (
            claimable_work,
            candidate_diagnostics,
            eval_errors,
            eval_degraded,
            eval_blocked,
        ) = _collect_claimable_work(
            raw_issues,
            slug=slug,
            repo_owner=repo_details.get("owner"),
            agent=agent,
        )

        open_work = bool(issue_rows or pr_rows or any(wt["issue"] is not None for wt in sanitized_worktrees))

        errors: List[str] = []
        if eval_errors:
            errors.extend(eval_errors)

        degraded = eval_degraded
        if eval_blocked:
            state = "blocked"
            exit_code = EXIT_BLOCKED
            summary = (
                f"BLOCKED: Candidate evaluation encountered {len(eval_errors)} integrity/evaluation error(s)."
            )
        elif open_work:
            state = "waiting"
            exit_code = EXIT_WAITING
            summary = (
                f"WAITING: {len(issue_rows)} open issue(s), {len(pr_rows)} open PR(s), {len(claims)} claim(s)."
            )
        else:
            state = "complete"
            exit_code = EXIT_COMPLETE
            summary = "COMPLETE: no open issues, no open PRs, no claims, no issue worktrees."

        return {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "exit_code": exit_code,
            "summary": summary,
            "degraded": degraded,
            "errors": errors,
            "repository": repo_details,
            "board": board_info,
            "open_issues": issue_rows,
            "open_pull_requests": pr_rows,
            "claims": claims,
            "dependencies": dependencies,
            "touches_reservations": touches_reservations,
            "worktrees": sanitized_worktrees,
            "worker_assignments": worker_assignments,
            "tags_releases": tags_releases,
            "claimable_work": claimable_work,
            "candidate_diagnostics": candidate_diagnostics,
        }

    except Exception as exc:
        return _fail_closed(f"Unexpected snapshot evaluation error: {exc}", "ERROR: Unexpected exception.")
    finally:
        os.chdir(previous)


def format_snapshot_text(snapshot: Dict[str, Any]) -> str:  # noqa: C901, PLR0912
    """Format snapshot as human-readable plain text summary."""
    repo = snapshot.get("repository") or {}
    repo_name = repo.get("name") or repo.get("slug") or "Factory"
    lines = [
        f"=== {repo_name}: Factory Loop Snapshot ===",
        f"Schema: {snapshot.get('schema_version', 'unknown')}",
        f"State: {snapshot.get('state', 'UNKNOWN').upper()}",
        f"Summary: {snapshot.get('summary', '')}",
    ]
    if repo.get("slug"):
        lines.append(f"Repository: {repo.get('slug')} ({repo.get('current_branch')})")

    board = snapshot.get("board")
    if board:
        lines.append(f"Board: #{board.get('number')} '{board.get('title')}'")
        counts = board.get("status_counts") or {}
        if counts:
            lines.append("  Status Counts: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))

    claimable = snapshot.get("claimable_work") or []
    if claimable:
        lines.append(f"Claimable Work ({len(claimable)}):")
        for item in claimable:
            lines.append(f"  • #{item['issue']} [{item['priority']}] {item['title']} ({item['skill']})")

    diag = snapshot.get("candidate_diagnostics") or {}
    if diag.get("integrity_issues"):
        lines.append(f"Candidate Integrity Issues ({len(diag['integrity_issues'])}):")
        for item in diag["integrity_issues"]:
            lines.append(f"  • #{item['number']}: {item['reason']}")
    if diag.get("blocked"):
        lines.append(f"Blocked Candidates ({len(diag['blocked'])}):")
        for item in diag["blocked"]:
            deps_str = ", ".join(f"#{d}" for d in item["blocked_by"])
            lines.append(f"  • #{item['number']} (blocked by {deps_str})")
    if diag.get("conflicted"):
        lines.append(f"Conflicted Candidates ({len(diag['conflicted'])}):")
        for item in diag["conflicted"]:
            conf_str = ", ".join(item["conflict"])
            lines.append(f"  • #{item['number']} (conflict: {conf_str})")
    if diag.get("missing_touches"):
        lines.append(f"Missing Touches / Untrusted ({len(diag['missing_touches'])}):")
        for num in diag["missing_touches"]:
            lines.append(f"  • #{num}")

    worktrees = snapshot.get("worktrees") or []
    if worktrees:
        lines.append(f"Worktrees ({len(worktrees)}):")
        for wt in worktrees:
            lines.append(f"  • [{'stale' if wt['stale'] else 'active'}] {wt['path']} ({wt['branch']})")

    errors = snapshot.get("errors") or []
    if errors:
        lines.append("Errors / Degradations:")
        for err in errors:
            lines.append(f"  • {err}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit bounded, deterministic read-only factory loop snapshot.",
    )
    parser.add_argument("--repo-dir", default=".", help="Governed repository working directory")
    parser.add_argument("--json", action="store_true", help="Emit snapshot as JSON")
    parser.add_argument("--agent", default=None, help="Agent identity for candidate matching")
    args = parser.parse_args()

    snapshot = evaluate_factory_loop_snapshot(args.repo_dir, agent=args.agent)
    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        print(format_snapshot_text(snapshot))
    sys.exit(snapshot["exit_code"])


if __name__ == "__main__":
    main()
