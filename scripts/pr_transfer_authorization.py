#!/usr/bin/env python3
# line-ceiling: 80
"""Durable, head-bound operator approval for immediate PR authorship transfer."""

import json

from common import fetch_issue_comments, get_repo_slug, run_cmd

APPROVAL_VERSION = "aru-author-transfer-approval:v1"
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


def render_transfer_approval(agent: str, family: str, head: str, reason: str) -> str:
    payload = json.dumps({"agent": agent, "family": family, "head": head,
                          "reason": reason}, sort_keys=True, separators=(",", ":"))
    return ("Operator-approved authorship transfer.\n\n"
            f"<!-- {APPROVAL_VERSION} {payload} -->")


def operator_transfer_approved(
    pr_id: int, agent: str, family: str, head: str, reason: str,
) -> bool:
    """True only for an exact approval posted by a write-capable GitHub user."""
    slug = get_repo_slug()
    if not slug or not head:
        return False
    expected = render_transfer_approval(agent, family, head, reason)
    for comment in fetch_issue_comments(pr_id):
        user = comment.get("user") if isinstance(comment, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        if (comment.get("body") != expected or user.get("type") != "User"
                or not isinstance(login, str) or not login):
            continue
        code, out, _ = run_cmd(
            ["gh", "api", f"repos/{slug}/collaborators/{login}/permission",
             "--jq", ".permission"], check=False)
        if code == 0 and out.strip().casefold() in WRITE_PERMISSIONS:
            return True
    return False
