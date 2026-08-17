#!/usr/bin/env python3
"""
fleet_status.py - Authoritative state calculation for Aru_Agentic_SDLC factory.

Provides deterministic evaluation of factory state:
  * complete (exit 0) - every governed issue is Done/closed, no open PRs, no active claims,
                       no board drift, no orphan worktrees.
  * waiting  (exit 2) - active work in flight, Ready/In Progress/In Review/Backlog issues,
                       pending CI, or pending reviews.
  * blocked  (exit 3) - severe escalated merge conflict or ambiguous board.
  * stalled  (exit 4) - zero merges within --stall-hours while open PRs and
                       registered agents both exist; outranks waiting, never blocked.
  * error    (exit 1) - GitHub API / auth failures; fails closed.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common import (
    claimed_by,
    get_repo_projects,
    get_repo_slug,
    label_names,
    query_issue_project_items,
    query_open_issues,
    run_cmd,
    run_gh_json,
    select_governed_project_items,
    select_governed_projects,
)

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_WAITING = 2
EXIT_BLOCKED = 3
# A stall is not a blocked repo. Reusing EXIT_BLOCKED would hide zero
# throughput inside a code callers already treat as routine.
EXIT_STALLED = 4

STUCK_HOURS = 4.0
DEFAULT_STALL_HOURS = 4.0
# Merged PRs listed by ``gh pr list --state merged`` come back newest-created
# first, not newest-merged. A long-lived PR merged today can fall outside a
# 30-row creation window and look like a stall. Search by merge date instead.
STALL_MERGE_WINDOW = 100
STALL_MERGE_LOOKBACK_DAYS = 30
REVIEW_AGE_WARN_HOURS = 2.0
REVIEW_AGE_ATTN_HOURS = 8.0
CI_FAIL_WARN = 0.2
CI_FAIL_ATTN = 0.5
REWORK_WARN = 2
REWORK_ATTN = 3
_SEV_RANK = {"ok": 0, "warn": 1, "attn": 2}
_CI_FAILED_CONCLUSIONS = {"FAILURE", "FAILED", "TIMED_OUT", "STARTUP_FAILURE"}
_CI_COMPLETED_CONCLUSIONS = _CI_FAILED_CONCLUSIONS | {
    "SUCCESS", "NEUTRAL", "SKIPPED", "CANCELLED",
}
_QUEUED_AT_RE = re.compile(r"review-queued-at:\s*(\S+)")
_REWORK_BLOCKING_RE = re.compile(
    r"changes[\s_-]*requested|(?<![Nn]o )blocking findings?|\*\*blocking:\*\*",
    re.I,
)

LINE_CEILING = 400
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".worktrees",
    ".mypy_cache",
    "dist",
    "build",
    "coverage",
    ".next",
    "vendor",
    "target",
    "Pods",
    "DerivedData",
    ".tox",
}

# Source files the factory actually ships, across stack packs. Docs, lockfiles,
# images, and generated minified assets stay out so LOC is not inflated.
CODE_FILE_SUFFIXES = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs",
    ".java", ".kt", ".kts",
    ".swift", ".m", ".mm",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
    ".cs",
    ".rb", ".php", ".scala", ".lua", ".r",
    ".sql",
    ".sh", ".bash", ".zsh", ".ps1",
    ".gd", ".dart",
    ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".graphql", ".gql",
})

PR_FIELDS = (
    "number,title,isDraft,labels,reviews,statusCheckRollup,updatedAt,"
    "createdAt,headRefName,body,comments,reviewDecision,mergeStateStatus,state"
)


def list_open_prs_details() -> Optional[List[Dict[str, Any]]]:
    """Fetches list of open PRs via gh CLI."""
    cmd = ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", PR_FIELDS]
    res = run_gh_json(cmd)
    return res if isinstance(res, list) else None


def list_worktree_branches() -> List[str]:
    """Lists active git worktree branches."""
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False)
    if code != 0 or not out:
        return []
    branches = []
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.append(line.replace("branch refs/heads/", "").strip())
    return branches


def _is_regular_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _count_lines(path: str) -> Optional[int]:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def _is_source_file(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".min.js") or lower.endswith(".min.css"):
        return False
    return os.path.splitext(lower)[1] in CODE_FILE_SUFFIXES


def collect_codebase_health(repo_dir: str) -> Dict[str, Any]:
    """Summarize local bloat: LOC, average size, files at the 400-line ceiling."""
    loc = 0
    file_count = 0
    total_bytes = 0
    over_ceiling = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES]
        for name in files:
            if not _is_source_file(name):
                continue
            path = os.path.join(root, name)
            if not _is_regular_file(path):
                continue
            lines = _count_lines(path)
            if lines is None:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            file_count += 1
            loc += lines
            total_bytes += size
            if lines >= LINE_CEILING:
                rel = os.path.relpath(path, repo_dir)
                over_ceiling.append({"path": rel, "lines": lines})
    over_ceiling.sort(key=lambda item: (-item["lines"], item["path"]))
    average = int(total_bytes / file_count) if file_count else 0
    return {
        "loc": loc,
        "file_count": file_count,
        "average_file_bytes": average,
        "line_ceiling": LINE_CEILING,
        "files_at_or_over_ceiling": over_ceiling,
    }


def _error(reason: str, summary: str) -> Dict[str, Any]:
    return {
        "state": "error",
        "exit_code": EXIT_ERROR,
        "reasons": [reason],
        "summary": summary,
    }


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hours_ago(value: Any, now: datetime) -> Optional[float]:
    parsed = value if isinstance(value, datetime) else _parse_ts(value)
    if parsed is None:
        return None
    hours = (now - parsed).total_seconds() / 3600.0
    return round(hours, 2) if hours >= 0 else None


def _worst_severity(levels: Iterable[str]) -> str:
    worst = "ok"
    for level in levels:
        if _SEV_RANK.get(level, 0) > _SEV_RANK[worst]:
            worst = level
    return worst


def _question(key: str, title: str, severity: str, summary: str, **payload: Any) -> Dict[str, Any]:
    return {"key": key, "title": title, "severity": severity, "summary": summary, **payload}


def _merged_pr_search(now: Optional[datetime] = None) -> str:
    """GitHub search that is bounded by merge date, not creation date."""
    when = now or datetime.now(timezone.utc)
    since = (when - timedelta(days=STALL_MERGE_LOOKBACK_DAYS)).date().isoformat()
    return f"is:pr is:merged merged:>={since}"


def most_recent_merge_history() -> Tuple[Optional[datetime], bool]:
    """Newest mergedAt in the stall lookback, plus whether the lookup succeeded.

    A failed ``gh`` call is not the same as an empty merge history. Callers
    must not report a stall when this returns ``(None, False)``.
    """
    res = run_gh_json([
        "gh", "pr", "list",
        "--search", _merged_pr_search(),
        "--limit", str(STALL_MERGE_WINDOW),
        "--json", "mergedAt",
    ])
    if not isinstance(res, list):
        return None, False
    stamps = []
    for row in res:
        if not isinstance(row, dict):
            continue
        parsed = _parse_ts(row.get("mergedAt"))
        if parsed is not None:
            stamps.append(parsed)
    return (max(stamps) if stamps else None), True


def most_recent_merge_time() -> Optional[datetime]:
    """Newest mergedAt across a bounded window, or None if empty or unreadable."""
    newest, ok = most_recent_merge_history()
    return newest if ok else None


def registered_agent_count(repo_dir: str = ".") -> int:
    """Agents registered against this project. 0 when presence is unreadable.

    Read-only: expire=False, matching doctor_presence_summary. Heartbeat
    expiry must never mutate the registry from a diagnostic path, and it
    never releases a GitHub claim.
    """
    try:
        import agent_presence
    except ImportError:
        return 0
    try:
        target = str(Path(repo_dir).resolve())
        project_id = agent_presence.resolve_project_id(Path(target))
        records = agent_presence.PresenceStore().query_project(
            checkout_path=target, project_id=project_id, expire=False,
        )
    except Exception:
        # Presence is advisory. An unreadable registry must not manufacture a
        # stall, and detect_stall() treats 0 agents as "no stall".
        return 0
    return len(records) if isinstance(records, list) else 0


def detect_stall(
    hours_since_last_merge: Optional[float],
    open_pr_count: int,
    agent_count: int,
    stall_hours: float = DEFAULT_STALL_HOURS,
    merge_lookup_ok: bool = True,
) -> bool:
    """True only when throughput is zero AND there is work AND agents to do it.

    All three conditions are required. A quiet board with nothing open is not
    a stall, and neither is a board with no agents running -- alerting on
    either trains operators to ignore the alarm. A failed merge-history
    lookup is also not a stall: absence of evidence must not page anyone.
    """
    if not merge_lookup_ok or stall_hours <= 0:
        return False
    if open_pr_count <= 0 or agent_count <= 0:
        return False
    if hours_since_last_merge is None:
        # Nothing merged in the window, with open PRs and live agents.
        return True
    return hours_since_last_merge >= stall_hours


def _stall_question(
    hours_since_last_merge: Optional[float],
    open_pr_count: int,
    agent_count: int,
    stall_hours: float = DEFAULT_STALL_HOURS,
    merge_lookup_ok: bool = True,
) -> Dict[str, Any]:
    stalled = detect_stall(
        hours_since_last_merge, open_pr_count, agent_count, stall_hours,
        merge_lookup_ok=merge_lookup_ok,
    )
    if not merge_lookup_ok:
        summary = (
            f"Merge history unavailable; stall detection skipped. "
            f"{open_pr_count} open PR(s), {agent_count} agent(s)."
        )
    else:
        age = "never" if hours_since_last_merge is None else f"{hours_since_last_merge:.1f}h ago"
        if stalled:
            summary = (
                f"No merge in {stall_hours:g}h (last: {age}) with "
                f"{open_pr_count} open PR(s) and {agent_count} registered agent(s)."
            )
        else:
            summary = f"Last merge {age}; {open_pr_count} open PR(s), {agent_count} agent(s)."
    return _question(
        "stall", "Is the fleet still merging?", "attn" if stalled else "ok", summary,
        stalled=stalled,
        hours_since_last_merge=hours_since_last_merge,
        stall_hours=stall_hours,
        open_pr_count=open_pr_count,
        registered_agents=agent_count,
        merge_lookup_ok=merge_lookup_ok,
    )


def resolve_fleet_size(configured: Optional[int] = None) -> Optional[int]:
    """Prefer an explicit count; otherwise read ARU_FLEET_SIZE. Never infer from claims."""
    if configured is not None:
        return configured if configured >= 0 else None
    raw = os.environ.get("ARU_FLEET_SIZE", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _launch_fleet_clone_count(repo_dir: str, home: Optional[Path] = None) -> int:
    root = (home or Path.home()) / ".aru-fleet" / Path(os.path.abspath(repo_dir)).name
    if not root.is_dir():
        return 0
    return sum(1 for child in root.iterdir() if child.is_dir() and (child / ".git").exists())


def _run_fleet_agent_count(repo_dir: str, state_root: Optional[Path] = None) -> int:
    if state_root is None:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        digest = hashlib.sha256(str(Path(repo_dir).resolve()).encode("utf-8")).hexdigest()[:16]
        state_root = base / "aru-factory" / digest
    if not state_root.is_dir():
        return 0
    return sum(
        1 for child in state_root.iterdir()
        if child.is_file() and child.suffix == ".json" and child.stem[0:1] != "."
    )


def discover_fleet_size(repo_dir: str) -> Optional[int]:
    """Count launch_fleet clones and run_fleet runner state; never use held claims."""
    launched = max(_launch_fleet_clone_count(repo_dir), _run_fleet_agent_count(repo_dir))
    return launched or None


def fetch_ci_history(window_days: int, repo_dir: str = ".") -> List[Dict[str, Any]]:
    """Windowed Actions runs for ``repo_dir``, including failed-then-green reruns."""
    from factory_metrics import fetch_ci_runs

    original = os.getcwd()
    target = os.path.abspath(repo_dir)
    try:
        os.chdir(target)
        return fetch_ci_runs(window_days)
    except OSError as exc:
        raise RuntimeError(
            f"Could not collect CI history from repository '{target}': {exc}"
        ) from exc
    finally:
        os.chdir(original)


def _comment_bodies(pr: Dict[str, Any]) -> List[str]:
    bodies = [pr.get("body") or ""]
    for comment in pr.get("comments") or []:
        if isinstance(comment, dict):
            bodies.append(comment.get("body") or "")
        elif isinstance(comment, str):
            bodies.append(comment)
    return bodies


def _latest_queued_at(pr: Dict[str, Any]) -> Optional[datetime]:
    stamps = []
    for body in _comment_bodies(pr):
        for match in _QUEUED_AT_RE.finditer(body):
            parsed = _parse_ts(match.group(1))
            if parsed is not None:
                stamps.append(parsed)
    return max(stamps) if stamps else None


def _has_reviewed_by(pr: Dict[str, Any]) -> bool:
    return any(
        name.startswith("reviewed-by:") and name.split(":", 1)[-1]
        for name in label_names(pr)
    )


def resolve_ready_target(
    fleet_size: Optional[int] = None, configured: Optional[int] = None
) -> Optional[int]:
    """Prefer an explicit target; otherwise read ARU_READY_TARGET; otherwise fleet_size."""
    if configured is not None:
        return configured if configured >= 0 else None
    raw = os.environ.get("ARU_READY_TARGET", "").strip()
    if raw:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    if fleet_size is not None and fleet_size >= 0:
        return fleet_size
    return None


def _review_queue_depth(prs: List[Dict[str, Any]]) -> int:
    """Counts open PRs awaiting review."""
    return sum(1 for pr in prs if _pending_review(pr))


def _ready_severity(
    ready_depth: int,
    ready_target: Optional[int],
    claimable: int,
    review_queue_depth: int = 0,
    is_complete: bool = False,
) -> tuple[str, str, str]:
    if ready_target is None:
        return "warn", " — fleet size unavailable", "unknown"
    if is_complete:
        return "ok", "", "complete"
    if review_queue_depth >= 6:
        return "attn", f" — review queue flooded ({review_queue_depth} awaiting review)", "flooded"
    if ready_depth < ready_target:
        return "attn", " — factory is starved", "starved"
    if claimable < ready_target:
        return "warn", " — path conflicts starve extra agents", "conflicted"
    if review_queue_depth > 0 and review_queue_depth >= ready_target:
        return "warn", f" — review queue accumulating ({review_queue_depth} awaiting review)", "busy"
    return "ok", "", "healthy"


def _ready_question(
    issues: List[Dict[str, Any]],
    prs: Optional[List[Dict[str, Any]]] = None,
    fleet_size: Optional[int] = None,
    ready_target: Optional[int] = None,
) -> Dict[str, Any]:
    from triage_backlog import capacity, partition

    prs = prs or []
    _, ready, held = partition(issues)
    cap = capacity(ready, held)
    ready_depth = cap["ready_total"]
    in_flight = len(held)
    claimable = len(cap["concurrent"])
    target = ready_target if ready_target is not None else resolve_ready_target(fleet_size)
    review_queue = _review_queue_depth(prs)
    is_complete = not issues and not prs

    severity, note, signal = _ready_severity(
        ready_depth, target, claimable, review_queue_depth=review_queue, is_complete=is_complete
    )
    fleet_text = "" if fleet_size is None else f"; fleet {fleet_size}"
    target_text = f"; target {target}" if target is not None and target != fleet_size else ""
    review_text = f", {review_queue} in review queue" if prs or review_queue > 0 else ""
    summary = (
        f"{ready_depth} Ready, {in_flight} in flight, {claimable} claimable"
        f"{review_text}{fleet_text}{target_text}{note}"
    )
    return _question(
        "ready_depth", "Ready depth vs fleet size", severity, summary,
        ready_depth=ready_depth,
        ready_target=target,
        fleet_size=fleet_size,
        in_flight=in_flight,
        claimable=claimable,
        review_queue_depth=review_queue,
        signal=signal,
    )


def _holder_rows(issues: List[Dict[str, Any]], prs: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for issue in issues:
        agent = claimed_by(issue)
        if not agent:
            continue
        age = _hours_ago(issue.get("updatedAt"), now)
        rows.append({
            "type": "issue", "number": issue["number"], "agent": agent,
            "age_hours": age,
            "age_availability": "measured" if age is not None else "unavailable",
        })
    for pr in prs:
        reviewer = next(
            (name[9:] for name in label_names(pr) if name.startswith("reviewer:")),
            None,
        )
        if not reviewer:
            continue
        age = _hours_ago(pr.get("updatedAt"), now)
        rows.append({
            "type": "review", "number": pr["number"], "agent": reviewer,
            "age_hours": age,
            "age_availability": "measured" if age is not None else "unavailable",
        })
    return rows


def _holders_question(issues: List[Dict[str, Any]], prs: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    rows = _holder_rows(issues, prs, now)
    stuck = [row for row in rows if row["age_hours"] is not None and row["age_hours"] >= STUCK_HOURS]
    missing = [row for row in rows if row["age_hours"] is None]
    if stuck:
        severity = "attn"
        summary = ", ".join(
            f"{row['type']} #{row['number']} ({row['agent']}) idle {row['age_hours']:.1f}h"
            for row in stuck
        )
    elif missing:
        severity = "warn"
        summary = f"{len(rows)} holder(s); {len(missing)} without a measured age"
    elif rows:
        severity = "ok"
        summary = ", ".join(
            f"{row['type']} #{row['number']} ({row['agent']}) {row['age_hours']:.1f}h"
            for row in rows
        )
    else:
        severity, summary = "ok", "no active claims"
    return _question("holders", "Who holds what, and for how long", severity, summary, holders=rows)


def _pending_review(pr: Dict[str, Any]) -> bool:  # noqa: C901, PLR0912
    if pr.get("isDraft"):
        return False
    if _has_reviewed_by(pr):
        return False
    decision = (pr.get("reviewDecision") or "").upper()
    if decision == "APPROVED":
        return False
    if "_active_review_feedback" in pr:
        feedback = pr["_active_review_feedback"]
        if feedback is not None and len(feedback) > 0:
            return False
    elif "unresolvedReviewThreadsCount" in pr:
        unresolved = pr.get("unresolvedReviewThreadsCount")
        if unresolved is not None and unresolved > 0:
            return False
    elif "reviewThreads" in pr:
        threads = pr.get("reviewThreads") or {}
        if isinstance(threads, dict) and "nodes" in threads:
            unresolved_nodes = sum(1 for t in threads["nodes"] if not t.get("isResolved"))
            if unresolved_nodes > 0:
                return False
        elif isinstance(threads, list):
            unresolved_list = sum(1 for t in threads if not t.get("isResolved"))
            if unresolved_list > 0:
                return False
    else:
        try:
            from fetch_pr_feedback import fetch_active_review_feedback
            feedback = fetch_active_review_feedback(pr["number"])
            pr["_active_review_feedback"] = feedback
            if feedback is not None and len(feedback) > 0:
                return False
        except (ImportError, Exception):
            pass
    return True


def _review_age_question(prs: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    pending = []
    for pr in prs:
        if not _pending_review(pr):
            continue
        queued = _latest_queued_at(pr)
        age = _hours_ago(queued, now) if queued is not None else None
        pending.append({
            "number": pr["number"], "age_hours": age,
            "age_availability": "measured" if age is not None else "unavailable",
        })
    measured = [row["age_hours"] for row in pending if row["age_hours"] is not None]
    oldest = max(measured) if measured else None
    if oldest is not None and oldest >= REVIEW_AGE_ATTN_HOURS:
        severity = "attn"
    elif oldest is not None and oldest >= REVIEW_AGE_WARN_HOURS:
        severity = "warn"
    elif pending and oldest is None:
        severity = "warn"
    else:
        severity = "ok"
    if not pending:
        summary = "none awaiting review"
    elif oldest is None:
        summary = f"{len(pending)} awaiting review; age unavailable"
    else:
        summary = f"{len(pending)} awaiting review; oldest {oldest:.1f}h"
    return _question(
        "review_age", "PRs awaiting review, by age", severity, summary,
        pending=pending, oldest_age_hours=oldest,
    )


def _is_rework_review(review: Dict[str, Any]) -> bool:
    state = (review.get("state") or "").upper()
    if state == "CHANGES_REQUESTED":
        return True
    if state != "COMMENTED":
        return False
    body = review.get("body") or ""
    if _REWORK_BLOCKING_RE.search(body):
        return True
    return False


def _review_rounds(pr: Dict[str, Any]) -> int:
    return sum(1 for review in (pr.get("reviews") or []) if _is_rework_review(review))


def _review_rounds_question(prs: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [{"number": pr["number"], "review_rounds": _review_rounds(pr)} for pr in prs]
    highest = max((row["review_rounds"] for row in rows), default=0)
    if highest >= REWORK_ATTN:
        severity = "attn"
    elif highest >= REWORK_WARN:
        severity = "warn"
    else:
        severity = "ok"
    if not rows:
        summary = "no open PRs"
    else:
        summary = f"max {highest} review round(s) across {len(rows)} open PR(s)"
    return _question(
        "review_rounds", "Review rounds per PR", severity, summary,
        pull_requests=rows, max_review_rounds=highest,
    )


def _ci_run_failed(run: Dict[str, Any]) -> Optional[bool]:
    status = (run.get("status") or "").upper()
    conclusion = (run.get("conclusion") or "").upper()
    if status in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"} and not conclusion:
        return None
    if not conclusion:
        return None
    if conclusion not in _CI_COMPLETED_CONCLUSIONS and status != "COMPLETED":
        return None
    return conclusion in _CI_FAILED_CONCLUSIONS


def _ci_question(
    ci_runs: Optional[List[Dict[str, Any]]] = None,
    ci_error: Optional[str] = None,
) -> Dict[str, Any]:
    if ci_error or ci_runs is None:
        detail = f"unavailable ({ci_error})" if ci_error else "unavailable"
        return _question(
            "ci_failure_rate", "CI failure rate", "warn", detail,
            availability="unavailable",
        )
    completed = 0
    failed = 0
    for run in ci_runs:
        result = _ci_run_failed(run)
        if result is None:
            continue
        completed += 1
        if result:
            failed += 1
    rate = round(failed / completed, 4) if completed else None
    if rate is not None and rate >= CI_FAIL_ATTN:
        severity = "attn"
    elif rate is not None and rate >= CI_FAIL_WARN:
        severity = "warn"
    else:
        severity = "ok"
    if completed == 0:
        summary = "no completed CI runs in window"
    else:
        summary = f"{failed}/{completed} failed ({rate:.0%})"
    return _question(
        "ci_failure_rate", "CI failure rate", severity, summary,
        failed=failed, completed=completed, failure_rate=rate,
        availability="measured",
    )


def _cost_question(closed: Optional[Dict[str, Any]], error: Optional[str]) -> Dict[str, Any]:
    if error or not closed:
        detail = f"unavailable ({error})" if error else "unavailable"
        return _question(
            "cost", "Cost and wall time per closed issue", "warn", detail,
            availability="unavailable",
        )
    cost = closed.get("cost_per_closed_issue") or {}
    cycles = [
        row["cycle_time_hours"]
        for row in closed.get("issues") or []
        if row.get("cycle_time_hours") is not None
    ]
    avg_cycle = round(sum(cycles) / len(cycles), 2) if cycles else None
    cycle_text = (
        f", avg cycle {avg_cycle:.2f}h" if avg_cycle is not None else ", cycle time unavailable"
    )
    outliers = closed.get("outlier_issue_numbers") or []
    count = closed.get("closed_issue_count", 0)
    avg_cost = cost.get("average_usd_measured")
    if outliers:
        severity = "attn"
        summary = f"{count} closed; {len(outliers)} outlier(s){cycle_text}"
    elif cost.get("availability") != "measured":
        severity = "warn"
        summary = f"{count} closed; cost unavailable (no measured local CLI data){cycle_text}"
    else:
        severity = "ok"
        summary = f"{count} closed; ${avg_cost:.6f} avg measured{cycle_text}"
    return _question(
        "cost", "Cost and wall time per closed issue", severity, summary,
        availability=cost.get("availability") or "unavailable",
        closed_issue_count=count,
        average_usd_measured=avg_cost,
        average_cycle_hours=avg_cycle,
        outlier_issue_numbers=outliers,
    )


def build_operator_screen(
    issues: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    closed_issues: Optional[Dict[str, Any]] = None,
    metrics_error: Optional[str] = None,
    fleet_size: Optional[int] = None,
    ready_target: Optional[int] = None,
    ci_runs: Optional[List[Dict[str, Any]]] = None,
    ci_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Answer the six §4.2 questions from already-fetched board and PR facts."""
    clock = now or datetime.now(timezone.utc)
    questions = [
        _ready_question(issues, prs=prs, fleet_size=fleet_size, ready_target=ready_target),
        _holders_question(issues, prs, clock),
        _review_age_question(prs, clock),
        _review_rounds_question(prs),
        _ci_question(ci_runs, ci_error),
        _cost_question(closed_issues, metrics_error),
    ]
    return {"questions": questions, "severity": _worst_severity(q["severity"] for q in questions)}


def apply_ci_failure_rate(
    screen: Optional[Dict[str, Any]],
    ci_runs: Optional[List[Dict[str, Any]]],
    ci_error: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not screen:
        return screen
    questions = [q for q in screen.get("questions") or [] if q.get("key") != "ci_failure_rate"]
    questions.insert(4, _ci_question(ci_runs, ci_error))
    screen["questions"] = questions
    screen["severity"] = _worst_severity(q["severity"] for q in questions)
    return screen


def apply_closed_issue_cost(
    screen: Optional[Dict[str, Any]],
    closed_issues: Optional[Dict[str, Any]],
    metrics_error: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not screen:
        return screen
    questions = [q for q in screen.get("questions") or [] if q.get("key") != "cost"]
    questions.append(_cost_question(closed_issues, metrics_error))
    screen["questions"] = questions
    screen["severity"] = _worst_severity(q["severity"] for q in questions)
    return screen


def format_operator_screen(screen: Dict[str, Any]) -> str:
    markers = {"ok": "[OK]  ", "warn": "[WARN]", "attn": "[ATTN]"}
    lines = ["", "=== Operator screen ==="]
    for question in screen.get("questions") or []:
        marker = markers.get(question.get("severity"), "[WARN]")
        lines.append(f"{marker} {question['title']}: {question['summary']}")
    return "\n".join(lines)


def _write_alert_file(directory: str, text: str) -> str:
    """Operator-owned 0600 alert body. Never interpolated into a shell."""
    path = os.path.join(directory, f"aru-stall-{os.getpid()}.txt")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    return path


def stall_alert_text(question: Dict[str, Any], blocked: List[str]) -> str:
    """Secret-safe alert body: stable counts and gate names, never logs or diffs.

    The changing merge-age must not appear here. slack_notify.py hashes the
    full text for dedupe, so an elapsed-age that ticks every 0.1h would page
    operators once per cache window for the same stall.
    """
    hours = question.get("stall_hours")
    window = f"{hours:g}h" if isinstance(hours, (int, float)) else "the stall window"
    lines = [
        "Fleet has stopped merging.",
        (
            f"No merge within {window} with "
            f"{question.get('open_pr_count', 0)} open PR(s) and "
            f"{question.get('registered_agents', 0)} registered agent(s)."
        ),
    ]
    if blocked:
        lines.append("Open PRs and their blocking gate:")
        lines.extend(f"- {item}" for item in blocked)
    return "\n".join(line for line in lines if line)


def _stall_pr_reasons(reasons: Optional[List[Any]]) -> List[str]:
    """PR-level waiting reasons only; drop the stall summary and issue rows."""
    return [
        str(reason)
        for reason in (reasons or [])
        if str(reason).startswith("PR #")
    ]


def _stall_github_pr(status: Dict[str, Any], blocked: List[str]) -> Optional[int]:
    """Durable GitHub target for a fleet-level stall alert."""
    for value in status.get("open_pr_numbers") or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    for item in blocked:
        match = re.search(r"PR #(\d+)", item)
        if match:
            return int(match.group(1))
    return None


def notify_stall(
    question: Dict[str, Any],
    blocked: List[str],
    repo_dir: str,
    agent: str,
    family: str,
    pr: Optional[int] = None,
) -> bool:
    """Best-effort Slack alert. Never changes the caller's exit code.

    slack_notify.py owns deduplication; this must not re-alert per tick or add
    a second dedup layer. Slack downtime must never halt the GitHub loop.
    hitl delivery requires a durable GitHub issue/PR target.
    """
    slug = get_repo_slug()
    if not slug:
        return False
    project_id = os.environ.get("ARU_PROJECT_ID") or ""
    if not project_id:
        return False
    if pr is None:
        return False
    path = None
    try:
        path = _write_alert_file(repo_dir, stall_alert_text(question, blocked))
        code, _, _ = run_cmd([
            "python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "slack_notify.py"),
            "--project-id", project_id,
            "--agent", agent, "--family", family,
            "--event", "hitl", "--repo", slug,
            "--pr", str(int(pr)),
            "--repo-dir", repo_dir,
            "--decision-file", path,
        ], check=False)
        return code == 0
    except (OSError, ValueError):
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _pr_label_value(pr: Dict[str, Any], prefix: str) -> Optional[str]:
    for name in label_names(pr):
        if name.startswith(prefix):
            value = name[len(prefix):].strip()
            if value:
                return value
    return None


def next_queue_action(
    ok: bool,
    first_blocking: Optional[str],
    unresolved_count: int = 0,
) -> str:
    """Map dry-run gate outcome to an operator-facing next action.

    This is intentionally thinner than picker claim eligibility: the queue is
    global, not agent-relative. Unresolved threads route to feedback even when
    another gate also fails, matching the picker's feedback-before-merge order.
    """
    if ok:
        return "merge"
    if unresolved_count > 0:
        return "feedback"
    if first_blocking == "review":
        return "review"
    return "wait"


def queue_ci_label(pr: Optional[Dict[str, Any]], gates: Optional[List[Any]] = None) -> str:
    """Compact CI state for the merge-queue table (always shown)."""
    import merge_pr as mp

    if gates:
        for entry in gates:
            if isinstance(entry, dict):
                name, passed, message = entry.get("name"), entry.get("passed"), entry.get("message") or ""
            else:
                name, passed, message = entry[0], entry[1], entry[2] if len(entry) > 2 else ""
            if name == "ci":
                if passed:
                    return "green"
                lowered = (message or "").lower()
                if "not finished" in lowered or "pending" in lowered:
                    return "pending"
                if "no ci" in lowered:
                    return "none"
                return "red"
    if not pr:
        return "—"
    ok, message = mp.check_ci(pr)
    if ok:
        return "green"
    lowered = (message or "").lower()
    if "not finished" in lowered:
        return "pending"
    if "no ci" in lowered:
        return "none"
    return "red"


def queue_thread_label(unresolved: Optional[int], *, known: bool = True) -> str:
    """Compact review-thread state for the merge-queue table (always shown)."""
    if not known or unresolved is None:
        return "unknown"
    if unresolved <= 0:
        return "clean"
    return f"{unresolved} open"


def _serialize_queue_gates(gates: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in gates or []:
        if isinstance(entry, dict):
            out.append(
                {
                    "name": entry.get("name"),
                    "passed": bool(entry.get("passed")),
                    "message": entry.get("message") or "",
                }
            )
        else:
            name, passed, message = entry
            out.append({"name": name, "passed": bool(passed), "message": message})
    return out


def evaluate_queue_row(
    pr: Dict[str, Any],
    *,
    fetch_pr_fn=None,
    linked_issues_fn=None,
    issue_body_fn=None,
    review_evidence_fn=None,
    evaluate_dod_fn=None,
) -> Dict[str, Any]:
    """Evaluate one open PR for the merge-queue view without merging."""
    import merge_pr as mp

    fetch_pr_fn = fetch_pr_fn or mp.fetch_pr
    linked_issues_fn = linked_issues_fn or mp.linked_issues
    issue_body_fn = issue_body_fn or (
        lambda num: (mp._gh_json(["gh", "issue", "view", str(num), "--json", "body"]) or {}).get("body") or ""
    )
    review_evidence_fn = review_evidence_fn or mp.review_evidence
    evaluate_dod_fn = evaluate_dod_fn or mp.evaluate_dod

    number = pr.get("number")
    title = pr.get("title") or ""
    author = _pr_label_value(pr, "author:")
    reviewer = _pr_label_value(pr, "reviewed-by:") or _pr_label_value(pr, "reviewer:")

    full = fetch_pr_fn(number) if number is not None else None
    if not full:
        return {
            "pr": number,
            "title": title,
            "author": author,
            "reviewer": reviewer,
            "ok": False,
            "first_blocking": "fetch",
            "verdict": "could not fetch pull request",
            "next_action": "wait",
            "gates": [],
            "unresolved_threads": 0,
            "ci": "—",
            "threads": "unknown",
        }

    title = full.get("title") or title
    author = _pr_label_value(full, "author:") or author
    reviewer = (
        _pr_label_value(full, "reviewed-by:")
        or _pr_label_value(full, "reviewer:")
        or reviewer
    )

    issue_nums = linked_issues_fn(full.get("body"))
    if not issue_nums:
        return {
            "pr": number,
            "title": title,
            "author": author,
            "reviewer": reviewer,
            "ok": False,
            "first_blocking": "issue link",
            "verdict": "PR body has no Closes #<issue>",
            "next_action": "wait",
            "gates": [],
            "unresolved_threads": 0,
            "ci": queue_ci_label(full, None),
            "threads": "unknown",
        }

    issue_bodies: Dict[int, str] = {}
    for num in issue_nums:
        try:
            issue_bodies[num] = issue_body_fn(num) or ""
        except Exception as exc:  # fail closed per PR, keep the rest of the queue
            return {
                "pr": number,
                "title": title,
                "author": author,
                "reviewer": reviewer,
                "ok": False,
                "first_blocking": f"accept #{num}",
                "verdict": f"could not read issue #{num}: {exc}",
                "next_action": "wait",
                "gates": [],
                "unresolved_threads": 0,
                "ci": queue_ci_label(full, None),
                "threads": "unknown",
            }

    evidence = review_evidence_fn(number)
    # None means the GraphQL/auth query failed — fail closed for this row.
    # Do not coerce to {} or check_reviews will KeyError on missing keys.
    if evidence is None or evidence.get("error"):
        unresolved: Optional[int] = None
        threads_known = False
        ok = False
        # Keep review as the sole blocking gate so first_blocking stays
        # review-evidence failure; CI still surfaces via queue_ci_label(full).
        gates: List[Any] = [
            ("review", False, "Could not determine review-thread state; refusing rather than guessing."),
        ]
    else:
        unresolved = int(evidence.get("unresolved") or 0)
        threads_known = True
        ok, gates = evaluate_dod_fn(full, issue_bodies, evidence)

    first_blocking = next((name for name, passed, _ in gates if not passed), None)
    if ok:
        verdict = "pass"
    elif first_blocking:
        message = next((m for n, p, m in gates if n == first_blocking and not p), "")
        verdict = f"{first_blocking}: {message}" if message else f"unmet: {first_blocking}"
    else:
        verdict = "blocked"

    return {
        "pr": number,
        "title": title,
        "author": author,
        "reviewer": reviewer,
        "ok": bool(ok),
        "first_blocking": first_blocking,
        "verdict": verdict,
        "next_action": next_queue_action(
            bool(ok), first_blocking, 0 if unresolved is None else unresolved
        ),
        "gates": _serialize_queue_gates(gates),
        "unresolved_threads": 0 if unresolved is None else int(unresolved),
        "ci": queue_ci_label(full, gates),
        "threads": queue_thread_label(unresolved, known=threads_known),
    }


def build_merge_queue(
    prs: Optional[List[Dict[str, Any]]] = None,
    *,
    evaluate_row_fn=None,
    list_prs_fn=None,
) -> Dict[str, Any]:
    """Build the merge-queue payload for every open PR (read-only).

    When ``prs`` is omitted and the open-PR listing fails, returns
    ``queue: None`` with an error reason so callers exit non-zero instead of
    printing a false empty-queue all-clear.
    """
    rows_fn = evaluate_row_fn or evaluate_queue_row
    if prs is not None:
        open_prs = prs
    else:
        listed = (list_prs_fn or list_open_prs_details)()
        if listed is None:
            return {
                "queue": None,
                "open_prs_count": 0,
                "mergeable_count": 0,
                "error": "Could not list open pull requests.",
            }
        open_prs = listed
    rows = [rows_fn(pr) for pr in open_prs]
    return {
        "queue": rows,
        "open_prs_count": len(rows),
        "mergeable_count": sum(1 for row in rows if row.get("ok")),
    }


def format_merge_queue(queue: Dict[str, Any]) -> str:
    """Render a compact CLI table for the merge-queue view."""
    rows = queue.get("queue") or []
    lines = [
        "=== Merge queue (dry-run; no merges) ===",
        f"Open PRs: {queue.get('open_prs_count', len(rows))}  "
        f"Mergeable: {queue.get('mergeable_count', 0)}",
        "",
        f"{'PR':<6} {'Author':<16} {'Reviewer':<16} {'CI':<8} {'Threads':<10} "
        f"{'Next':<10} Verdict",
        f"{'-'*6} {'-'*16} {'-'*16} {'-'*8} {'-'*10} {'-'*10} {'-'*36}",
    ]
    if not rows:
        lines.append("(no open PRs)")
        return "\n".join(lines)
    for row in rows:
        pr = f"#{row.get('pr')}"
        author = (row.get("author") or "—")[:16]
        reviewer = (row.get("reviewer") or "—")[:16]
        ci = (row.get("ci") or "—")[:8]
        threads = (row.get("threads") or "—")[:10]
        action = row.get("next_action") or "wait"
        verdict = row.get("verdict") or ""
        lines.append(
            f"{pr:<6} {author:<16} {reviewer:<16} {ci:<8} {threads:<10} "
            f"{action:<10} {verdict}"
        )
    return "\n".join(lines)


def _with_operator_screen(
    status: Dict[str, Any],
    issues: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    fleet_size: Optional[int] = None,
    ready_target: Optional[int] = None,
    stall: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status["operator_screen"] = build_operator_screen(
        issues, prs, fleet_size=fleet_size, ready_target=ready_target,
    )
    # Deliberately NOT a seventh operator-screen question: that screen is the
    # documented six-question §4.2 contract. Stall is its own top-level key.
    if stall is not None:
        status["stall"] = stall
    return status


def evaluate_fleet_status(
    repo_dir: str = ".",
    fleet_size: Optional[int] = None,
    ready_target: Optional[int] = None,
    stall_hours: float = DEFAULT_STALL_HOURS,
) -> Dict[str, Any]:
    """Calculates authoritative factory state for ``repo_dir``."""
    try:
        target = os.path.abspath(repo_dir)
    except (OSError, TypeError, ValueError) as exc:
        return _error(
            f"Could not resolve repository directory '{repo_dir}': {exc}",
            "ERROR: Invalid repository directory.",
        )
    if not os.path.isdir(target):
        return _error(
            f"Repository directory does not exist: {target}",
            "ERROR: Invalid repository directory.",
        )

    original = os.getcwd()
    try:
        os.chdir(target)
        size = (
            resolve_fleet_size(fleet_size)
            if fleet_size is not None
            else discover_fleet_size(target)
        )
        resolved_target = resolve_ready_target(size, configured=ready_target)
        status = _evaluate_current_repo(
            fleet_size=size, ready_target=resolved_target,
            stall_hours=stall_hours, repo_dir=target,
        )
    except OSError as exc:
        return _error(
            f"Could not evaluate repository directory '{target}': {exc}",
            "ERROR: Could not access repository directory.",
        )
    finally:
        os.chdir(original)

    status["codebase_health"] = collect_codebase_health(target)
    return status


def _evaluate_current_repo(  # noqa: C901, PLR0912, PLR0915
    fleet_size: Optional[int] = None,
    ready_target: Optional[int] = None,
    stall_hours: float = DEFAULT_STALL_HOURS,
    repo_dir: str = ".",
) -> Dict[str, Any]:
    """Calculates state after the caller has selected the repository cwd."""
    slug = get_repo_slug()
    if not slug:
        return _error(
            "Could not determine GitHub repository slug.",
            "ERROR: Unable to resolve repository slug.",
        )

    projects = get_repo_projects(slug)
    if projects is None:
        return _error(
            "Failed to list project boards from GitHub API.",
            "ERROR: Could not query project boards.",
        )
    governed_projects = select_governed_projects(projects, slug)
    if len(governed_projects) == 0:
        return {
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": [f"No governed project board found for repository '{slug}'."],
            "summary": "BLOCKED: Missing governed project board.",
        }
    if len(governed_projects) > 1:
        titles = [p.get("title", "") for p in governed_projects]
        return {
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": [f"Ambiguous governed project boards for '{slug}': {titles}"],
            "summary": "BLOCKED: Multiple project boards match governance title.",
        }

    governed_board = governed_projects[0]
    board_title = governed_board.get("title", "")

    issues = query_open_issues()
    if issues is None:
        return _error(
            "Failed to list open issues from GitHub API.",
            "ERROR: Could not query open issues.",
        )

    prs = list_open_prs_details()
    if prs is None:
        return _error(
            "Failed to list open pull requests from GitHub API.",
            "ERROR: Could not query open pull requests.",
        )

    worktree_branches = list_worktree_branches()

    blocked_reasons: List[str] = []
    waiting_reasons: List[str] = []
    drifted_issues: List[int] = []
    orphan_issues: List[int] = []
    active_claims: List[Dict[str, Any]] = []

    # Evaluate Issues
    for issue in issues:
        num = issue["number"]
        labels = set(label_names(issue))

        holder = claimed_by(issue)
        if holder:
            active_claims.append({"type": "issue", "number": num, "agent": holder})

        # Board drift check
        items = query_issue_project_items(num)
        if items is None:
            return _error(
                f"Failed to query project-board items for issue #{num}.",
                "ERROR: Could not query issue project-board state.",
            )
        gov_items = select_governed_project_items(items, slug)
        if not gov_items:
            orphan_issues.append(num)
            waiting_reasons.append(f"Issue #{num} is open but not on board '{board_title}'.")
        else:
            status_value = gov_items[0].get("status") or {}
            board_status = status_value.get("name")
            if not board_status:
                p_field = (gov_items[0].get("project") or {}).get("field") or {}
                board_options = {
                    o["id"]: o["name"] for o in p_field.get("options", [])
                }
                board_status = board_options.get(gov_items[0].get("statusOptionId"))
            # Label vs Board alignment check
            current_status_label = next(
                (name.replace("status:", "") for name in labels if name.startswith("status:")),
                None,
            )
            normalized_board_status = (board_status or "").lower().replace(" ", "-")
            if current_status_label and normalized_board_status and current_status_label != normalized_board_status:
                drifted_issues.append(num)
                waiting_reasons.append(f"Issue #{num} status label ('{current_status_label}') drifts from board status ('{board_status}').")

        if "status:ready" in labels:
            waiting_reasons.append(f"Issue #{num} is Ready for implementation.")
        elif "status:in-progress" in labels:
            waiting_reasons.append(f"Issue #{num} is In Progress ({holder or 'unassigned'}).")
        elif "status:in-review" in labels:
            waiting_reasons.append(f"Issue #{num} is In Review.")
        elif "status:backlog" in labels:
            waiting_reasons.append(f"Issue #{num} is in Backlog.")

    # Evaluate PRs
    for pr in prs:
        num = pr["number"]
        labels = set(label_names(pr))
        decision = (pr.get("reviewDecision") or "").upper()
        merge_state = (pr.get("mergeStateStatus") or "").upper()

        reviewer_label = next(
            (name.replace("reviewer:", "") for name in labels if name.startswith("reviewer:")),
            None,
        )
        if reviewer_label:
            active_claims.append({"type": "review", "number": num, "agent": reviewer_label})

        if "needs-human-review" in labels and merge_state == "DIRTY":
            blocked_reasons.append(
                f"PR #{num} has an escalated severe merge conflict that agents could not resolve."
            )

        if decision == "CHANGES_REQUESTED":
            waiting_reasons.append(f"PR #{num} has requested changes.")
        elif decision == "APPROVED":
            waiting_reasons.append(f"PR #{num} is approved and waiting for merge.")
        else:
            waiting_reasons.append(f"PR #{num} is open and pending review.")

    # Worktrees check
    for branch in worktree_branches:
        m = re.search(r"issue-(\d+)", branch, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if not any(i["number"] == num for i in issues):
                waiting_reasons.append(f"Worktree branch '{branch}' exists for closed/merged issue #{num}.")

    # Stall is a fleet-level condition: no per-agent branch can observe zero
    # global throughput, because every waiting agent is locally in a valid state.
    newest_merge, merge_lookup_ok = most_recent_merge_history()
    hours_since_last_merge = (
        _hours_ago(newest_merge, datetime.now(timezone.utc)) if merge_lookup_ok else None
    )
    stall = _stall_question(
        hours_since_last_merge,
        len(prs),
        registered_agent_count(repo_dir),
        stall_hours,
        merge_lookup_ok=merge_lookup_ok,
    )
    open_pr_numbers = [int(pr["number"]) for pr in prs if pr.get("number") is not None]

    # Calculate overall state
    if blocked_reasons:
        return _with_operator_screen({
            "state": "blocked",
            "exit_code": EXIT_BLOCKED,
            "reasons": blocked_reasons,
            "summary": f"BLOCKED: {len(blocked_reasons)} condition(s) require escalation.",
            "project_board": {"title": board_title, "id": governed_board.get("id")},
            "open_issues_count": len(issues),
            "open_prs_count": len(prs),
            "active_claims": active_claims,
            "orphans": orphan_issues,
            "drifted": drifted_issues,
            "open_pr_numbers": open_pr_numbers,
        }, issues, prs, fleet_size=fleet_size, ready_target=ready_target, stall=stall)

    if stall.get("stalled"):
        return _with_operator_screen({
            "state": "stalled",
            "exit_code": EXIT_STALLED,
            "reasons": [stall["summary"]] + waiting_reasons,
            "summary": f"STALLED: {stall['summary']}",
            "hours_since_last_merge": hours_since_last_merge,
            "project_board": {"title": board_title, "id": governed_board.get("id")},
            "open_issues_count": len(issues),
            "open_prs_count": len(prs),
            "active_claims": active_claims,
            "orphans": orphan_issues,
            "drifted": drifted_issues,
            "open_pr_numbers": open_pr_numbers,
        }, issues, prs, fleet_size=fleet_size, ready_target=ready_target, stall=stall)

    if waiting_reasons or issues or prs:
        return _with_operator_screen({
            "state": "waiting",
            "exit_code": EXIT_WAITING,
            "reasons": waiting_reasons,
            "summary": f"WAITING: {len(issues)} open issue(s), {len(prs)} open PR(s).",
            "hours_since_last_merge": hours_since_last_merge,
            "project_board": {"title": board_title, "id": governed_board.get("id")},
            "open_issues_count": len(issues),
            "open_prs_count": len(prs),
            "active_claims": active_claims,
            "orphans": orphan_issues,
            "drifted": drifted_issues,
            "open_pr_numbers": open_pr_numbers,
        }, issues, prs, fleet_size=fleet_size, ready_target=ready_target, stall=stall)

    return _with_operator_screen({
        "state": "complete",
        "exit_code": EXIT_COMPLETE,
        "reasons": [],
        "summary": "COMPLETE: All governed issues closed, zero open PRs, zero active claims.",
        "project_board": {"title": board_title, "id": governed_board.get("id")},
        "open_issues_count": 0,
        "open_prs_count": 0,
        "active_claims": [],
        "orphans": [],
        "drifted": [],
    }, issues, prs, fleet_size=fleet_size, ready_target=ready_target, stall=stall)


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Evaluate factory fleet completion state.")
    parser.add_argument("--json", action="store_true", help="Output state in JSON format")
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Show merge-queue view (per-PR merge_pr --dry-run verdicts; never merges)",
    )
    parser.add_argument(
        "--queue-json",
        action="store_true",
        help="Emit merge-queue view as JSON (implies --queue; never merges)",
    )
    parser.add_argument("--repo-dir", default=".", help="Repository working directory")
    parser.add_argument("--metrics", action="store_true", help="Include opt-in closed-issue cost/cycle metrics")
    parser.add_argument("--metrics-window-days", type=int, default=30, help="Closed-issue metrics window")
    parser.add_argument("--metrics-usage-file", help="Optional measured local CLI usage JSON/JSONL")
    parser.add_argument(
        "--fleet-size", type=int, default=None,
        help="Configured/launched agent count (or set ARU_FLEET_SIZE)",
    )
    parser.add_argument(
        "--ready-target", type=int, default=None,
        help="Configured Ready depth target (or set ARU_READY_TARGET)",
    )
    parser.add_argument(
        "--stall-hours", type=float, default=DEFAULT_STALL_HOURS,
        help="Hours without a merge before reporting a stall (0 disables)",
    )
    parser.add_argument(
        "--stall-agent", default="fleet-status",
        help="Agent id used for the stall Slack alert",
    )
    parser.add_argument(
        "--stall-family", default="human",
        help="Model family used for the stall Slack alert",
    )
    args = parser.parse_args()

    if args.queue or args.queue_json:
        try:
            target = os.path.abspath(args.repo_dir)
        except (OSError, TypeError, ValueError) as exc:
            print(f"[ERROR] Could not resolve repository directory '{args.repo_dir}': {exc}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        if not os.path.isdir(target):
            print(f"[ERROR] Repository directory does not exist: {target}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        previous = os.getcwd()
        try:
            os.chdir(target)
            queue = build_merge_queue()
        except OSError as exc:
            print(f"[ERROR] Could not access repository directory '{target}': {exc}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        finally:
            os.chdir(previous)
        if queue.get("queue") is None:
            reason = queue.get("error") or "Could not build merge queue."
            if args.queue_json or args.json:
                print(json.dumps(queue, indent=2))
            else:
                print(f"[ERROR] {reason}", file=sys.stderr)
            sys.exit(EXIT_ERROR)
        if args.queue_json or args.json:
            print(json.dumps(queue, indent=2))
        else:
            print(format_merge_queue(queue))
        sys.exit(EXIT_COMPLETE)

    status = evaluate_fleet_status(
        args.repo_dir,
        fleet_size=resolve_fleet_size(args.fleet_size),
        ready_target=args.ready_target,
        stall_hours=args.stall_hours,
    )
    if status.get("state") == "stalled":
        # GitHub first, Slack second, and a Slack failure never changes the
        # exit code -- the loop must not halt because the control room is down.
        # Stall lives at status["stall"], not on the six-question operator screen.
        question = status.get("stall")
        if question:
            blocked = _stall_pr_reasons(status.get("reasons"))
            notify_stall(
                question,
                blocked,
                args.repo_dir, args.stall_agent, args.stall_family,
                pr=_stall_github_pr(status, blocked),
            )
    if status.get("operator_screen") and status.get("state") != "error":
        ci_error = None
        ci_runs = None
        try:
            if args.metrics_window_days <= 0:
                raise RuntimeError("--metrics-window-days must be positive.")
            ci_runs = fetch_ci_history(args.metrics_window_days, args.repo_dir)
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            ci_error = str(exc)
        apply_ci_failure_rate(status.get("operator_screen"), ci_runs, ci_error)
    if args.metrics and status.get("state") != "error":
        closed = None
        metrics_error = None
        try:
            if args.metrics_window_days <= 0:
                raise RuntimeError("--metrics-window-days must be positive.")
            from factory_metrics import collect_factory_metrics
            closed = collect_factory_metrics(
                args.metrics_window_days,
                args.metrics_usage_file,
                repo_dir=args.repo_dir,
            )["closed_issues"]
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            metrics_error = str(exc)
        apply_closed_issue_cost(status.get("operator_screen"), closed, metrics_error)
        if closed is not None:
            status["factory_metrics"] = closed
        else:
            reason = f"Closed-issue metrics unavailable: {metrics_error}"
            status.setdefault("reasons", []).append(reason)
            status["factory_metrics_error"] = reason

    if args.json:
        print(json.dumps(status, indent=2))
        sys.exit(status["exit_code"])

    print("=== Aru_Agentic_SDLC: Factory Fleet Status ===")
    print(f"State: {status['state'].upper()}")
    print(f"Summary: {status['summary']}")
    if status.get("operator_screen"):
        print(format_operator_screen(status["operator_screen"]))
    stall = status.get("stall")
    if stall:
        marker = "[ATTN]" if stall.get("stalled") else "[OK]  "
        print(f"\n{marker} {stall['title']}: {stall['summary']}")
    if status.get("reasons"):
        print("\nDetails:")
        for r in status["reasons"]:
            print(f"  • {r}")
    health = status.get("codebase_health")
    if health:
        print("\nCodebase health:")
        print(f"  LOC: {health['loc']}")
        print(f"  files: {health['file_count']}")
        print(f"  average file size: {health['average_file_bytes']} bytes")
        over = health["files_at_or_over_ceiling"]
        print(f"  files at/over {health['line_ceiling']} lines: {len(over)}")
        for item in over[:20]:
            print(f"    • {item['path']} ({item['lines']})")
    metrics = status.get("factory_metrics")
    if metrics:
        cost = metrics["cost_per_closed_issue"]
        measured = cost["average_usd_measured"]
        cost_text = f"${measured:.6f}" if measured is not None else "unavailable"
        print("\nClosed-issue metrics:")
        print(f"  window: {metrics['window_days']} days")
        print(f"  closed issues: {metrics['closed_issue_count']}")
        print(f"  measured cost per closed issue: {cost_text}")
        print(f"  outliers: {metrics['outlier_issue_numbers'] or 'none'}")
    sys.exit(status["exit_code"])


if __name__ == "__main__":
    main()
