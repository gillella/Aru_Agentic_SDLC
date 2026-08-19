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
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import merge_pr
from common import (
    AGENT_LABEL_PREFIX,
    agent_labels,
    claimed_by,
    ensure_label,
    get_issue,
    get_repo_slug,
    is_trusted_metadata_author,
    label_names,
    repository_owner_login,
    repository_trusted_logins,
    run_cmd,
)
from factory_metrics import fetch_paginated_gh_api, parse_iso
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


def _status_labels(issue: dict) -> list[str]:
    """Returns deterministic unique status labels for fail-closed decisions."""
    return sorted({
        name.lower() for name in label_names(issue) if name.lower().startswith("status:")
    })


def _has_in_progress(issue: dict) -> bool:
    return _status_labels(issue) == ["status:in-progress"]


def _status_name(issue: dict) -> str:
    statuses = _status_labels(issue)
    if not statuses:
        return "unknown"
    if len(statuses) > 1:
        values = ",".join(name.removeprefix("status:") for name in statuses)
        return f"ambiguous({values})"
    return statuses[0].removeprefix("status:")


def _has_ready(issue: dict) -> bool:
    return _status_labels(issue) == ["status:ready"]


def _metadata_is_trusted(issue: dict, owner=None, trusted_logins=None) -> bool:
    """Re-evaluates provenance with the same predicate the picker uses."""
    return is_trusted_metadata_author(
        issue, owner, trusted_logins=trusted_logins,
    )


def _refuse_untrusted_metadata(issue_id: int) -> int:
    print(
        f"[CONFLICT] Issue #{issue_id} metadata is untrusted; refusing to claim "
        "until a trusted rewrite.",
        file=sys.stderr,
    )
    return EXIT_CONFLICT


def _needs_human(issue: dict) -> bool:
    return "needs-human" in {name.lower() for name in label_names(issue)}


def _rollback_claim(issue_id: int, agent: str, assignee: str,
                    target_status: str = "Ready") -> None:
    """Best-effort undo after a late contender wins during/after status update."""
    update_status(issue_id, target_status, require_board=True)
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
        if _needs_human(issue):
            if my_label in holders:
                _rollback_claim(issue_id, agent, "@me", target_status="Backlog")
            print(
                f"[CONFLICT] Issue #{issue_id} became operator-only (needs-human) "
                "while the claim was settling.",
                file=sys.stderr,
            )
            return EXIT_CONFLICT
        if my_label not in holders:
            print(
                f"[ERROR] Claim label '{my_label}' was not present during read-back "
                f"(settle {round_num}/{SETTLE_ROUNDS}).",
                file=sys.stderr,
            )
            return EXIT_ERROR

        if not _has_ready(issue):
            # Another worker may have parked or completed the issue after our
            # initial Ready read. A concurrently completed claim by this same
            # agent is safe to resume; every other transition invalidates this
            # fresh attempt. Remove only our contender label when another
            # identity is also present, preserving the parked author's marker.
            if _has_in_progress(issue) and holders == [my_label]:
                return EXIT_OK
            if len(holders) > 1:
                _remove_agent_label(issue_id, agent)
            print(
                f"[CONFLICT] Issue #{issue_id} became {_status_name(issue)} while "
                "the claim was settling; refusing to reopen it.",
                file=sys.stderr,
            )
            return EXIT_CONFLICT

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
                    my_label: str, owner=None, trusted_logins=None) -> int:
    """Assign, move board status, and confirm no late lower-sorting contender."""
    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Could not revalidate Issue #{issue_id} before finalizing.",
              file=sys.stderr)
        return EXIT_ERROR
    if not _metadata_is_trusted(issue, owner, trusted_logins):
        holders = agent_labels(issue)
        if my_label in holders:
            _rollback_claim(issue_id, agent, assignee)
        return _refuse_untrusted_metadata(issue_id)
    holders = agent_labels(issue)
    if _needs_human(issue):
        if my_label in holders:
            _rollback_claim(issue_id, agent, assignee, target_status="Backlog")
        print(
            f"[CONFLICT] Issue #{issue_id} became operator-only (needs-human) "
            "before claim finalization.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    if _has_in_progress(issue) and holders == [my_label]:
        print(f"[INFO] Issue #{issue_id} is already yours; resuming.")
        return EXIT_OK
    if not _has_ready(issue):
        if my_label in holders and len(holders) > 1:
            _remove_agent_label(issue_id, agent)
        print(
            f"[CONFLICT] Issue #{issue_id} became {_status_name(issue)} before "
            "claim finalization; refusing to reopen it.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

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
    if not _metadata_is_trusted(issue, owner, trusted_logins):
        _rollback_claim(issue_id, agent, assignee)
        return _refuse_untrusted_metadata(issue_id)

    holders = agent_labels(issue)
    if _needs_human(issue):
        print(
            f"[CONFLICT] Issue #{issue_id} became operator-only (needs-human) "
            "during claim finalization; returning it to Backlog.",
            file=sys.stderr,
        )
        _rollback_claim(issue_id, agent, assignee, target_status="Backlog")
        return EXIT_CONFLICT
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
    owner = repository_owner_login()
    trusted_logins = repository_trusted_logins()

    # --- Step 1: pre-check -------------------------------------------------
    if _needs_human(issue):
        print(
            f"[CONFLICT] Issue #{issue_id} is operator-only (needs-human); "
            "factory agents must not claim it.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

    if _has_ready(issue) and not _metadata_is_trusted(issue, owner, trusted_logins):
        return _refuse_untrusted_metadata(issue_id)

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
        if _has_ready(issue):
            print(f"[INFO] Completing interrupted claim on #{issue_id}...")
            return _finalize_claim(
                issue_id, agent, status, assignee, my_label,
                owner=owner, trusted_logins=trusted_logins,
            )
        print(
            f"[CONFLICT] Issue #{issue_id} is {_status_name(issue)}, not Ready or "
            "In Progress; refusing stale same-agent recovery.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

    # A fresh claim may start only from Ready. In Review deliberately makes
    # claimed_by() return None so the author can take new work, but that must
    # not make the parked issue claimable again. The same-agent interrupted
    # In Progress/Ready paths above remain explicitly resumable.
    if not _has_ready(issue):
        print(
            f"[CONFLICT] Issue #{issue_id} is {_status_name(issue)}, not Ready; "
            "refusing a new claim.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

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
    return _finalize_claim(
        issue_id, agent, status, assignee, my_label,
        owner=owner, trusted_logins=trusted_logins,
    )


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

    target_status = "Backlog" if _needs_human(issue) else "Ready"
    if not update_status(issue_id, target_status, require_board=True):
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
    print(f"♻️  Issue #{issue_id} released by '{agent}' and returned to {target_status}.")


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
AUTHOR_LABEL_PREFIX = "author:"


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


def review_claimant(labels):
    """The agent currently holding the review claim, if any.

    Named for the prefix it actually reads. It was called reviewed_by(), which
    described a different label - merge_pr.py read `reviewed-by:` while this
    wrote `reviewer:`, and the mismatch went unnoticed partly because the
    function name matched the key nobody wrote.
    """
    held = reviewer_labels(labels)
    return held[0][len(REVIEWER_LABEL_PREFIX):] if held else None


# Retained so existing callers keep working; prefer review_claimant.
reviewed_by = review_claimant


def pr_author(labels):
    """The agent stamped as the PR's author, or None if unstamped."""
    for name in labels or []:
        if name.startswith(AUTHOR_LABEL_PREFIX):
            return name[len(AUTHOR_LABEL_PREFIX):]
    return None


def _remove_reviewer_label(pr_id: int, agent: str) -> bool:
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--remove-label", _reviewer_label_for(agent)],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not remove review claim for '{agent}': {err}", file=sys.stderr)
    return code == 0


def claim_review(pr_id: int, agent: str) -> int:  # noqa: C901
    """Claims a pull request for review. Same exit codes as claim_issue."""
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    # Refuse the PR's own author here, not only in the picker. fetch_next_work
    # filters own-authored PRs when it hands out review work, but a direct
    # `--pr <n> --agent <me>` bypasses that, and merge_pr.py now treats this
    # claim as the identity of the reviewer. The guarantee has to live where
    # the label is written.
    author = pr_author(labels)
    if author and author == agent:
        print(f"[CONFLICT] PR #{pr_id} was authored by '{agent}'. "
              "An agent may not claim review of its own PR.", file=sys.stderr)
        return EXIT_CONFLICT

    holder = review_claimant(labels)
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


REVIEWED_BY_LABEL_PREFIX = "reviewed-by:"
REVIEW_HEAD_ATTESTATION_VERSION = "aru-review-head:v1"


def _reviewed_by_label_for(agent: str) -> str:
    return f"{REVIEWED_BY_LABEL_PREFIX}{agent}"


def _reviewed_head_for_completion(pr_id: int) -> str | None:
    """Current head only when a substantive independent review covers it."""
    slug = get_repo_slug()
    if not slug or "/" not in slug:
        return None
    owner, name = slug.split("/", 1)
    result = merge_pr._reviewed_current_head(owner, name, pr_id)
    if result is None:
        return None
    head, reviewed_head, _reviews = result
    if not reviewed_head or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        return None
    return head.lower()


def _review_head_attestation(agent: str, head: str) -> str:
    payload = json.dumps(
        {"agent": agent, "head": head}, sort_keys=True, separators=(",", ":")
    )
    return (
        f"<!-- {REVIEW_HEAD_ATTESTATION_VERSION} {payload} -->\n"
        "## Review completion\n\n"
        f"- reviewed-by: `{agent}`\n"
        f"- reviewed-head: `{head}`\n"
    )


def complete_review(pr_id: int, agent: str) -> int:
    """Attributes a finished review, then releases the claim.

    This exists because the step had no command. `fleet-worker.md` told the
    reviewing agent to "label the PR reviewed-by:<id>" in prose and gave a
    command only for the release that followed. The executable half got done
    and the prose half did not, so PRs arrived claimed but unattributed and
    the merge gate refused work that had genuinely been reviewed.

    Ordering matters: attribute first, release second. The reverse would leave
    a window where the PR is neither claimed nor attributed, and another agent
    could pick it up for a review that had already happened.
    """
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    # Attribution is not something a passer-by may write. Requiring the claim
    # keeps "who reviewed this" tied to the agent that actually took the work.
    holder = review_claimant(labels)
    if holder != agent:
        print(f"[CONFLICT] PR #{pr_id} review is held by "
              f"'{holder or 'nobody'}', not '{agent}'. Claim it before completing.",
              file=sys.stderr)
        return EXIT_CONFLICT

    author = pr_author(labels)
    if author and author == agent:
        print(f"[CONFLICT] PR #{pr_id} was authored by '{agent}'. "
              "An agent may not attribute a review of its own PR.", file=sys.stderr)
        return EXIT_CONFLICT

    reviewed_head = _reviewed_head_for_completion(pr_id)
    if reviewed_head is None:
        print(
            f"[CONFLICT] PR #{pr_id} has no substantive independent review on its "
            "current head. Submit the review before completing attribution.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT

    code, _, err = run_cmd(
        ["gh", "pr", "comment", str(pr_id), "--body",
         _review_head_attestation(agent, reviewed_head)],
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not stamp reviewed-head evidence: {err}", file=sys.stderr)
        return EXIT_ERROR

    stamp = _reviewed_by_label_for(agent)
    if not ensure_label(stamp, "0e8a16", f"Reviewed by agent '{agent}'"):
        print(f"[ERROR] Could not provision attribution label '{stamp}'.", file=sys.stderr)
        return EXIT_ERROR
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--add-label", stamp], check=False
    )
    if code != 0:
        print(f"[ERROR] Could not attribute the review: {err}", file=sys.stderr)
        return EXIT_ERROR
    print(f"🏷️  Attributed review of PR #{pr_id} to '{agent}'.")

    if not _remove_reviewer_label(pr_id, agent):
        # The merge gate deliberately blocks every live claim. Returning
        # success here would tell the reviewer that completion finished while
        # leaving the PR unable to merge. Attribution is idempotent, so a
        # nonzero result makes the same --complete-review command safely
        # retryable after a transient GitHub label-removal failure.
        print("[ERROR] Review attributed, but the claim label could not be released. "
              "Retry --complete-review or release it with --release.", file=sys.stderr)
        return EXIT_ERROR
    print(f"✅ Review of PR #{pr_id} completed by '{agent}'; claim released.")
    return EXIT_OK


def release_review(pr_id: int, agent: str) -> int:
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR
    holder = review_claimant(labels)
    if holder != agent:
        print(f"[CONFLICT] PR #{pr_id} review is held by '{holder}', not '{agent}'.",
              file=sys.stderr)
        return EXIT_CONFLICT
    if not _remove_reviewer_label(pr_id, agent):
        return EXIT_ERROR
    print(f"♻️  Review claim on PR #{pr_id} released by '{agent}'.")
    return EXIT_OK


# --- Mechanical merge of a pull request ------------------------------------
# Merge is board work once independent review is proven. Two agents racing the
# same merge wastes retries and can confuse close-out, so it uses the same
# optimistic label protocol as review claims. The label is coordination only:
# merge_pr.py remains the sole merge authority and never treats merger:<id> as
# review attestation.

MERGER_LABEL_PREFIX = "merger:"


def _merger_label_for(agent: str) -> str:
    return f"{MERGER_LABEL_PREFIX}{agent}"


def merger_labels(labels) -> list:
    return sorted(name for name in (labels or []) if name.startswith(MERGER_LABEL_PREFIX))


def merge_claimant(labels):
    held = merger_labels(labels)
    return held[0][len(MERGER_LABEL_PREFIX):] if held else None


def _remove_merger_label(pr_id: int, agent: str) -> bool:
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--remove-label", _merger_label_for(agent)],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not remove merge claim for '{agent}': {err}", file=sys.stderr)
    return code == 0


def _has_peer_reviewer(labels, author: str | None) -> bool:
    for name in labels or []:
        if not name.startswith(REVIEWED_BY_LABEL_PREFIX):
            continue
        who = name[len(REVIEWED_BY_LABEL_PREFIX):]
        if who and who != author:
            return True
    return False


def claim_merge(pr_id: int, agent: str) -> int:  # noqa: C901
    """Claims a pull request for mechanical merge. Same exit codes as claim_issue.

    The PR author may hold this claim only when a distinct completed peer review
    is already attributed. Self-review remains impossible because this path never
    writes reviewed-by:<id>.
    """
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR

    author = pr_author(labels)
    if author and author == agent and not _has_peer_reviewer(labels, author):
        print(f"[CONFLICT] PR #{pr_id} was authored by '{agent}' and has no "
              f"distinct {REVIEWED_BY_LABEL_PREFIX}<peer> attribution yet. "
              "Self-review cannot unlock mechanical merge.", file=sys.stderr)
        return EXIT_CONFLICT

    holder = merge_claimant(labels)
    if holder and holder != agent:
        print(f"[CONFLICT] PR #{pr_id} merge is already claimed by '{holder}'.",
              file=sys.stderr)
        return EXIT_CONFLICT
    if holder == agent:
        print(f"[INFO] PR #{pr_id} merge is already yours; resuming.")
        return EXIT_OK

    my_label = _merger_label_for(agent)
    if not ensure_label(my_label, "5319e7", f"Merging by agent '{agent}'"):
        print(f"[ERROR] Could not provision merge label '{my_label}'.", file=sys.stderr)
        return EXIT_ERROR

    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--add-label", my_label], check=False
    )
    if code != 0:
        print(f"[ERROR] Could not apply merge claim: {err}", file=sys.stderr)
        return EXIT_ERROR

    time.sleep(READBACK_DELAY_S)
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] Could not read back PR #{pr_id} after claiming merge.",
              file=sys.stderr)
        _remove_merger_label(pr_id, agent)
        return EXIT_ERROR

    holders = merger_labels(labels)
    if my_label not in holders:
        print(f"[ERROR] Merge label '{my_label}' was not present during read-back.",
              file=sys.stderr)
        return EXIT_ERROR

    if len(holders) > 1:
        winner = holders[0]
        contenders = ", ".join(h[len(MERGER_LABEL_PREFIX):] for h in holders)
        if winner != my_label:
            print(f"[CONFLICT] Race to merge #{pr_id} between [{contenders}]; "
                  f"'{winner[len(MERGER_LABEL_PREFIX):]}' wins. Releasing.",
                  file=sys.stderr)
            _remove_merger_label(pr_id, agent)
            return EXIT_CONFLICT
        print(f"[INFO] Race to merge #{pr_id} between [{contenders}]; '{agent}' wins.")

    time.sleep(CONFIRM_DELAY_S)
    confirm = _pr_labels(pr_id)
    if confirm is not None:
        holders = merger_labels(confirm)
        if len(holders) > 1 and holders[0] != my_label:
            print(f"[CONFLICT] Late contender on merge of PR #{pr_id}; "
                  f"'{holders[0][len(MERGER_LABEL_PREFIX):]}' wins. Releasing.",
                  file=sys.stderr)
            _remove_merger_label(pr_id, agent)
            return EXIT_CONFLICT

    print(f"✅ PR #{pr_id} claimed for merge by '{agent}'.")
    return EXIT_OK


def release_merge(pr_id: int, agent: str) -> int:
    labels = _pr_labels(pr_id)
    if labels is None:
        print(f"[ERROR] PR #{pr_id} not found.", file=sys.stderr)
        return EXIT_ERROR
    holder = merge_claimant(labels)
    if holder != agent:
        print(f"[CONFLICT] PR #{pr_id} merge is held by '{holder}', not '{agent}'.",
              file=sys.stderr)
        return EXIT_CONFLICT
    if not _remove_merger_label(pr_id, agent):
        return EXIT_ERROR
    print(f"♻️  Merge claim on PR #{pr_id} released by '{agent}'.")
    return EXIT_OK


def _claim_labeled_at(pr_number: int, label_name: str):
    """Return the latest exact-label claim event, or None when unprovable."""
    events = fetch_paginated_gh_api(
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/timeline"
    )
    if events is None:
        print(
            f"[WARN] Could not read claim timeline for PR #{pr_number}; "
            "no claims were reaped.",
            file=sys.stderr,
        )
        return None

    matches = []
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "labeled":
            continue
        label = event.get("label")
        if not isinstance(label, dict) or label.get("name") != label_name:
            continue
        when = parse_iso(event.get("created_at") or "")
        if when is None or when.tzinfo is None:
            print(
                f"[WARN] Claim timeline for PR #{pr_number} has an invalid "
                f"timestamp for {label_name}; no claims were reaped.",
                file=sys.stderr,
            )
            return None
        matches.append(when)

    if not matches:
        print(
            f"[WARN] Claim timeline for PR #{pr_number} has no labeled event "
            f"for {label_name}; no claims were reaped.",
            file=sys.stderr,
        )
        return None
    return max(matches)


def _claims_with_timestamps(prs: list, prefix: str, claimant):
    """Resolve every live claim before callers perform any label mutation."""
    claims = []
    seen = set()
    for pr in prs:
        names = [
            lab.get("name", "")
            for lab in pr.get("labels", [])
            if isinstance(lab, dict)
        ]
        holder = claimant(names)
        if not holder:
            continue
        number = pr.get("number")
        if not isinstance(number, int):
            print(
                "[WARN] Claimed PR is missing a valid number; no claims were reaped.",
                file=sys.stderr,
            )
            return None
        if number in seen:
            continue
        seen.add(number)
        label_name = f"{prefix}{holder}"
        claimed_at = _claim_labeled_at(number, label_name)
        if claimed_at is None:
            return None
        claims.append((pr, holder, claimed_at))
    return claims


def _revalidate_claims(prs: list, prefix: str, claimant, expected: list):
    """Fail closed if any live claim changed during the reaper preflight.

    GitHub does not offer an atomic compare-and-remove operation for labels.
    A second complete timeline snapshot immediately before the removal pass
    prevents an old event observed by the first pass from authorizing removal
    of a claim that was released and reacquired while the sweep was running.
    """
    refreshed = _claims_with_timestamps(prs, prefix, claimant)
    if refreshed is None:
        return None
    expected_state = {
        (pr["number"], holder): claimed_at
        for pr, holder, claimed_at in expected
    }
    refreshed_state = {
        (pr["number"], holder): claimed_at
        for pr, holder, claimed_at in refreshed
    }
    if refreshed_state != expected_state:
        print(
            "[WARN] Claim state changed during reaper preflight; "
            "no claims were reaped.",
            file=sys.stderr,
        )
        return None
    return refreshed


def _effective_reap_threshold(
    holder: str,
    base_hours: int | float,
    store: Any = None,
    now: Optional[datetime] = None,
) -> tuple[float, str]:
    """Computes the effective reap threshold in hours and the explanatory reason.

    - Live agent with a fresh heartbeat -> full base_hours ("live agent")
    - Agent absent from registry -> reduced threshold max(1.0, base_hours / 2.0) ("agent absent from presence registry")
    - Agent with expired heartbeat / offline -> reduced threshold max(1.0, base_hours / 2.0) ("agent heartbeat expired" / availability)
    - Missing or unreadable presence registry -> falls back to full base_hours with a warning ("presence unavailable")
    - The reduced threshold never drops below 1.0 hour.
    """
    base = float(base_hours)
    if base <= 0:
        return 0.0, "reaping disabled"

    if store is None:
        try:
            from agent_presence import PresenceStore
            store = PresenceStore()
        except Exception as exc:
            print(f"[WARN] Presence registry unavailable: {exc}", file=sys.stderr)
            return base, "presence registry unavailable (full threshold fallback)"

    current_time = now or datetime.now(timezone.utc)
    try:
        record = store.get(holder)
    except Exception as exc:
        print(f"[WARN] Could not inspect presence for '{holder}': {exc}", file=sys.stderr)
        return base, "presence check failed (full threshold fallback)"

    if record is None:
        reduced = max(1.0, base / 2.0)
        return reduced, "agent absent from presence registry"

    if record.availability in {"unavailable", "temporarily-offline"}:
        reduced = max(1.0, base / 2.0)
        return reduced, f"agent availability is '{record.availability}'"

    if not record.last_heartbeat:
        reduced = max(1.0, base / 2.0)
        return reduced, "agent has no recorded heartbeat"

    try:
        from agent_presence import _parse_iso
        last_hb = _parse_iso(record.last_heartbeat)
    except Exception:
        reduced = max(1.0, base / 2.0)
        return reduced, "agent has invalid heartbeat timestamp"

    ttl_seconds = getattr(store, "heartbeat_ttl_seconds", 300)
    if (current_time - last_hb).total_seconds() > ttl_seconds:
        reduced = max(1.0, base / 2.0)
        return reduced, "agent heartbeat expired"

    return base, "live agent"


def reap_stale_merges(hours: int = 4, presence_store: Any = None, now: Optional[datetime] = None) -> list:
    """Releases merge claims that went quiet without finishing close-out.

    Open and merged PRs are both scanned. A crash right after server-side merge
    leaves ``merger:dead`` on a MERGED PR; peers must be able to reclaim it, and
    a reaper that only looks at ``--state open`` can never free that label.
    Author and reviewed-by attribution are never touched.
    """
    if hours <= 0:
        return []

    prs = []
    for state in ("open", "merged"):
        code, out, _ = run_cmd(
            ["gh", "pr", "list", "--state", state, "--limit", "200",
             "--json", "number,labels,state,mergedAt"],
            check=False,
        )
        if code != 0:
            print(f"[WARN] Could not list {state} PRs; merge reaping incomplete.",
                  file=sys.stderr)
            return []
        try:
            batch = json.loads(out) if out else []
        except json.JSONDecodeError:
            print(f"[WARN] Could not parse {state} PR list; merge reaping aborted.",
                  file=sys.stderr)
            return []
        prs.extend(batch)

    claims = _claims_with_timestamps(prs, MERGER_LABEL_PREFIX, merge_claimant)
    if claims is None:
        return []
    claims = _revalidate_claims(
        prs, MERGER_LABEL_PREFIX, merge_claimant, claims
    )
    if claims is None:
        return []

    store = presence_store
    if store is None:
        try:
            from agent_presence import PresenceStore
            store = PresenceStore()
        except Exception as exc:
            print(f"[WARN] Could not initialize presence store: {exc}", file=sys.stderr)
            store = None

    current_now = now or datetime.now(timezone.utc)
    released = []
    for pr, holder, claimed_at in claims:
        number = pr["number"]
        eff_hours, reason = _effective_reap_threshold(holder, hours, store, now=current_now)
        if eff_hours <= 0:
            continue
        cutoff = current_now - timedelta(hours=eff_hours)
        if claimed_at >= cutoff:
            continue
        if _remove_merger_label(number, holder):
            released.append(number)
            eff_str = f"{int(eff_hours)}h" if eff_hours.is_integer() else f"{eff_hours:.1f}h"
            print(f"♻️  Released stale merge claim on PR #{number} "
                  f"(held by '{holder}', {reason}, claim age > {eff_str}).",
                  file=sys.stderr)
    return released


def reap_stale_reviews(hours: int = 4, presence_store: Any = None, now: Optional[datetime] = None) -> list:  # noqa: C901, PLR0912
    """Releases review claims that have gone quiet.

    An agent that dies mid-review leaves the PR claimed forever, and a claimed
    PR is excluded from selection - so without this, one crash removes a PR
    from the review queue permanently and merge_pr.py blocks on it for good.

    A claim is stale when its latest labeled event is older than `hours` (or reduced
    threshold for absent/inactive agents) and carries no recent submitted review after
    that claim event.
    """
    if hours <= 0:
        return []

    code, out, _ = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200",
         "--json", "number,labels,reviews"],
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

    claims = _claims_with_timestamps(prs, REVIEWER_LABEL_PREFIX, review_claimant)
    if claims is None:
        return []
    claims = _revalidate_claims(
        prs, REVIEWER_LABEL_PREFIX, review_claimant, claims
    )
    if claims is None:
        return []

    store = presence_store
    if store is None:
        try:
            from agent_presence import PresenceStore
            store = PresenceStore()
        except Exception as exc:
            print(f"[WARN] Could not initialize presence store: {exc}", file=sys.stderr)
            store = None

    current_now = now or datetime.now(timezone.utc)
    released = []
    for pr, holder, claimed_at in claims:
        eff_hours, reason = _effective_reap_threshold(holder, hours, store, now=current_now)
        if eff_hours <= 0:
            continue
        cutoff = current_now - timedelta(hours=eff_hours)
        reviewed_after_claim = False
        for review in pr.get("reviews") or []:
            login = ((review.get("author") or {}).get("login") or "")
            if merge_pr.is_advisory_review_account(login):
                continue
            when = parse_iso(review.get("submittedAt") or "")
            if when is None or when.tzinfo is None:
                continue
            if when > claimed_at and when >= cutoff:
                reviewed_after_claim = True
                break
        if reviewed_after_claim:
            continue
        if claimed_at >= cutoff:
            continue
        if _remove_reviewer_label(pr["number"], holder):
            released.append(pr["number"])
            eff_str = f"{int(eff_hours)}h" if eff_hours.is_integer() else f"{eff_hours:.1f}h"
            print(f"♻️  Released stale review claim on PR #{pr['number']} "
                  f"(held by '{holder}', {reason}, claim age > {eff_str}, "
                  "no recent review submitted after claim).",
                  file=sys.stderr)
    return released


def main():
    parser = argparse.ArgumentParser(description="Claim or release a GitHub issue or PR review for one agent.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int, help="GitHub Issue Number")
    target.add_argument("--pr", type=int, help="Pull Request number to claim for review or merge")
    parser.add_argument("--agent", type=str, required=True,
                        help="Agent id, e.g. 'agent-1'. Becomes the agent:<id> label.")
    parser.add_argument("--assignee", type=str, default="@me", help="GitHub assignee (default: @me)")
    parser.add_argument("--status", type=str, default="In Progress", help="Target status column")
    parser.add_argument("--release", action="store_true",
                        help="Give up this claim and return the work to the queue")
    parser.add_argument("--complete-review", action="store_true", dest="complete",
                        help="Attribute a finished PR review (reviewed-by:<id>) and "
                             "release the claim. Run after submitting the GitHub review.")
    parser.add_argument("--merge", action="store_true",
                        help="With --pr: claim or release mechanical merge (merger:<id>), "
                             "not review.")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release review/merge claims idle longer than HOURS")
    args = parser.parse_args()

    if args.reap_after:
        reap_stale_reviews(args.reap_after)
        reap_stale_merges(args.reap_after)

    if args.pr is not None:
        if args.merge and args.complete:
            print("[ERROR] --complete-review does not apply to merge claims.",
                  file=sys.stderr)
            sys.exit(EXIT_ERROR)
        if args.merge:
            rc = (release_merge(args.pr, args.agent) if args.release
                  else claim_merge(args.pr, args.agent))
            sys.exit(rc)
        if args.complete:
            sys.exit(complete_review(args.pr, args.agent))
        rc = release_review(args.pr, args.agent) if args.release else claim_review(args.pr, args.agent)
        sys.exit(rc)

    if args.merge:
        print("[ERROR] --merge applies to a PR; use --pr <n> --merge.", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    if args.complete:
        print("[ERROR] --complete-review applies to a PR review; use --pr <n>.",
              file=sys.stderr)
        sys.exit(EXIT_ERROR)

    if args.release:
        sys.exit(release_issue(args.issue, args.agent))
    sys.exit(claim_issue(args.issue, args.agent, args.status, args.assignee))


if __name__ == "__main__":
    main()
