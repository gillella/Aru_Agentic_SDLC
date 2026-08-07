#!/usr/bin/env python3
"""
claim_issue.py - Optimistically claims a GitHub issue for one agent.

GitHub exposes no compare-and-swap on issue state, so a true lock is not
available. The protocol here is optimistic:

  1. Read the issue. If another agent already holds it, abort (exit 2).
  2. Write our agent:<id> label.
  3. Read back. If two agents raced, both now see both labels and both compute
     the same winner - the lowest-sorting agent id. The loser releases.
  4. Move the board item.

Step 3 is what makes this safe. Without it, two agents that read "unclaimed"
in the same instant both proceed and duplicate the work.

Exit codes:
  0 - claimed
  2 - conflict; another agent holds it (caller should try the next candidate)
  1 - error
"""

import argparse
import sys
import time

from common import (
    AGENT_LABEL_PREFIX,
    agent_labels,
    claimed_by,
    ensure_label,
    get_issue,
    run_cmd,
)
from update_issue_status import update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2

# How long to wait before reading back. Needs to exceed the window in which a
# racing agent's label write lands on GitHub's side.
READBACK_DELAY_S = 2.0


def _label_for(agent: str) -> str:
    return f"{AGENT_LABEL_PREFIX}{agent}"


def _remove_agent_label(issue_id: int, agent: str) -> bool:
    """Removes only this agent's claim marker.

    Shared status labels must never be changed by a losing contender: the
    winner may already have moved the issue to In Progress, and removing that
    label here would make the outcome depend on which cleanup request lands
    last.
    """
    code, _, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--remove-label", _label_for(agent)],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not remove claim label for '{agent}': {err}", file=sys.stderr)
    return code == 0


def claim_issue(issue_id: int, agent: str, status: str = "In Progress",
                assignee: str = "@me") -> int:
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Issue #{issue_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    # --- Step 1: pre-check -------------------------------------------------
    holder = claimed_by(issue)
    if holder and holder != agent:
        print(f"[CONFLICT] Issue #{issue_id} is already held by '{holder}'.", file=sys.stderr)
        return EXIT_CONFLICT
    if holder == agent:
        print(f"[INFO] Issue #{issue_id} is already yours; resuming.")
        return EXIT_OK

    # --- Step 2: write our claim ------------------------------------------
    my_label = _label_for(agent)
    if not ensure_label(my_label, "1d76db", f"Claimed by agent '{agent}'"):
        print(f"[ERROR] Could not provision claim label '{my_label}'.", file=sys.stderr)
        return EXIT_ERROR

    code, _, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--add-label", my_label], check=False
    )
    if code != 0:
        print(f"[ERROR] Could not apply claim label: {err}", file=sys.stderr)
        return EXIT_ERROR

    # --- Step 3: read back and resolve any race ---------------------------
    time.sleep(READBACK_DELAY_S)
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Could not read back Issue #{issue_id} after claiming.", file=sys.stderr)
        _remove_agent_label(issue_id, agent)
        return EXIT_ERROR
    holders = agent_labels(issue)

    if my_label not in holders:
        print(
            f"[ERROR] Claim label '{my_label}' was not present during read-back.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if len(holders) > 1:
        winner = holders[0]  # deterministic: lowest-sorting label wins
        contenders = ", ".join(h[len(AGENT_LABEL_PREFIX):] for h in holders)
        if winner != my_label:
            print(
                f"[CONFLICT] Race on #{issue_id} between [{contenders}]; "
                f"'{winner[len(AGENT_LABEL_PREFIX):]}' wins. Releasing.",
                file=sys.stderr,
            )
            _remove_agent_label(issue_id, agent)
            return EXIT_CONFLICT
        print(f"[INFO] Race on #{issue_id} between [{contenders}]; '{agent}' wins.")

    # --- Step 4: commit the claim -----------------------------------------
    code, _, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--add-assignee", assignee], check=False
    )
    if code != 0:
        print(f"[WARN] Unable to assign issue: {err}. Continuing...", file=sys.stderr)

    # Direct import rather than shelling out to a relative script path. The
    # previous version ran ["python3", "scripts/update_issue_status.py", ...],
    # which resolves against the target project's cwd rather than the
    # framework's, so it silently did nothing from a project repo root.
    if not update_status(issue_id, status, require_board=True):
        _remove_agent_label(issue_id, agent)
        run_cmd(
            ["gh", "issue", "edit", str(issue_id), "--remove-assignee", assignee],
            check=False,
        )
        return EXIT_ERROR

    print(f"✅ Issue #{issue_id} claimed by '{agent}'.")
    return EXIT_OK


def release_issue(issue_id: int, agent: str) -> int:
    """Voluntarily gives up a claim so another agent can take the work.

    Without this, an agent that decides an issue is out of scope or blocked has
    no way to hand it back short of waiting for the reaper.
    """
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Issue #{issue_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    holder = claimed_by(issue)
    if holder != agent:
        print(f"[CONFLICT] Issue #{issue_id} is held by '{holder}', not '{agent}'.", file=sys.stderr)
        return EXIT_CONFLICT

    if not update_status(issue_id, "Ready", require_board=True):
        return EXIT_ERROR
    if not _remove_agent_label(issue_id, agent):
        update_status(issue_id, "In Progress", require_board=True)
        return EXIT_ERROR
    code, _, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--remove-assignee", "@me"],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Unable to remove assignee: {err}", file=sys.stderr)
    print(f"♻️  Issue #{issue_id} released by '{agent}' and returned to Ready.")
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Claim or release a GitHub issue for one agent.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--agent", type=str, required=True,
                        help="Agent id, e.g. 'agent-1'. Becomes the agent:<id> label.")
    parser.add_argument("--assignee", type=str, default="@me", help="GitHub assignee (default: @me)")
    parser.add_argument("--status", type=str, default="In Progress", help="Target status column")
    parser.add_argument("--release", action="store_true",
                        help="Give up this claim and return the issue to Ready")
    args = parser.parse_args()

    if args.release:
        sys.exit(release_issue(args.issue, args.agent))
    sys.exit(claim_issue(args.issue, args.agent, args.status, args.assignee))


if __name__ == "__main__":
    main()
