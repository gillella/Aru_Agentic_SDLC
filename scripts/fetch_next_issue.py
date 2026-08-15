#!/usr/bin/env python3
"""
fetch_next_issue.py - Selects the next actionable issue for one agent.

Multi-agent safe. Three things make it so:

  * Issues already held by another agent are excluded, so two agents are never
    handed the same work. (The previous version filtered only on --state open,
    so a claimed, in-progress issue was still returned as "next".)
  * Candidates whose `touches:` paths overlap work already in flight are
    excluded. `parallel-eligible` only means "no unresolved depends-on"; it
    says nothing about two agents editing the same file.
  * --claim walks the candidate list and takes the first issue it can claim,
    so a lost race costs one retry rather than duplicated work.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from common import (
    agent_labels,
    claimed_by,
    get_current_branch,
    list_open_issues,
    parse_touches,
    run_cmd,
    touches_conflict,
)
from update_issue_status import update_status


def parse_dependencies(body: str) -> List[int]:
    """Parses 'depends-on: #12, #14' pattern from issue body."""
    if not body:
        return []
    match = re.search(
        r"^\s*depends-on\s*:\s*(.*?)\s*$",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return []
    return [int(d) for d in re.findall(r"#(\d+)", match.group(1))]


def is_epic(labels: List[Dict[str, Any]]) -> bool:
    """Epics are phase-level containers, never directly implementable.

    They carry no depends-on, so without this filter they are always
    'unblocked' and always the lowest-numbered candidate - meaning the picker
    would hand an agent an epic every single time.
    """
    return "type:epic" in [label.get("name", "").lower() for label in labels]


def needs_human(labels: List[Dict[str, Any]]) -> bool:
    """Operator-only issues are visible board work, never factory work."""
    return "needs-human" in [label.get("name", "").lower() for label in labels]


def is_parallel_eligible(body: str, labels: List[Dict[str, Any]]) -> bool:
    """Checks if issue is flagged as parallel-eligible."""
    names = [label.get("name", "").lower() for label in labels]
    if "parallel-eligible" in names or "independent" in names:
        return True
    return bool(body) and "parallel-eligible: true" in body.lower()


def reap_stale_claims(issues: List[Dict[str, Any]], hours: int) -> List[int]:
    """Releases claims that have gone quiet.

    An agent that crashes mid-issue leaves it In Progress forever, and once
    claimed issues are excluded from selection nothing would ever pick it up
    again. An issue is stale when it has been untouched for `hours`, has no
    open PR, and has no remote branch carrying commits.
    """
    if hours <= 0:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pr_code, prs, pr_err = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200",
         "--json", "number,body,headRefName"],
        check=False,
    )
    if pr_code != 0:
        print(f"[WARN] Could not verify open PRs; no claims were reaped: {pr_err}", file=sys.stderr)
        return []
    try:
        open_prs = json.loads(prs) if prs else []
    except json.JSONDecodeError:
        print("[WARN] Could not parse open PRs; no claims were reaped.", file=sys.stderr)
        return []

    branch_code, remote_branches, branch_err = run_cmd(
        ["git", "ls-remote", "--heads", "origin"],
        check=False,
    )
    if branch_code != 0:
        print(
            f"[WARN] Could not verify remote branches; no claims were reaped: {branch_err}",
            file=sys.stderr,
        )
        return []

    def has_open_pr(num: int) -> bool:
        pat = re.compile(rf"closes\s+#{num}\b", re.IGNORECASE)
        return any(pat.search(p.get("body") or "") or f"issue-{num}-" in p.get("headRefName", "")
                   for p in open_prs)

    def has_remote_branch(num: int) -> bool:
        return f"issue-{num}-" in remote_branches

    released = []
    for issue in issues:
        num = issue["number"]
        if not agent_labels(issue):
            continue
        updated = issue.get("updatedAt")
        if not updated:
            continue
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts > cutoff or has_open_pr(num) or has_remote_branch(num):
            continue

        target_status = "Backlog" if needs_human(issue.get("labels", [])) else "Ready"
        if not update_status(num, target_status, require_board=True):
            print(
                f"[WARN] Could not synchronize #{num} to {target_status}; claim retained.",
                file=sys.stderr,
            )
            continue

        cmd = ["gh", "issue", "edit", str(num), "--remove-assignee", "@me"]
        for lbl in agent_labels(issue):
            cmd += ["--remove-label", lbl]
        if run_cmd(cmd, check=False)[0] == 0:
            released.append(num)
            print(f"♻️  Released stale claim on #{num} (idle > {hours}h, no PR, no branch).",
                  file=sys.stderr)
        else:
            update_status(num, "In Progress", require_board=True)
            print(
                f"[WARN] Could not remove the claim markers on #{num}; status restored.",
                file=sys.stderr,
            )
    return released


def build_candidates(issues: List[Dict[str, Any]], agent: Optional[str]) -> Dict[str, Any]:
    """Partitions open issues into in-flight, blocked, and claimable."""
    open_numbers = {i["number"] for i in issues}

    in_flight_paths: List[str] = []
    my_in_flight_issues: List[Dict[str, Any]] = []

    for issue in issues:
        labels = issue.get("labels", [])
        if needs_human(labels):
            continue
        names = {label.get("name", "").lower() for label in labels}
        holder = claimed_by(issue)
        if holder or "status:in-progress" in names or "status:in-review" in names:
            in_flight_paths.extend(parse_touches(issue.get("body") or ""))
        if holder:
            if agent and holder == agent:
                my_in_flight_issues.append(issue)

    candidates, blocked, conflicted, not_ready, missing_touches = [], [], [], [], []
    operator_only = []

    for issue in issues:
        num = issue["number"]
        body = issue.get("body") or ""
        labels = issue.get("labels", [])
        names = {label.get("name", "").lower() for label in labels}

        if needs_human(labels):
            operator_only.append(num)
            continue
        if is_epic(labels):
            continue
        if claimed_by(issue) or "status:in-progress" in names or "status:in-review" in names:
            continue  # held or in flight; not claimable as new implementation
        if "status:ready" not in names:
            not_ready.append(num)
            continue

        unresolved = [d for d in parse_dependencies(body) if d in open_numbers]
        if unresolved:
            blocked.append({"number": num, "blocked_by": unresolved})
            continue

        my_paths = parse_touches(body)
        if not my_paths:
            missing_touches.append(num)
            continue
        clash = touches_conflict(my_paths, in_flight_paths) if my_paths else None
        if clash:
            conflicted.append({"number": num, "conflict": list(clash)})
            continue

        candidates.append(issue)

    candidates.sort(key=lambda x: x["number"])
    return {
        "candidates": candidates,
        "blocked": blocked,
        "conflicted": conflicted,
        "not_ready": not_ready,
        "missing_touches": missing_touches,
        "operator_only": operator_only,
        "my_in_flight": min(my_in_flight_issues, key=lambda x: x["number"])
        if my_in_flight_issues else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch the next actionable issue for one agent.")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")
    parser.add_argument("--agent", type=str, default=None,
                        help="Agent id. Returns this agent's in-flight issue first (resume).")
    parser.add_argument("--claim", action="store_true",
                        help="Claim the first candidate that can be claimed. Requires --agent.")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release claims idle longer than HOURS with no PR and no branch.")
    args = parser.parse_args()

    if args.claim and not args.agent:
        print("[ERROR] --claim requires --agent.", file=sys.stderr)
        sys.exit(1)

    issues = list_open_issues()

    if args.reap_after:
        if reap_stale_claims(issues, args.reap_after):
            issues = list_open_issues()

    parts = build_candidates(issues, args.agent)
    candidates = parts["candidates"]
    my_in_flight = parts["my_in_flight"]

    # Session resume: a worktree branch named .../issue-<N>-... wins over
    # anything else only when that issue is still open and still claimed by
    # this agent. A retained worktree after merge/close/release must not block
    # claiming available work.
    current_branch = get_current_branch()
    branch_issue = None
    m = re.search(r"issue-(\d+)", current_branch, re.IGNORECASE)
    if m:
        branch_num = int(m.group(1))
        match = next((i for i in issues if i["number"] == branch_num), None)
        holder = claimed_by(match) if match else None
        operator_only = bool(match and needs_human(match.get("labels", [])))
        if match and args.agent and holder == args.agent and not operator_only:
            branch_issue = branch_num
        else:
            if not match:
                reason = "not open"
            elif operator_only:
                reason = "operator-only (needs-human)"
            elif not args.agent:
                reason = "no --agent to validate ownership"
            elif holder and holder != args.agent:
                reason = f"held by '{holder}'"
            else:
                reason = "not claimed by this agent"
            print(
                f"[INFO] Branch references #{branch_num} but it is {reason}; "
                "ignoring stale resume.",
                file=sys.stderr,
            )

    resume = branch_issue or (my_in_flight["number"] if my_in_flight else None)

    claimed_now = None
    if args.claim:
        if resume:
            claimed_now = resume
            print(f"[INFO] Resuming your in-flight issue #{resume}; not claiming new work.")
        else:
            from claim_issue import EXIT_CONFLICT, EXIT_OK, claim_issue

            # After a lost race, another agent's newly claimed touches: may
            # invalidate later candidates from this snapshot. Rebuild before
            # each retry so overlapping work is not claimed from stale data.
            attempted = set()
            while True:
                parts = build_candidates(issues, args.agent)
                candidates = parts["candidates"]
                remaining = [c for c in candidates if c["number"] not in attempted]
                if not remaining:
                    print("[INFO] No claimable issue available.", file=sys.stderr)
                    break
                cand = remaining[0]
                attempted.add(cand["number"])
                rc = claim_issue(cand["number"], args.agent)
                if rc == EXIT_OK:
                    claimed_now = cand["number"]
                    break
                if rc == EXIT_CONFLICT:
                    print(
                        f"[INFO] #{cand['number']} unavailable; rebuilding candidates.",
                        file=sys.stderr,
                    )
                    issues = list_open_issues()
                    continue
                print(
                    f"[ERROR] Claiming #{cand['number']} failed; aborting candidate walk.",
                    file=sys.stderr,
                )
                break

    next_issue = candidates[0] if candidates else None
    res = {
        "agent": args.agent,
        "session_branch": current_branch,
        "resumable_in_flight_issue": resume,
        "claimed_now": claimed_now,
        "next_progressive_issue": next_issue["number"] if next_issue else None,
        "next_issue_title": next_issue["title"] if next_issue else None,
        "total_open_issues": len(issues),
        "claimable_now": [i["number"] for i in candidates],
        "parallel_eligible_issues": [
            i["number"] for i in candidates
            if is_parallel_eligible(i.get("body") or "", i.get("labels", []))
        ],
        "blocked_by_dependencies": parts["blocked"],
        "blocked_by_file_conflict": parts["conflicted"],
        "not_ready": parts["not_ready"],
        "missing_touches": parts["missing_touches"],
        "operator_only_issues": parts["operator_only"],
        "held_by_other_agents": [
            {"number": i["number"], "agent": claimed_by(i)}
            for i in issues if claimed_by(i) and claimed_by(i) != args.agent
        ],
    }

    if args.json:
        print(json.dumps(res, indent=2))
        return

    print("=== Aru_Agentic_SDLC: Issue Dependency Evaluation ===")
    if args.agent:
        print(f"👤 Agent: {args.agent}")
    if resume:
        print(f"🔄 In-flight issue to resume: #{resume}")
    if claimed_now and claimed_now != resume:
        print(f"🔒 Claimed: #{claimed_now}")
    if next_issue:
        print(f"🎯 Next Actionable Issue: #{next_issue['number']} - {next_issue['title']}")
    else:
        print("✨ No unblocked, unclaimed issues available.")
    if res["parallel_eligible_issues"]:
        print(f"⚡ Claimable in parallel right now: {res['parallel_eligible_issues']}")
    if res["held_by_other_agents"]:
        held = ", ".join(f"#{h['number']}({h['agent']})" for h in res["held_by_other_agents"])
        print(f"🚧 Held by other agents: {held}")
    if res["blocked_by_file_conflict"]:
        cf = ", ".join(f"#{c['number']}" for c in res["blocked_by_file_conflict"])
        print(f"📁 Deferred - file conflict with in-flight work: {cf}")
    if res["missing_touches"]:
        print(f"📝 Deferred - missing touches declaration: {res['missing_touches']}")


if __name__ == "__main__":
    main()
