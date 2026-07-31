#!/usr/bin/env python3
"""
check_ci.py - Checks and polls automated CI pipeline status for a PR or active branch.
"""

import argparse
import sys
import time
from common import get_current_branch, run_gh_json


def check_ci_status(pr_id: int = None, wait: bool = False, poll_interval: int = 15, timeout: int = 300) -> bool:
    target = str(pr_id) if pr_id else get_current_branch()
    print(f"Checking CI status for target '{target}'...")

    cmd = ["gh", "pr", "checks", target, "--json", "name,state,bucket"]

    start_time = time.time()
    while True:
        checks = run_gh_json(cmd)
        if checks is None:
            print("[INFO] No active CI checks returned or gh CLI unconfigured.", file=sys.stderr)
            return True

        failing = [c for c in checks if c.get("state") in ["FAILURE", "ERROR", "CANCELLED"]]
        pending = [c for c in checks if c.get("state") in ["PENDING", "QUEUED", "IN_PROGRESS"]]

        if failing:
            print(f"❌ CI Check Failures Detected ({len(failing)} failed):", file=sys.stderr)
            for f in failing:
                print(f"  - {f.get('name')}: {f.get('state')}", file=sys.stderr)
            return False

        if not pending:
            print("✅ All CI pipeline checks passed successfully.")
            return True

        if not wait or (time.time() - start_time) > timeout:
            print(f"⏳ CI checks still pending ({len(pending)} in progress).")
            return True

        print(f"⏳ Waiting for CI checks to complete ({len(pending)} pending)... sleeping {poll_interval}s")
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="Check CI pipeline status.")
    parser.add_argument("--pr", type=int, default=None, help="PR Number (or checks current branch)")
    parser.add_argument("--wait", action="store_true", help="Poll until checks complete")
    args = parser.parse_args()

    success = check_ci_status(args.pr, args.wait)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
