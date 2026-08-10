#!/usr/bin/env python3
"""
claim_issue.py - Optimistically claims a GitHub issue, or a PR for review,
for one agent.

GitHub exposes no compare-and-swap on issue state, so a true lock is not
available. The protocol here is optimistic:

  1. Read the issue. If another agent already holds it, abort (exit 2).
  2. Write our agent:<id> label.
  3. Settle: sleep and read back repeatedly. If two agents raced, both see
     both labels and compute the same winner - the lowest-sorting agent id.
     The loser releases. Multiple settle rounds catch late label writes that
     arrive after an earlier sole-holder readback.
  4. Move the board item, then verify holders once more; roll back if a
     lower-sorting contender appeared during the status update.

Step 3/4 is what makes this safe. Without settle+confirm, two agents that
read "unclaimed" in the same instant both proceed and duplicate the work.

Exit codes:
  0 - claimed
  2 - conflict; another agent holds it (caller should try the next candidate)
  1 - error
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from common import (
    AGENT_LABEL_PREFIX,
    agent_labels,
    claimed_by,
    ensure_label,
    get_issue,
    label_names,
    run_cmd,
)
from update_issue_status import update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2

# How long to wait before each read-back. Needs to exceed the window in which a
# racing agent's label write lands on GitHub's side.
READBACK_DELAY_S = 2.0

# Consecutive settle rounds that must agree we are the winner before we commit
# the claim. A single sole-holder readback is not enough: a lexicographically
# smaller contender can still apply its label afterward and also declare win.
SETTLE_ROUNDS = 2

# The review-claim path below still uses a single confirmation read rather than
# SETTLE_ROUNDS. It predates the settle hardening above and was merged forward
# unchanged; the two claim paths should converge on one protocol. Until they do,
# review claiming has the weaker guarantee: a contender whose label lands after
# this confirm is not detected. Tracked as follow-up, not a regression - this is
# the behaviour PR #16 was reviewed and tested against.
CONFIRM_DELAY_S = 4.0


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


def _has_in_progress(issue: dict) -> bool:
    return "status:in-progress" in {name.lower() for name in label_names(issue)}


def _rollback_claim(issue_id: int, agent: str, assignee: str) -> None:
    """Best-effort undo after a late contender wins during/after status update."""
    update_status(issue_id, "Ready", require_board=True)
    _remove_agent_label(issue_id, agent)
    run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--remove-assignee", assignee],
        check=False,
    )


def _settle_as_winner(issue_id: int, agent: str, my_label: str) -> int:
    """Sleep/read until SETTLE_ROUNDS consecutive readbacks name us the winner.

    Returns EXIT_OK when settled as winner, EXIT_CONFLICT when another agent
    wins, EXIT_ERROR on read failure.
    """
    for round_num in range(1, SETTLE_ROUNDS + 1):
        time.sleep(READBACK_DELAY_S)
        issue = get_issue(issue_id)
        if not issue:
            print(
                f"[ERROR] Could not read back Issue #{issue_id} after claiming "
                f"(settle {round_num}/{SETTLE_ROUNDS}).",
                file=sys.stderr,
            )
            _remove_agent_label(issue_id, agent)
            return EXIT_ERROR

        holders = agent_labels(issue)
        if my_label not in holders:
            print(
                f"[ERROR] Claim label '{my_label}' was not present during read-back "
                f"(settle {round_num}/{SETTLE_ROUNDS}).",
                file=sys.stderr,
            )
            return EXIT_ERROR

        if holders[0] != my_label:
            contenders = ", ".join(h[len(AGENT_LABEL_PREFIX):] for h in holders)
            print(
                f"[CONFLICT] Race on #{issue_id} between [{contenders}]; "
                f"'{holders[0][len(AGENT_LABEL_PREFIX):]}' wins. Releasing.",
                file=sys.stderr,
            )
            _remove_agent_label(issue_id, agent)
            return EXIT_CONFLICT

        if len(holders) > 1:
            contenders = ", ".join(h[len(AGENT_LABEL_PREFIX):] for h in holders)
            print(
                f"[INFO] Race on #{issue_id} between [{contenders}]; "
                f"'{agent}' wins (settle {round_num}/{SETTLE_ROUNDS})."
            )

    return EXIT_OK


def _finalize_claim(issue_id: int, agent: str, status: str, assignee: str,
                    my_label: str) -> int:
    """Assign, move board status, and confirm no late lower-sorting contender."""
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

    # Post-commit verify: a late lexicographically-smaller label write can land
    # during update_status. If so, roll back rather than leave two winners.
    time.sleep(READBACK_DELAY_S)
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Could not verify Issue #{issue_id} after status update.", file=sys.stderr)
        _rollback_claim(issue_id, agent, assignee)
        return EXIT_ERROR

    holders = agent_labels(issue)
    if my_label not in holders or holders[0] != my_label:
        contenders = ", ".join(h[len(AGENT_LABEL_PREFIX):] for h in holders) or "(none)"
        print(
            f"[CONFLICT] Late contender on #{issue_id} after status update "
            f"[{contenders}]; releasing.",
            file=sys.stderr,
        )
        _rollback_claim(issue_id, agent, assignee)
        return EXIT_CONFLICT

    print(f"✅ Issue #{issue_id} claimed by '{agent}'.")
    return EXIT_OK


def claim_issue(issue_id: int, agent: str, status: str = "In Progress",
                assignee: str = "@me") -> int:
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Issue #{issue_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    my_label = _label_for(agent)

    # --- Step 1: pre-check -------------------------------------------------
    holder = claimed_by(issue)
    if holder and holder != agent:
        print(f"[CONFLICT] Issue #{issue_id} is already held by '{holder}'.", file=sys.stderr)
        return EXIT_CONFLICT
    if holder == agent:
        # Same-agent retry after a crash mid-claim: the label alone is not a
        # completed claim. Finish assignee + In Progress before reporting OK.
        if _has_in_progress(issue):
            print(f"[INFO] Issue #{issue_id} is already yours; resuming.")
            return EXIT_OK
        print(f"[INFO] Completing interrupted claim on #{issue_id}...")
        return _finalize_claim(issue_id, agent, status, assignee, my_label)

    # --- Step 2: write our claim ------------------------------------------
    if not ensure_label(my_label, "1d76db", f"Claimed by agent '{agent}'"):
        print(f"[ERROR] Could not provision claim label '{my_label}'.", file=sys.stderr)
        return EXIT_ERROR

    code, _, err = run_cmd(
        ["gh", "issue", "edit", str(issue_id), "--add-label", my_label], check=False
    )
    if code != 0:
        print(f"[ERROR] Could not apply claim label: {err}", file=sys.stderr)
        return EXIT_ERROR

    # --- Step 3: settle and resolve any race ------------------------------
    settled = _settle_as_winner(issue_id, agent, my_label)
    if settled != EXIT_OK:
        return settled

    # --- Step 4: commit the claim + post-verify ---------------------------
    return _finalize_claim(issue_id, agent, status, assignee, my_label)


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


# --- Reviewing a pull request ----------------------------------------------
# Review is work an agent claims off the board, not a CI job calling a provider
# API - agents run the loop under their own subscriptions. Two agents reviewing
# the same PR is the same waste as two agents implementing the same issue, so
# it uses the same optimistic protocol: write, read back, deterministic
# lowest-id tie-break, loser releases.
#
# Differences from an issue claim, both deliberate:
#   * no board status change - the linked issue stays In Review while its PR is
#     being reviewed; the review is not separate board work.
#   * no assignee - assignment is already meaningless here, since every agent
#     authenticates as the same GitHub user.

REVIEWER_LABEL_PREFIX = "reviewer:"


def _reviewer_label_for(agent: str) -> str:
    return f"{REVIEWER_LABEL_PREFIX}{agent}"


def _pr_labels(pr_id: int):
    """Returns the PR's label names, or None when the PR cannot be read."""
    code, out, _ = run_cmd(
        ["gh", "pr", "view", str(pr_id), "--json", "labels", "-q",
         "[.labels[].name] | join(\"\\n\")"],
        check=False,
    )
    if code != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def reviewer_labels(labels) -> list:
    return sorted(name for name in (labels or []) if name.startswith(REVIEWER_LABEL_PREFIX))


def reviewed_by(labels):
    """The agent currently holding the review claim, if any."""
    held = reviewer_labels(labels)
    return held[0][len(REVIEWER_LABEL_PREFIX):] if held else None


def _remove_reviewer_label(pr_id: int, agent: str) -> bool:
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--remove-label", _reviewer_label_for(agent)],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not remove review claim for '{agent}': {err}", file=sys.stderr)
    return code == 0


def claim_review(pr_id: int, agent: str) -> int:
    """Claims a pull request for review. Same exit codes as claim_issue."""
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    holder = reviewed_by(labels)
    if holder and holder != agent:
        print(f"[CONFLICT] PR #{pr_id} is already being reviewed by '{holder}'.", file=sys.stderr)
        return EXIT_CONFLICT
    if holder == agent:
        print(f"[INFO] PR #{pr_id} is already yours to review; resuming.")
        return EXIT_OK

    my_label = _reviewer_label_for(agent)
    if not ensure_label(my_label, "0e8a16", f"Under review by agent '{agent}'"):
        print(f"[ERROR] Could not provision review label '{my_label}'.", file=sys.stderr)
        return EXIT_ERROR

    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--add-label", my_label], check=False
    )
    if code != 0:
        print(f"[ERROR] Could not apply review claim: {err}", file=sys.stderr)
        return EXIT_ERROR

    time.sleep(READBACK_DELAY_S)
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] Could not read back PR #{pr_id} after claiming.", file=sys.stderr)
        _remove_reviewer_label(pr_id, agent)
        return EXIT_ERROR

    holders = reviewer_labels(labels)
    if my_label not in holders:
        print(f"[ERROR] Review label '{my_label}' was not present during read-back.",
              file=sys.stderr)
        return EXIT_ERROR

    if len(holders) > 1:
        winner = holders[0]
        contenders = ", ".join(h[len(REVIEWER_LABEL_PREFIX):] for h in holders)
        if winner != my_label:
            print(f"[CONFLICT] Race to review #{pr_id} between [{contenders}]; "
                  f"'{winner[len(REVIEWER_LABEL_PREFIX):]}' wins. Releasing.", file=sys.stderr)
            _remove_reviewer_label(pr_id, agent)
            return EXIT_CONFLICT
        print(f"[INFO] Race to review #{pr_id} between [{contenders}]; '{agent}' wins.")

    time.sleep(CONFIRM_DELAY_S)
    confirm = _pr_labels(pr_id)
    if confirm is not None:
        holders = reviewer_labels(confirm)
        if len(holders) > 1 and holders[0] != my_label:
            print(f"[CONFLICT] Late contender on PR #{pr_id}; "
                  f"'{holders[0][len(REVIEWER_LABEL_PREFIX):]}' wins. Releasing.",
                  file=sys.stderr)
            _remove_reviewer_label(pr_id, agent)
            return EXIT_CONFLICT

    print(f"✅ PR #{pr_id} claimed for review by '{agent}'.")
    return EXIT_OK


def release_review(pr_id: int, agent: str) -> int:
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR
    holder = reviewed_by(labels)
    if holder != agent:
        print(f"[CONFLICT] PR #{pr_id} review is held by '{holder}', not '{agent}'.",
              file=sys.stderr)
        return EXIT_CONFLICT
    if not _remove_reviewer_label(pr_id, agent):
        return EXIT_ERROR
    print(f"♻️  Review claim on PR #{pr_id} released by '{agent}'.")
    return EXIT_OK


def reap_stale_reviews(hours: int = 4) -> list:
    """Releases review claims that have gone quiet.

    An agent that dies mid-review leaves the PR claimed forever, and a claimed
    PR is excluded from selection - so without this, one crash removes a PR
    from the review queue permanently and merge_pr.py blocks on it for good.

    A claim is stale when the PR has not been updated for `hours` and carries no
    submitted review from this round. Deliberately conservative: a PR that has
    been reviewed is left alone even if the label lingers, because the label is
    then harmless and removing it could invite a duplicate review.
    """
    if hours <= 0:
        return []

    code, out, _ = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200",
         "--json", "number,labels,updatedAt,reviews"],
        check=False,
    )
    if code != 0:
        print("[WARN] Could not list PRs; no review claims were reaped.", file=sys.stderr)
        return []
    try:
        prs = json.loads(out) if out else []
    except json.JSONDecodeError:
        print("[WARN] Could not parse PR list; no review claims were reaped.", file=sys.stderr)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    released = []
    for pr in prs:
        names = [lab.get("name", "") for lab in pr.get("labels", [])]
        holder = reviewed_by(names)
        if not holder:
            continue
        # Only a review submitted since the idle cutoff proves this claim did
        # its job. Any historical review used to make `reviews` permanently
        # non-empty, so a claim taken after an earlier review round and then
        # abandoned could never be reaped - the label excluded the PR from
        # every future picker run, forever.
        recent = False
        for review in pr.get("reviews") or []:
            try:
                when = datetime.fromisoformat(
                    (review.get("submittedAt") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if when > cutoff:
                recent = True
                break
        if recent:
            continue  # A review landed in this window; the claim is spent.
        try:
            ts = datetime.fromisoformat((pr.get("updatedAt") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts > cutoff:
            continue
        if _remove_reviewer_label(pr["number"], holder):
            released.append(pr["number"])
            print(f"♻️  Released stale review claim on PR #{pr['number']} "
                  f"(held by '{holder}', idle > {hours}h, no review submitted).",
                  file=sys.stderr)
    return released


def main():
    parser = argparse.ArgumentParser(description="Claim or release a GitHub issue or PR review for one agent.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int, help="GitHub Issue Number")
    target.add_argument("--pr", type=int, help="Pull Request number to claim for review")
    parser.add_argument("--agent", type=str, required=True,
                        help="Agent id, e.g. 'agent-1'. Becomes the agent:<id> label.")
    parser.add_argument("--assignee", type=str, default="@me", help="GitHub assignee (default: @me)")
    parser.add_argument("--status", type=str, default="In Progress", help="Target status column")
    parser.add_argument("--release", action="store_true",
                        help="Give up this claim and return the work to the queue")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release review claims idle longer than HOURS with no review submitted")
    args = parser.parse_args()

    if args.reap_after:
        reap_stale_reviews(args.reap_after)

    if args.pr is not None:
        rc = release_review(args.pr, args.agent) if args.release else claim_review(args.pr, args.agent)
        sys.exit(rc)

    if args.release:
        sys.exit(release_issue(args.issue, args.agent))
    sys.exit(claim_issue(args.issue, args.agent, args.status, args.assignee))


if __name__ == "__main__":
    main()
