#!/usr/bin/env python3
"""
check_ci.py - Checks and polls automated CI pipeline status for a PR or active branch.
"""

import argparse
import sys
import time
from common import get_current_branch, get_repo_slug, run_gh_json


FAILED_STATES = {
    "FAILURE", "FAILED", "ERROR", "CANCELLED", "TIMED_OUT",
    "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE",
}
PENDING_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
PASSING_STATES = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _pull_head(target: str) -> str | None:
    """Resolve a PR head through REST without touching GraphQL quota."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    if target.isdigit():
        payload = run_gh_json(["gh", "api", f"repos/{slug}/pulls/{target}"])
    else:
        owner = slug.split("/", 1)[0]
        rows = run_gh_json([
            "gh", "api", "--method", "GET", f"repos/{slug}/pulls",
            "-f", "state=open", "-f", f"head={owner}:{target}", "-f", "per_page=100",
        ])
        payload = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    head = (payload or {}).get("head") if isinstance(payload, dict) else None
    sha = (head or {}).get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _ci_contexts(head_sha: str) -> list[dict] | None:
    """Return check-run and commit-status contexts from their REST endpoints."""
    slug = get_repo_slug()
    if not slug:
        return None
    checks = run_gh_json([
        "gh", "api", f"repos/{slug}/commits/{head_sha}/check-runs?per_page=100",
    ])
    statuses = run_gh_json([
        "gh", "api", f"repos/{slug}/commits/{head_sha}/status?per_page=100",
    ])
    if not isinstance(checks, dict) or not isinstance(statuses, dict):
        return None
    check_runs = checks.get("check_runs")
    status_rows = statuses.get("statuses")
    check_total = checks.get("total_count")
    status_total = statuses.get("total_count")
    if (
        not isinstance(check_runs, list)
        or not isinstance(status_rows, list)
        or type(check_total) is not int
        or type(status_total) is not int
        or check_total != len(check_runs)
        or status_total != len(status_rows)
    ):
        # More than 100 checks is uncommon; silently truncating would let a
        # failure on the next page disappear from the merge gate.
        return None
    contexts: list[dict] = []
    for check in check_runs:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            return None
        status = str(check.get("status") or "").upper()
        state = str(check.get("conclusion") or status).upper()
        contexts.append({"name": check["name"], "state": state})
    for status in status_rows:
        if not isinstance(status, dict) or not isinstance(status.get("context"), str):
            return None
        contexts.append({
            "name": status["context"],
            "state": str(status.get("state") or "").upper(),
        })
    return contexts


def check_ci_status(pr_id: int = None, wait: bool = False, poll_interval: int = 15, timeout: int = 300) -> bool:
    target = str(pr_id) if pr_id else get_current_branch()
    print(f"Checking CI status for target '{target}'...")

    head_sha = _pull_head(target)
    if not head_sha:
        print("[ERROR] Could not resolve the pull request head through GitHub REST.", file=sys.stderr)
        return False

    start_time = time.time()
    interval = max(1, poll_interval)
    while True:
        checks = _ci_contexts(head_sha)
        if checks is None:
            print("[ERROR] Could not read a complete CI status snapshot.", file=sys.stderr)
            return False

        failing = [c for c in checks if c.get("state") in FAILED_STATES]
        pending = [c for c in checks if c.get("state") in PENDING_STATES]
        unknown = [
            c for c in checks
            if c.get("state") not in FAILED_STATES | PENDING_STATES | PASSING_STATES
        ]

        if failing:
            print(f"❌ CI Check Failures Detected ({len(failing)} failed):", file=sys.stderr)
            for f in failing:
                print(f"  - {f.get('name')}: {f.get('state')}", file=sys.stderr)
            return False

        if unknown:
            print("[ERROR] CI returned an unknown or incomplete state:", file=sys.stderr)
            for item in unknown:
                print(f"  - {item.get('name')}: {item.get('state') or '<missing>'}", file=sys.stderr)
            return False

        if checks and not pending:
            print("✅ All CI pipeline checks passed successfully.")
            return True

        elapsed = time.time() - start_time
        if not wait:
            label = "not started" if not checks else f"still pending ({len(pending)} in progress)"
            print(f"⏳ CI checks {label}.")
            return True
        if elapsed >= timeout:
            print("❌ Timed out before CI produced a complete passing result.", file=sys.stderr)
            return False

        pending_label = len(pending) if checks else 0
        sleep_for = min(interval, max(0, timeout - elapsed))
        print(f"⏳ Waiting for CI checks to complete ({pending_label} pending)... sleeping {sleep_for:.0f}s")
        time.sleep(sleep_for)
        # CI normally takes minutes.  Backing off caps a five-minute wait at
        # roughly eight REST snapshots instead of twenty-one GraphQL polls.
        interval = min(60, interval * 2)


def main():
    parser = argparse.ArgumentParser(description="Check CI pipeline status.")
    parser.add_argument("--pr", type=int, default=None, help="PR Number (or checks current branch)")
    parser.add_argument("--wait", action="store_true", help="Poll until checks complete")
    args = parser.parse_args()

    success = check_ci_status(args.pr, args.wait)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
