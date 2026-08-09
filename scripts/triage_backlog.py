#!/usr/bin/env python3
"""triage_backlog.py - promotes Backlog issues to Ready, and sizes the fleet.

Triage is the throughput cap nobody owns. The picker cannot hand out a Backlog
issue, so a fleet drains the Ready column and then idles while a human is
asleep. This script does the mechanical half of triage - verifying the Ready
contract - and reports exactly what each blocked issue is missing so the human
half is a two-minute job instead of an archaeology session.

  python3 triage_backlog.py                # report only
  python3 triage_backlog.py --promote      # promote everything that qualifies
  python3 triage_backlog.py --promote --issue 24 --issue 25
  python3 triage_backlog.py --capacity     # just the fleet-size answer

The Ready contract (all four required):
  * not an epic
  * acceptance criteria present, as checkboxes
  * a touches: declaration
  * every depends-on issue is closed
"""

import argparse
import re
import sys
from typing import Any

from common import (
    claimed_by,
    label_names,
    list_open_issues,
    parse_touches,
    touches_conflict,
)
from fetch_next_issue import is_epic, parse_dependencies
from update_issue_status import update_status


def acceptance_criteria(body: str) -> list[str]:
    """Returns checkbox lines under the Acceptance Criteria heading.

    Requires the heading. A bare list of checkboxes elsewhere in the body is
    not acceptance criteria - it is usually a task list the author left behind,
    and treating it as criteria would promote under-specified issues.
    """
    if not body:
        return []
    parts = re.split(
        r"^\s*#{1,4}\s*acceptance criteria\s*$", body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(parts) < 2:
        return []
    tail = re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]
    return [ln.strip() for ln in tail.splitlines() if re.match(r"^\s*[-*]\s*\[[ xX]\]", ln)]


def has_verification(body: str) -> bool:
    """A Verification section is what makes an acceptance criterion checkable."""
    if not body:
        return False
    parts = re.split(
        r"^\s*#{1,4}\s*verification\s*$", body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(parts) < 2:
        return False
    tail = re.split(r"^\s*#{1,4}\s+", parts[1], flags=re.MULTILINE)[0]
    return bool(tail.strip())


def ready_gaps(issue: dict[str, Any], open_numbers: set) -> list[str]:
    """Returns the list of unmet Ready-contract elements. Empty means ready."""
    body = issue.get("body") or ""
    gaps = []

    if is_epic(issue.get("labels", [])):
        gaps.append("is an epic (never directly implementable)")
        return gaps

    if not acceptance_criteria(body):
        gaps.append("no acceptance criteria checkboxes")
    if not has_verification(body):
        gaps.append("no verification section")
    if not parse_touches(body):
        gaps.append("no touches: declaration")

    unresolved = [d for d in parse_dependencies(body) if d in open_numbers]
    if unresolved:
        gaps.append("depends-on still open: " + ", ".join(f"#{d}" for d in unresolved))

    return gaps


def partition(issues: list[dict[str, Any]]) -> tuple[list, list, list]:
    """Splits open issues into (backlog, ready, held)."""
    backlog, ready, held = [], [], []
    for issue in issues:
        names = {n.lower() for n in label_names(issue)}
        if claimed_by(issue):
            held.append(issue)
        elif "status:ready" in names:
            ready.append(issue)
        elif "status:backlog" in names:
            backlog.append(issue)
    return backlog, ready, held


def capacity(ready: list[dict[str, Any]], held: list[dict[str, Any]]) -> dict[str, Any]:
    """How many agents this board can actually keep busy right now.

    Ready count alone overstates it: two Ready issues whose touches overlap
    cannot run at the same time, and neither can one that collides with work
    already in flight. This walks the list greedily the way the picker would.
    """
    in_flight_paths: list[str] = []
    for issue in held:
        in_flight_paths.extend(parse_touches(issue.get("body") or ""))

    concurrent, deferred = [], []
    for issue in sorted(ready, key=lambda i: i["number"]):
        paths = parse_touches(issue.get("body") or "")
        if not paths:
            deferred.append((issue["number"], "no touches declaration"))
            continue
        clash = touches_conflict(paths, in_flight_paths)
        if clash:
            deferred.append((issue["number"], f"path conflict on {clash[0]}"))
            continue
        concurrent.append(issue["number"])
        in_flight_paths.extend(paths)

    return {"concurrent": concurrent, "deferred": deferred, "ready_total": len(ready)}


def print_capacity(cap: dict[str, Any], held: list[dict[str, Any]]) -> None:
    n = len(cap["concurrent"])
    print("\n=== Fleet capacity ===")
    print(f"  Ready issues:            {cap['ready_total']}")
    print(f"  Claimable simultaneously: {n}  {cap['concurrent']}")
    if cap["deferred"]:
        print("  Deferred this moment:")
        for num, why in cap["deferred"]:
            print(f"    #{num}: {why}")
    if held:
        holders = ", ".join(f"#{i['number']}({claimed_by(i)})" for i in held)
        print(f"  In flight:               {holders}")
    if n == 0:
        print("\n  → Launch nothing. Triage first.")
    else:
        print(f"\n  → Launch at most {n} agent(s). More will idle.")


def main():
    parser = argparse.ArgumentParser(description="Verify the Ready contract and promote Backlog issues.")
    parser.add_argument("--promote", action="store_true", help="Promote qualifying issues to Ready")
    parser.add_argument("--issue", type=int, action="append", default=[],
                        help="Restrict to specific issue numbers (repeatable)")
    parser.add_argument("--capacity", action="store_true", help="Print only the fleet-capacity summary")
    args = parser.parse_args()

    issues = list_open_issues()
    if not issues:
        print("[ERROR] No open issues returned; is gh authenticated in this repo?", file=sys.stderr)
        return 1
    open_numbers = {i["number"] for i in issues}
    backlog, ready, held = partition(issues)

    if args.capacity:
        print_capacity(capacity(ready, held), held)
        return 0

    if args.issue:
        backlog = [i for i in backlog if i["number"] in args.issue]

    qualified, blocked = [], []
    for issue in sorted(backlog, key=lambda i: i["number"]):
        gaps = ready_gaps(issue, open_numbers)
        (blocked if gaps else qualified).append((issue, gaps))

    print(f"=== Backlog triage — {len(backlog)} issue(s) examined ===\n")
    if qualified:
        print("Meets the Ready contract:")
        for issue, _ in qualified:
            print(f"  ✅ #{issue['number']:<4} {issue['title']}")
    if blocked:
        print("\nBlocked — needs a human:")
        for issue, gaps in blocked:
            print(f"  ❌ #{issue['number']:<4} {issue['title']}")
            for gap in gaps:
                print(f"        · {gap}")

    promoted = 0
    if args.promote and qualified:
        print()
        for issue, _ in qualified:
            if update_status(issue["number"], "Ready"):
                print(f"  ⬆️  #{issue['number']} → Ready")
                promoted += 1
            else:
                print(f"  [WARN] #{issue['number']} could not be promoted", file=sys.stderr)
        # Re-read so capacity reflects the promotions we just made.
        ready = partition(list_open_issues())[1]
    elif qualified:
        print(f"\n  {len(qualified)} issue(s) would be promoted. Re-run with --promote.")

    print_capacity(capacity(ready, held), held)
    return 0


if __name__ == "__main__":
    sys.exit(main())
