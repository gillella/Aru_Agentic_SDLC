#!/usr/bin/env python3
"""
update_issue_status.py - Moves a GitHub issue's Project Board item AND its
status:* label together.

The previous version only applied a label; the board was never touched, so the
board and the CLI view of the world drifted apart immediately. It also never
removed the superseded label, so issues accumulated every status they had ever
held.
"""

import argparse
import sys

from common import get_issue, label_names, run_cmd, set_board_status

VALID_STATUSES = ["Backlog", "Ready", "In Progress", "In Review", "Done"]


def _slug(status: str) -> str:
    return "status:" + status.lower().replace(" ", "-")


def _transition_precondition_holds(
    issue_id: int,
    issue: dict,
    current_labels: set[str],
    expected_status: str | None,
    require_unclaimed: bool,
    expected_updated_at: str | None,
) -> bool:
    if expected_updated_at is not None and issue.get("updatedAt") != expected_updated_at:
        print(
            f"[CONFLICT] Issue #{issue_id} changed after qualification; "
            "refusing conditional status transition.",
            file=sys.stderr,
        )
        return False
    if expected_status is not None:
        expected_label = _slug(expected_status)
        current_statuses = sorted(
            label for label in current_labels if label.startswith("status:")
        )
        if issue.get("state", "OPEN").upper() != "OPEN" or current_statuses != [expected_label]:
            print(
                f"[CONFLICT] Issue #{issue_id} is not exactly {expected_status}; "
                "refusing conditional status transition.",
                file=sys.stderr,
            )
            return False
    if require_unclaimed and any(label.startswith("agent:") for label in current_labels):
        print(
            f"[CONFLICT] Issue #{issue_id} is claimed; refusing status transition.",
            file=sys.stderr,
        )
        return False
    return True


def update_status(
    issue_id: int,
    status: str,
    require_board: bool = False,
    expected_status: str | None = None,
    require_unclaimed: bool = False,
    expected_updated_at: str | None = None,
) -> bool:
    canonical = next((s for s in VALID_STATUSES if s.lower() == status.lower()), None)
    if not canonical:
        print(
            f"[ERROR] '{status}' is not a valid status. Expected one of: {', '.join(VALID_STATUSES)}",
            file=sys.stderr,
        )
        return False

    issue = get_issue(issue_id)
    if not issue:
        print(f"[ERROR] Issue #{issue_id} not found.", file=sys.stderr)
        return False

    target_label = _slug(canonical)
    current_labels = set(label_names(issue))
    if not _transition_precondition_holds(
        issue_id, issue, current_labels, expected_status, require_unclaimed,
        expected_updated_at,
    ):
        return False
    stale_labels = [
        _slug(s)
        for s in VALID_STATUSES
        if _slug(s) != target_label and _slug(s) in current_labels
    ]
    if canonical == "Done":
        stale_labels.extend(
            [name for name in current_labels if name.startswith("agent:")]
        )
    previous_status = next(
        (s for s in VALID_STATUSES if _slug(s) in current_labels),
        None,
    )

    print(f"Moving Issue #{issue_id} to '{canonical}'...")

    # When the board is mandatory, move it first.  This prevents a failed
    # board lookup from changing the label and leaving the two representations
    # out of sync while still returning an error to the caller.
    board_ok = set_board_status(issue_id, canonical)
    if board_ok:
        print(f"✅ Project board item moved to '{canonical}'.")
    else:
        msg = f"Issue #{issue_id} is not on any project board, or the board has no '{canonical}' option."
        if require_board:
            print(f"[ERROR] {msg}", file=sys.stderr)
            return False
        print(f"[WARN] {msg} Proceeding with a label-only update.", file=sys.stderr)

    cmd = ["gh", "issue", "edit", str(issue_id), "--add-label", target_label]
    for lbl in stale_labels:
        cmd += ["--remove-label", lbl]
    code, out, err = run_cmd(cmd, check=False)
    if code == 0:
        print(f"✅ Label '{target_label}' applied; superseded status labels removed.")
        return True

    print(f"[WARN] Label update failed: {err or out}", file=sys.stderr)
    if board_ok and previous_status:
        if set_board_status(issue_id, previous_status):
            print(
                f"[WARN] Restored project board item to '{previous_status}' after label failure.",
                file=sys.stderr,
            )
        else:
            print(
                f"[ERROR] Could not restore project board item to '{previous_status}'.",
                file=sys.stderr,
            )
    return False


def main():
    parser = argparse.ArgumentParser(description="Update GitHub issue status on both the board and its labels.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--status", type=str, required=True,
                        help=f"Target status. One of: {', '.join(VALID_STATUSES)}")
    parser.add_argument("--require-board", action="store_true",
                        help="Exit non-zero if the board item could not be moved")
    args = parser.parse_args()

    if not update_status(args.issue, args.status, args.require_board):
        sys.exit(1)


if __name__ == "__main__":
    main()
