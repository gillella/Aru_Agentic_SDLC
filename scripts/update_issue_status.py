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

from common import run_cmd, set_board_status

VALID_STATUSES = ["Backlog", "Ready", "In Progress", "In Review", "Done"]


def _slug(status: str) -> str:
    return "status:" + status.lower().replace(" ", "-")


def update_status(issue_id: int, status: str, require_board: bool = False) -> bool:
    canonical = next((s for s in VALID_STATUSES if s.lower() == status.lower()), None)
    if not canonical:
        print(
            f"[ERROR] '{status}' is not a valid status. Expected one of: {', '.join(VALID_STATUSES)}",
            file=sys.stderr,
        )
        return False

    target_label = _slug(canonical)
    stale_labels = [_slug(s) for s in VALID_STATUSES if _slug(s) != target_label]

    print(f"Moving Issue #{issue_id} to '{canonical}'...")

    cmd = ["gh", "issue", "edit", str(issue_id), "--add-label", target_label]
    for lbl in stale_labels:
        cmd += ["--remove-label", lbl]
    code, out, err = run_cmd(cmd, check=False)
    label_ok = code == 0
    if label_ok:
        print(f"✅ Label '{target_label}' applied; superseded status labels removed.")
    else:
        print(f"[WARN] Label update failed: {err or out}", file=sys.stderr)

    board_ok = set_board_status(issue_id, canonical)
    if board_ok:
        print(f"✅ Project board item moved to '{canonical}'.")
    else:
        msg = f"Issue #{issue_id} is not on any project board, or the board has no '{canonical}' option."
        if require_board:
            print(f"[ERROR] {msg}", file=sys.stderr)
            return False
        print(f"[WARN] {msg} Label was still applied.", file=sys.stderr)

    return label_ok


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
