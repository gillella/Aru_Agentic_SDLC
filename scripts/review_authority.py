#!/usr/bin/env python3
"""Who may approve a merge, read from the default branch.

The rule shipped in #663 asks only that the approver is not the author, so the agent
fleet that produced a change can approve it. This resolves a per-repository posture and,
under the strict posture, requires the approving account to be one the repository has
authorized.

The declaration is read from the default branch, never from the pull-request head, so a
change cannot authorize itself. Weakening the posture, or adding a reviewer, is therefore
judged by the posture and list that are already on the default branch.

What this establishes and what it does not: it binds approval authority to named accounts
and structurally refuses GitHub Apps. It cannot establish that a person read the diff. An
agent holding a credential for a listed account is indistinguishable from its owner at
every API, so that residual is covered by credential hygiene, not by this gate.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    KernelError,
    canonical_github_actor,
    default_branch_name,
    gh_json,
    repo_slug,
    same_github_actor,
)

POLICY_PATH = ".aru/review.json"
STRICT = "human"
POSTURES = (STRICT, "any", "none")
# The shortest body that is not a reflex. Twelve characters excludes the tokens people
# type without looking -- ok, lgtm, +1, ship it -- and admits a short real sentence.
# It is a floor on effort, not evidence of reading, and the contract says so.
MIN_JUDGEMENT = 12


class Policy:
    """A repository's resolved review posture. Constructing it never widens authority."""

    def __init__(self, posture: str, reviewers: list[str], source: str) -> None:
        self.posture = posture
        self.reviewers = reviewers
        self.source = source

    @property
    def strict(self) -> bool:
        return self.posture == STRICT


def _decode(payload: Any) -> str | None:
    """Return the file text, or None when the response is not a readable file."""
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        return base64.b64decode(content).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def read_policy_text(*, cwd: str | Path | None = None) -> str | None:
    """Read the declaration from the default branch. None when the file is absent."""
    branch = default_branch_name(cwd=cwd)
    try:
        payload = gh_json(
            ["api", f"repos/{repo_slug(cwd=cwd)}/contents/{POLICY_PATH}?ref={branch}"],
            cwd=cwd,
        )
    except KernelError:
        # Absent is a legitimate state and must not be distinguishable from a denied read
        # in the permissive direction: both resolve strict below.
        return None
    return _decode(payload)


def resolve(text: str | None, *, source: str = POLICY_PATH) -> Policy:
    """Resolve a declaration to a posture. Anything unreadable resolves strict."""
    if text is None:
        return Policy(STRICT, [], f"{source} (absent)")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return Policy(STRICT, [], f"{source} (malformed)")
    if not isinstance(data, dict):
        return Policy(STRICT, [], f"{source} (malformed)")
    posture = data.get("authority")
    if posture not in POSTURES:
        posture = STRICT
    raw = data.get("reviewers")
    reviewers = (
        [name for name in raw if isinstance(name, str) and name.strip()]
        if isinstance(raw, list)
        else []
    )
    return Policy(posture, reviewers, source)


def load_policy(*, cwd: str | Path | None = None) -> Policy:
    """The repository's posture, with a safe default when nothing is declared.

    A repository that has never declared a posture resolves strict, which would otherwise
    authorize nobody and refuse its own first merge. Fall back to the account that owns
    the repository: still a named account, still not an App, and already the account that
    can administer the repository. An organization owner matches no person, so an
    organization must declare its reviewers, which the bootstrap does at creation.
    """
    policy = resolve(read_policy_text(cwd=cwd))
    if policy.strict and not policy.reviewers:
        owner = repo_slug(cwd=cwd).split("/")[0]
        return Policy(policy.posture, [owner], f"{policy.source}; defaulted to the repository owner")
    return policy


def authorized(login: str, policy: Policy) -> bool:
    return any(same_github_actor(login, name) for name in policy.reviewers)


def _latest_by_account(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The last decisive review per account. GitHub lists reviews oldest first."""
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        user = review.get("user") if isinstance(review, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise KernelError("review evidence is malformed")
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[canonical_github_actor(login)] = review
    return latest


def approvals_of_head(
    reviews: list[dict[str, Any]], *, author: str, head: str
) -> list[dict[str, Any]]:
    """Approvals of this exact head from accounts other than the author."""
    found = []
    for account, review in _latest_by_account(reviews).items():
        if review.get("state") != "APPROVED" or review.get("commit_id") != head:
            continue
        if same_github_actor(account, author):
            continue
        found.append(review)
    return found


def by_authorized_reviewer(
    approvals: list[dict[str, Any]], policy: Policy
) -> list[dict[str, Any]]:
    """Approvals the strict posture accepts.

    An App is refused even when listed: the likeliest way this degrades silently is a bot
    added to the reviewer set, and no App is an authorized human reviewer.
    """
    return [
        review
        for review in approvals
        if str(review["user"].get("type") or "").lower() != "bot"
        and authorized(str(review["user"]["login"]), policy)
    ]


def refusal(
    *,
    author: str,
    head: str,
    reviews: list[dict[str, Any]],
    policy: Policy,
    require_judgement: bool = False,
) -> str | None:
    """Why this head may not merge, or None when an acceptable approval exists.

    Asked in the order a reader would: is there an approval of this exact head at all, is
    the account allowed to give it, and is it a real judgement.
    """
    if policy.posture == "none":
        return None
    approvals = approvals_of_head(reviews, author=author, head=head)
    if not approvals:
        return "no approval of the exact head by an account other than the author"
    if not policy.strict:
        return None
    named = by_authorized_reviewer(approvals, policy)
    if not named:
        if not policy.reviewers:
            return (
                f"{policy.source} authorizes no reviewer, so no approval can satisfy the "
                f"{STRICT} posture; declare the accounts that may approve"
            )
        return (
            "no approval of the exact head by an authorized reviewer "
            f"(authorized: {', '.join(sorted(policy.reviewers))})"
        )
    if require_judgement and not any(
        len(str(review.get("body") or "").strip()) >= MIN_JUDGEMENT for review in named
    ):
        return (
            f"the approval carries no written judgement of at least {MIN_JUDGEMENT} "
            "characters"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    from common import gh_paginated

    slug = repo_slug()
    pr = gh_json(["api", f"repos/{slug}/pulls/{args.pr}"])
    author = pr.get("user", {}).get("login") if isinstance(pr.get("user"), dict) else None
    if not isinstance(author, str) or not author.strip():
        raise KernelError("PR author is unreadable")
    if str(pr.get("head", {}).get("sha") or "").lower() != args.expected_head.lower():
        raise KernelError("expected head does not match the current PR head")
    policy = resolve(read_policy_text())
    reason = refusal(
        author=author,
        head=args.expected_head,
        reviews=gh_paginated(f"repos/{slug}/pulls/{args.pr}/reviews?per_page=100"),
        policy=policy,
    )
    if args.json:
        print(json.dumps({"posture": policy.posture, "source": policy.source, "refusal": reason}))
    if reason:
        raise KernelError(reason)
    print(f"approved under the {policy.posture} posture")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KernelError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
