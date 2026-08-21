#!/usr/bin/env python3
# line-ceiling: 3630
"""merge_pr.py - the Definition-of-Done gate.

Branch protection is not available on every plan, and "CI green before merge"
written in a playbook is not a gate. This script is the gate: it refuses to
merge until the DoD is provably met, then performs the whole close-out -
merge, delete branch, prune worktree, move the board to Done - so the tail of
the lifecycle stops depending on someone remembering it.

Every refusal names the one unmet condition and exits non-zero. Nothing here
is advisory.

  python3 merge_pr.py --pr 42
  python3 merge_pr.py --pr 42 --dry-run

Exit codes:
  0 - merged (or dry-run passed every check)
  1 - error, or merge completed with resumable close-out failures
  3 - DoD not met; nothing was merged
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import acceptance_runner
from common import (
    VERIFICATION_EVIDENCE_END,
    VERIFICATION_EVIDENCE_SCHEMA,
    VERIFICATION_EVIDENCE_START,
    get_repo_slug,
    run_cmd,
)
from create_pr import render_verification_evidence, replace_verification_evidence
from update_issue_status import update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 3

# Namespace for merge checkpoints. The merge boundary is the meaningful
# rollback target; per-commit tagging was rejected as noise (#80).
CHECKPOINT_PREFIX = "ckpt/"

# Completed-review attribution, written by claim_issue.py --complete-review.
# This is the only label that satisfies the gate.
AUTHOR_LABEL = "author:"
REVIEWED_BY_LABEL = "reviewed-by:"
# The authoring agent's model family, stamped on the PR by create_pr.py.
FAMILY_LABEL = "family:"
# reviewer-family:<id>:<family> - the reviewing agent's model family, stamped by
# claim_issue.py --complete-review. Needed because the framework's identity is
# the pair (id, family) and only the PR's own family was ever recorded (#307).
REVIEWER_FAMILY_LABEL = "reviewer-family:"
REVIEW_HEAD_ATTESTATION_VERSION = "aru-review-head:v1"
# The transient claim, written by claim_review. Deliberately NOT accepted here:
# it records that an agent took the PR off the queue, not that it read anything.
# Treating it as attestation would let an author's own same-account review plus
# any peer's claim satisfy the gate before that peer had looked at the diff.
REVIEW_CLAIM_LABEL = "reviewer:"
# Transient merge-execution claim from claim_merge. Cleared on close-out; never
# treated as review attestation.
MERGER_CLAIM_LABEL = "merger:"

# Review apps can add useful findings, but their comments are not independent
# approval unless the factory operator names that App as the reviewer identity
# (#123). GitHub exposes some bot logins with a ``[bot]`` suffix and the
# Codex connector without one, so both forms must be recognized explicitly.
ADVISORY_REVIEW_ACCOUNTS = {"chatgpt-codex-connector"}
# Advisory review-bot commit-status contexts that must never gate CI. A bot
# review (e.g. CodeRabbit) posts a StatusContext that stays PENDING while it
# re-reads the diff; it is not a build check and cannot certify the head.
ADVISORY_CHECK_CONTEXTS = {"coderabbit"}
REVIEW_APP_LOGIN_ENV = "ARU_REVIEW_APP_LOGIN"
# GraphQL's review author is an Actor. Only a User can supply independent
# review evidence; all other known actor kinds are automation or identities
# whose human independence cannot be established. An unrecognized kind makes
# the query unknown rather than silently becoming trusted.
KNOWN_REVIEW_ACTOR_TYPES = {
    "App", "Bot", "EnterpriseUserAccount", "Mannequin", "Organization", "User",
}

# Large diffs must be split unless the reviewed PR body records why a waiver is
# necessary. Independent review remains a separate, mandatory gate.
SIZE_LIMIT = 400

# Review-round threshold for automated scope-reduction guidance (issue #98).
# Matches fleet_status.REWORK_ATTN. Crossing it never fails DoD and never
# creates a human gate — it emits split guidance for the authoring agent.
REVIEW_ROUND_THRESHOLD = 3
REVIEW_ROUND_SPLIT_MARKER = "<!-- aru-review-round-split:v1 -->"
# Per-follow-up provenance so retries can reuse issues created before the
# PR marker comment lands (partial emission / comment-post failure).
REVIEW_ROUND_SPLIT_ITEM_FMT = "<!-- aru-review-round-split-item:pr={pr}:idx={idx} -->"
REVIEW_ROUND_SPLIT_ITEM_RE = re.compile(
    r"<!--\s*aru-review-round-split-item:pr=(\d+):idx=(\d+)\s*-->"
)
_REWORK_BLOCKING_RE = re.compile(
    r"changes[\s_-]*requested|(?<![Nn]o )blocking findings?|\*\*blocking:\*\*",
    re.I,
)

# One initial close-out attempt plus these bounded retries.  Keeping the policy
# in the merge authority prevents desktop clients from disagreeing about when
# a transient cleanup failure becomes exceptional operator intervention.
CLOSEOUT_RETRY_DELAYS = (5, 15, 45)

PR_FIELDS = (
    "number,title,body,state,isDraft,mergeable,mergeStateStatus,baseRefName,baseRefOid,author,"
    "headRefName,headRefOid,additions,deletions,changedFiles,files,reviews,"
    "statusCheckRollup,labels,"
    "mergedAt,mergeCommit,headRepository,headRepositoryOwner,isCrossRepository"
)


def _gh_json(args, cwd=None):
    """Runs a gh command expected to emit JSON. Returns None on any failure."""
    code, out, err = run_cmd(args, check=False, cwd=cwd)
    if code != 0:
        print(f"[ERROR] {' '.join(args[:3])}...: {err.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        print(f"[ERROR] Unparseable JSON from {' '.join(args[:3])}...", file=sys.stderr)
        return None


def fetch_pr(pr_id):
    return _gh_json(["gh", "pr", "view", str(pr_id), "--json", PR_FIELDS])


def linked_issues(body):
    """Every issue this PR closes, in order of appearance.

    A PR may legitimately close several issues, and GitHub closes all of them.
    Checking only the first would let the acceptance criteria of the others
    through unverified - which is the exact hole this script exists to close.
    """
    seen, out = set(), []
    for match in re.finditer(r"\bcloses\s+#(\d+)\b", body or "", re.IGNORECASE):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def linked_issue(body):
    """The first closed issue, or None. Kept for callers that want just one."""
    issues = linked_issues(body)
    return issues[0] if issues else None


# A finding is disposed of in one of two ways: it is fixed, or it is
# withdrawn. Only the first leaves evidence in the diff, so the second has to
# say so out loud. A reply whose first top-level marker is "Withdrawn:"
# records that the
# reviewer or author retracted the finding rather than addressing it, and the
# merge audit line reports it. Without this, requiring a commit per finding
# would force agents to manufacture no-op commits to clear a thread they had
# legitimately argued down - an audit trail that actively lies is worse than
# the gap this closes.
WITHDRAWN_MARKER = re.compile(r"^(?:\*\*)?withdrawn:(?:\*\*)?(?:\s|$)", re.IGNORECASE)
SIZE_WAIVER_REGION_RE = re.compile(
    r"^\s*size-waiver:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE,
)
BODY_REMEDY_MARKER_RE = re.compile(
    r"\bbody-remedy:\s*(size-waiver|verification)\b", re.IGNORECASE,
)
SIZE_WAIVER_REQUEST_RE = re.compile(
    r"\b(?:add|include|put|record)\b[^\n]{0,160}`?size-waiver:\s*`?"
    r"[^\n]{0,120}\b(?:to|in)\s+(?:the\s+)?(?:(?:pull request|pr)\s+)?body\b",
    re.IGNORECASE,
)
VERIFICATION_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:^|\n)\s*(?:```[^\n]*\n\s*)?(?:\S+/)?python(?:3)?\s+[^\n]*"
    r"create_pr\.py[\"'`]?\s+[^\n]*--refresh-pr\b"
    r"|\b(?:run|rerun|use|invoke)\b[^\n]{0,120}`?--refresh-pr\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_ts(value):
    """ISO-8601 from the GitHub API to a comparable datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_review_ts(value):
    """A submitted GitHub review timestamp, including its timezone."""
    parsed = _parse_ts(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def _size_waiver_region(body):
    """The exact body region accepted by the existing size gate."""
    match = SIZE_WAIVER_REGION_RE.search(body or "")
    return match.group(1).strip() if match else None


def _verification_region(body):
    """The marker-delimited verification block, or None when absent/ambiguous."""
    text = body or ""
    if (
        text.count(VERIFICATION_EVIDENCE_START) != 1
        or text.count(VERIFICATION_EVIDENCE_END) != 1
    ):
        return None
    start = text.find(VERIFICATION_EVIDENCE_START)
    end = text.find(VERIFICATION_EVIDENCE_END)
    if start < 0 or end < start:
        return None
    return text[start:end + len(VERIFICATION_EVIDENCE_END)]


def _finding_body_region(body):
    """Which body-only gate remedy, if any, the finding explicitly names."""
    text = body or ""
    marker = BODY_REMEDY_MARKER_RE.search(text)
    if marker:
        return marker.group(1).lower()
    if SIZE_WAIVER_REQUEST_RE.search(text):
        return "size-waiver"
    if VERIFICATION_REQUEST_RE.search(text):
        return "verification"
    return None


def _body_edit_events(owner, name, pr_id, expected_head):  # noqa: C901, PLR0912, PLR0915
    """Verified author body-region changes, sourced from GitHub edit history.

    A bare pull-request ``updatedAt`` cannot distinguish body edits from reviews,
    labels, or comments. ``userContentEdits`` supplies immutable edit timestamps,
    editor identity, and the body snapshot after each edit. Unknown, truncated,
    or internally inconsistent history fails closed with ``None``.
    """
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          body
          author { login __typename }
          userContentEdits(first:100, after:$cursor) {
            nodes {
              id
              editedAt
              diff
              editor { login __typename }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    seen_ids = set()
    edits = []
    current_body = None
    author_login = None

    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            pull = data["data"]["repository"]["pullRequest"]
            connection = pull["userContentEdits"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if pull.get("headRefOid") != expected_head:
            return None
        body = pull.get("body")
        author = pull.get("author") or {}
        if (
            not isinstance(body, str)
            or author.get("__typename") != "User"
            or not isinstance(author.get("login"), str)
            or not author["login"]
            or not isinstance(nodes, list)
            or not isinstance(has_next, bool)
        ):
            return None
        if current_body is None:
            current_body = body
            author_login = author["login"]
        elif current_body != body or author_login != author["login"]:
            return None

        for node in nodes:
            if not isinstance(node, dict):
                return None
            edit_id = node.get("id")
            edited_at = _parse_ts(node.get("editedAt"))
            snapshot = node.get("diff")
            editor = node.get("editor") or {}
            if (
                not isinstance(edit_id, str) or not edit_id or edit_id in seen_ids
                or edited_at is None or edited_at.tzinfo is None
                or not isinstance(snapshot, str)
                or not isinstance(editor.get("__typename"), str)
                or not isinstance(editor.get("login"), str)
                or not editor["login"]
            ):
                return None
            seen_ids.add(edit_id)
            edits.append({
                "at": edited_at,
                "snapshot": snapshot,
                "by_author": (
                    editor["__typename"] == "User"
                    and editor["login"] == author_login
                ),
            })

        if not has_next:
            break
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str) or not next_cursor
            or next_cursor in seen_cursors
        ):
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if not edits:
        return {"size-waiver": [], "verification": []}

    groups = {}
    for edit in edits:
        group = groups.setdefault(
            edit["at"], {"snapshots": set(), "author_edited": False},
        )
        group["snapshots"].add(edit["snapshot"])
        group["author_edited"] = group["author_edited"] or edit["by_author"]
    ordered = []
    for edited_at in sorted(groups):
        group = groups[edited_at]
        if len(group["snapshots"]) != 1:
            return None
        ordered.append({
            "at": edited_at,
            "snapshot": next(iter(group["snapshots"])),
            "author_edited": group["author_edited"],
        })
    if ordered[-1]["snapshot"] != current_body:
        return None

    events = {"size-waiver": [], "verification": []}
    previous = ordered[0]["snapshot"]
    for edit in ordered[1:]:
        snapshot = edit["snapshot"]
        if edit["author_edited"]:
            prior_waiver = _size_waiver_region(previous)
            next_waiver = _size_waiver_region(snapshot)
            if next_waiver and next_waiver != prior_waiver:
                events["size-waiver"].append(edit["at"])

            prior_verification = _verification_region(previous)
            next_verification = _verification_region(snapshot)
            evidence, _ = parse_verification_evidence(snapshot)
            if (
                next_verification
                and next_verification != prior_verification
                and evidence is not None
                and evidence.get("head_sha") == expected_head
            ):
                events["verification"].append(edit["at"])
        previous = snapshot

    # The requested remedy must still exist in the final body. Historical
    # compliance followed by removal is not evidence at merge time.
    if _size_waiver_region(current_body) is None:
        events["size-waiver"] = []
    current_evidence, _ = parse_verification_evidence(current_body)
    if (
        current_evidence is None
        or current_evidence.get("head_sha") != expected_head
    ):
        events["verification"] = []
    return events


def _reviewed_current_head(owner, name, pr_id):  # noqa: C901, PLR0912, PLR0915
    """Returns ``(head_oid, reviewed_head, reviews)`` for every review page.

    GitHub caps connection pages at 100 entries.  Review-heavy pull requests
    therefore need an independent cursor from review-thread pagination; using
    only the first page can hide the only review submitted against the current
    head.  Every page repeats ``headRefOid`` so a concurrent push makes the
    whole result unknown instead of combining evidence from different heads.
    """
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          reviews(first:100, after:$cursor) {
            nodes {
              id
              state
              submittedAt
              body
              author { login __typename }
              commit { oid }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    expected_head = None
    reviewed_head = False
    reviews = []
    seen_review_ids = set()

    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            pull = data["data"]["repository"]["pullRequest"]
            head = pull["headRefOid"]
            connection = pull["reviews"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if (
            not isinstance(head, str) or not head
            or not isinstance(nodes, list)
            or not isinstance(has_next, bool)
        ):
            return None
        if expected_head is None:
            expected_head = head
        elif head != expected_head:
            return None

        for review in nodes:
            if not isinstance(review, dict):
                return None
            state = review.get("state")
            author = review.get("author")
            commit = review.get("commit")
            review_id = review.get("id")
            submitted_at = review.get("submittedAt")
            body = review.get("body")
            if (
                state not in {
                    "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED",
                    "PENDING",
                }
                or not isinstance(review_id, str) or not review_id
                or review_id in seen_review_ids
                or not isinstance(author, dict)
                or (commit is not None and not isinstance(commit, dict))
                or not isinstance(body, str)
            ):
                return None
            login = author.get("login")
            actor_type = author.get("__typename")
            oid = (commit or {}).get("oid")
            if (
                not isinstance(login, str) or not login
                or actor_type not in KNOWN_REVIEW_ACTOR_TYPES
                or commit is not None and (not isinstance(oid, str) or not oid)
                or state == "PENDING" and submitted_at is not None
                or state != "PENDING" and _parse_review_ts(submitted_at) is None
            ):
                return None
            seen_review_ids.add(review_id)
            reviews.append(review)
            if state in {"PENDING", "DISMISSED"}:
                continue
            if actor_type != "User" and not is_configured_review_app(login):
                continue
            if is_advisory_review_account(login):
                continue
            # In the same-account fleet, a peer's overall review and the PR
            # author's thread replies share one GitHub login. GitHub preserves
            # the important semantic distinction: the mandated peer review
            # has a substantive overall body, while a thread reply is emitted
            # as an empty COMMENTED review. Verdict reviews are substantive
            # even when their optional body is empty.
            if state == "COMMENTED" and not body.strip():
                continue
            if oid == expected_head:
                reviewed_head = True

        if not has_next:
            return expected_head, reviewed_head, reviews
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str) or not next_cursor
            or next_cursor in seen_cursors
        ):
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _review_head_attestations(owner, name, pr_id, expected_head):  # noqa: C901, PLR0912
    """Head-bound agent attestations written by complete_review().

    Pull-request comments are paginated independently from reviews and review
    threads. Malformed markers are ignored and therefore cannot create review
    evidence; the absence of a valid marker still fails the merge gate closed.
    The repeated head check prevents evidence from being combined across a
    concurrent push.
    """
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          comments(first:100, after:$cursor) {
            nodes { body author { login __typename } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    prefix = f"<!-- {REVIEW_HEAD_ATTESTATION_VERSION} "
    cursor = None
    seen_cursors = set()
    attestations = []
    while True:
        args = [
            "gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            pull = data["data"]["repository"]["pullRequest"]
            connection = pull["comments"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if (
            pull.get("headRefOid") != expected_head
            or not isinstance(nodes, list)
            or not isinstance(has_next, bool)
        ):
            return None
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("body"), str):
                return None
            body = node["body"]
            if not body.startswith(prefix):
                continue
            marker, separator, _rest = body.partition(" -->")
            if not separator:
                continue
            raw_payload = marker[len(prefix):]
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                continue
            author = node.get("author")
            if (
                not isinstance(author, dict)
                or author.get("__typename") != "User"
                or not isinstance(author.get("login"), str)
                or not author["login"]
                or not isinstance(payload, dict)
                or set(payload) != {"agent", "head"}
                or not isinstance(payload.get("agent"), str)
                or not payload["agent"]
                or not isinstance(payload.get("head"), str)
                or re.fullmatch(r"[0-9a-fA-F]{40,64}", payload["head"]) is None
            ):
                continue
            attestations.append({
                "agent": payload["agent"],
                "head": payload["head"].lower(),
                "github_login": author["login"],
            })
        if not has_next:
            return attestations
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str) or not next_cursor
            or next_cursor in seen_cursors
        ):
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def review_evidence(pr_id):  # noqa: C901, PLR0912, PLR0915
    """Facts the review gate needs beyond a count of open threads.

    Returns ``None`` on any query failure - an unknown review state must never
    merge - otherwise a dict:

      ``unresolved``  open, non-outdated threads.
      ``unfixed``     threads resolved with no commit after the finding was
                      raised and no explicit withdrawal. This is the shape that
                      let PR #62 merge with five blocking findings intact:
                      resolving a thread is a UI toggle and proves nothing
                      about the code.
      ``body_addressed`` threads resolved after an author edit changed the
                      exact size-waiver or current-head verification region.
      ``withdrawn``   threads whose resolution was declared a withdrawal.
      ``reviewed_head``  True when at least one substantive, non-advisory
                      review was submitted against the current head. A review
                      of an earlier commit attests to code that is no longer
                      proposed, so a push after review must invalidate it.

    Commit ordering is not proof of causation - a commit landing after a
    finding may be unrelated. It is the weaker claim the gate can actually
    check, and it is only ever used to refuse, never to approve.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    review_result = _reviewed_current_head(owner, name, pr_id)
    if review_result is None:
        return None
    expected_head, reviewed_head, reviews = review_result
    review_attestations = _review_head_attestations(
        owner, name, pr_id, expected_head
    )
    if review_attestations is None:
        return None
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          commits(last:100) {
            nodes { commit { committedDate } }
          }
          reviewThreads(first:100, after:$cursor) {
            nodes {
              isResolved
              isOutdated
              comments(first:50) { nodes { createdAt body } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""

    cursor = None
    seen_cursors = set()
    unresolved = 0
    unfixed = 0
    outdated_unfixed = 0
    outdated_addressed = 0
    withdrawn = 0
    body_addressed = 0
    body_edit_events = None
    commit_times = None

    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            pull = data["data"]["repository"]["pullRequest"]
            connection = pull["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list) or not isinstance(has_next, bool):
            return None
        if pull.get("headRefOid") != expected_head:
            return None

        # Commits do not change between thread pages; read once. The repeated
        # head check above rejects a concurrent push on every page.
        if commit_times is None:
            try:
                commit_times = sorted(
                    ts for ts in (
                        _parse_ts(((c or {}).get("commit") or {}).get("committedDate"))
                        for c in (pull.get("commits") or {}).get("nodes") or []
                    ) if ts is not None
                )
            except (AttributeError, TypeError):
                return None

        for node in nodes:
            outdated = bool(node.get("isOutdated"))
            resolved = bool(node.get("isResolved"))
            comments = (node.get("comments") or {}).get("nodes") or []

            if not resolved and not outdated:
                unresolved += 1
                continue

            if not comments:
                if not resolved and outdated:
                    outdated_unfixed += 1
                continue

            if any(WITHDRAWN_MARKER.search(c.get("body") or "") for c in comments):
                withdrawn += 1
                continue

            raised = _parse_ts(comments[0].get("createdAt"))
            has_commit_after = (raised is not None) and any(ts > raised for ts in commit_times)

            if not resolved and outdated:
                if not has_commit_after:
                    outdated_unfixed += 1
                else:
                    outdated_addressed += 1
                continue

            if resolved:
                if has_commit_after:
                    continue
                region = _finding_body_region(comments[0].get("body") or "")
                has_body_edit_after = False
                if region is not None and raised is not None:
                    if body_edit_events is None:
                        body_edit_events = _body_edit_events(
                            owner, name, pr_id, expected_head,
                        )
                        if body_edit_events is None:
                            return None
                    has_body_edit_after = any(
                        edited_at > raised
                        for edited_at in body_edit_events.get(region, [])
                    )
                if has_body_edit_after:
                    body_addressed += 1
                else:
                    unfixed += 1

        if not has_next:
            return {
                "head_oid": expected_head,
                "reviews": reviews,
                "review_attestations": review_attestations,
                "unresolved": unresolved,
                "unfixed": unfixed,
                "outdated_unfixed": outdated_unfixed,
                "outdated_addressed": outdated_addressed,
                "body_addressed": body_addressed,
                "withdrawn": withdrawn,
                "reviewed_head": reviewed_head,
            }
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def unresolved_threads(pr_id):
    """Counts unresolved review threads.

    gh pr view cannot report thread resolution, so this drops to GraphQL.
    Returns None when the query fails, which the caller treats as a refusal
    rather than a pass - an unknown review state must never merge.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          reviewThreads(first:100, after:$cursor) {
            nodes { isResolved isOutdated }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    unresolved = 0
    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list) or not isinstance(has_next, bool):
            return None
        unresolved += sum(
            1 for node in nodes
            if not node.get("isResolved") and not node.get("isOutdated")
        )
        if not has_next:
            return unresolved
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def unticked_criteria(issue_body):
    """Returns the acceptance-criteria lines still unchecked.

    Only counts checkboxes under an 'Acceptance Criteria' heading; a checklist
    elsewhere in the body (a reviewer's notes, say) must not gate the merge.
    Criteria that carry a `verify:` command are executed by the runner instead
    of being certified by a tick.
    """
    return [
        item.text
        for item in acceptance_runner.parse_criteria(issue_body)
        if item.argv is None and not item.ticked
    ]


# --- Individual gates -------------------------------------------------------
# Each returns (passed, message). Kept pure and separate so the test suite can
# drive every refusal path without a network.

def check_open(pr):
    if pr.get("state") != "OPEN":
        return False, f"PR is {pr.get('state','?').lower()}, not open."
    if pr.get("isDraft"):
        return False, "PR is still a draft."
    return True, "PR is open."


def _check_name(check):
    """Check runs carry 'name'; legacy commit statuses carry 'context'."""
    return check.get("name") or check.get("context") or "check"


def _check_time(check):
    """When this run finished, for ordering runs of the same check.

    Falls back to the start time when a run has not completed. Returns None
    when neither timestamp is usable, which the caller treats as "cannot be
    ordered" rather than "is current".
    """
    raw = check.get("completedAt") or check.get("startedAt")
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Allowlist, not denylist. Enumerating the failure conclusions let unknown ones -
# STARTUP_FAILURE, STALE, anything GitHub adds later - fall through to "green"
# and merge an unverified head. Only these three mean "passed"; every other
# completed conclusion fails closed.
PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
IN_PROGRESS_STATES = {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS",
                      "WAITING", "REQUESTED"}


def _check_outcome(check):
    """Raw (status, result) for one run.

    Check runs report 'conclusion'; legacy commit statuses report 'state'.
    """
    return (
        (check.get("status") or "").upper(),
        (check.get("conclusion") or check.get("state") or "").upper(),
    )


def _check_verdict(check):
    """The (verdict, detail) this run contributes: pending, passing, or failing.

    Ties between two runs are compared on this rather than on the raw fields. A
    check run ({status: COMPLETED, conclusion: SUCCESS}) and a legacy commit
    status ({state: SUCCESS}) describe one green outcome through different
    fields, so comparing raw tuples would call them a disagreement and block a
    PR that check_ci itself treats as green.

    check_ci reads the same helper, so the tie-break and the verdict cannot
    disagree about what a run means.
    """
    status, result = _check_outcome(check)
    if (status and status != "COMPLETED" and not result) or result in IN_PROGRESS_STATES:
        return ("pending", "")
    if result in PASSING_CONCLUSIONS:
        return ("passing", "")
    return ("failing", result.lower() or "unknown")


def _current_runs(rollup):
    """Reduce the rollup to the live run of each check name.

    GitHub keeps every run recorded against the head commit: the push-triggered
    run, the pull_request-triggered run, and every re-run. Judging all of them
    means one superseded failure holds the gate red forever, so re-running a
    job green cannot unblock a PR and only an operator can (#306).

    Array order carries no recency guarantee, so ordering comes from the
    timestamps. A name with a single entry needs no ordering - that entry is
    the run. A name with several entries where any timestamp is unusable is
    reported as unorderable and fails closed: guessing which run is live is
    exactly the mistake being fixed.
    """
    groups = {}
    for check in rollup:
        groups.setdefault(_check_name(check), []).append(check)

    current, unorderable = {}, []
    for name, runs in groups.items():
        if len(runs) == 1:
            current[name] = runs[0]
            continue
        stamped = [(_check_time(run), run) for run in runs]
        if any(when is None for when, _ in stamped):
            unorderable.append(name)
            continue
        newest = max(when for when, _ in stamped)
        tied = [run for when, run in stamped if when == newest]
        # max() returns the first maximal element, which is array order -- the
        # one thing this function documents as carrying no recency evidence. Two
        # runs finishing in the same second would otherwise decide a merge by
        # response ordering. Ties that agree on the outcome are harmless; ties
        # that disagree are undecidable.
        if len({_check_verdict(run) for run in tied}) > 1:
            unorderable.append(name)
            continue
        current[name] = tied[0]
    return current, sorted(unorderable)


def check_ci(pr):
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return False, "No CI checks reported on the head commit. A PR with no checks is not verified."
    current, unorderable = _current_runs(rollup)
    # An advisory review bot is not a build check, so it must not hold the gate
    # by being unresolvable either (see ADVISORY_CHECK_CONTEXTS).
    unorderable = [name for name in unorderable
                   if (name or "").lower() not in ADVISORY_CHECK_CONTEXTS]
    if unorderable:
        return False, (
            f"CI recency is undecidable for: {', '.join(unorderable)}. "
            "Several runs of one check either carry no usable timestamp or are "
            "tied on the newest one while disagreeing, so which is current "
            "cannot be established."
        )

    failing, pending = [], []
    for name in sorted(current):
        if (name or "").lower() in ADVISORY_CHECK_CONTEXTS:
            # Advisory review bots are not build checks; a stuck PENDING status
            # from one must not hold the CI gate (see ADVISORY_CHECK_CONTEXTS).
            continue
        verdict, detail = _check_verdict(current[name])
        if verdict == "pending":
            pending.append(name)
        elif verdict == "failing":
            failing.append(f"{name}={detail}")
    if failing:
        return False, f"CI is red: {', '.join(failing)}."
    if pending:
        return False, f"CI has not finished: {', '.join(pending)}."
    return True, f"CI green ({len(current)} checks)."


def label_values(pr, prefix):
    """All values of labels sharing a prefix, e.g. every reviewed-by:<id>."""
    return [
        (lab.get("name") or "")[len(prefix):]
        for lab in (pr.get("labels") or [])
        if (lab.get("name") or "").startswith(prefix)
    ]


def latest_state_per_reviewer(reviews):
    """Collapses review history to each reviewer's most recent verdict.

    `reviews` is the full submission history, so a reviewer who requested
    changes and later approved still has the CHANGES_REQUESTED entry in it.
    Reading the raw list blocks such a PR forever, contradicting the message
    that says re-approval is supported. Only the last word from each reviewer
    counts.
    """
    latest = {}
    for review in reviews:
        state = (review.get("state") or "").upper()
        if state in {"PENDING", "COMMENTED"}:
            # A comment-only review does not change a prior verdict, and a
            # pending one was never submitted.
            continue
        who = ((review.get("author") or {}).get("login") or review.get("id"))
        submitted_at = _parse_review_ts(review.get("submittedAt"))
        if not isinstance(who, str) or not who or submitted_at is None:
            return None
        prior = latest.get(who)
        if prior is not None:
            if submitted_at == prior[0]:
                # GitHub IDs distinguish the submissions, but equal timestamps
                # cannot prove which verdict is newer. Never trust page order.
                return None
            if submitted_at < prior[0]:
                continue
        latest[who] = (submitted_at, state)
    return {who: state for who, (_, state) in latest.items()}


def configured_review_app_logins():
    """Operator-configured GitHub App logins trusted as independent reviewers."""
    raw = os.environ.get(REVIEW_APP_LOGIN_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_configured_review_app(login):
    """True when ``login`` is the installed reviewer App from #123."""
    normalized = (login or "").lower()
    return bool(normalized) and normalized in configured_review_app_logins()


def is_advisory_review_account(login):
    """Whether a GitHub reviewer identity belongs to review automation."""
    if is_configured_review_app(login):
        return False
    normalized = (login or "").lower()
    return normalized.endswith("[bot]") or normalized in ADVISORY_REVIEW_ACCOUNTS


def is_advisory_review_actor(review):
    """Whether a review's GraphQL actor cannot provide independent attestation."""
    author = review.get("author") or {}
    actor_type = author.get("__typename")
    login = author.get("login") or ""
    if is_configured_review_app(login):
        return False
    return (
        actor_type is not None and actor_type != "User"
    ) or is_advisory_review_account(login)


def _current_head_reviewers(evidence):
    """Known human accounts with substantive reviews on the evidenced head."""
    if not evidence:
        return []
    head = evidence.get("head_oid")
    if not isinstance(head, str) or not head:
        return []
    reviewers = set()
    for review in evidence.get("reviews") or []:
        state = (review.get("state") or "").upper()
        author = review.get("author") or {}
        login = author.get("login") or ""
        actor_type = author.get("__typename")
        oid = (review.get("commit") or {}).get("oid")
        body = review.get("body")
        if (
            oid == head
            and (actor_type == "User" or is_configured_review_app(login))
            and not is_advisory_review_actor(review)
            and state not in {"PENDING", "DISMISSED"}
            and (state != "COMMENTED" or isinstance(body, str) and body.strip())
        ):
            reviewers.add(login)
    return sorted(reviewers)


def identity_values(pr, prefix):
    """Label values that actually name an agent: trimmed, empties dropped.

    A bare `author:` label parses to "", which no reviewer id can equal, so
    every reviewer read as an independent peer and a self-review satisfied the
    review gate -- an authorization bypass in the one check that exists to prove
    independence. A bare `reviewed-by:` fails open the same way from the other
    side. Neither names an agent, so neither may take part in the comparison.
    """
    return [value for value in
            (raw.strip() for raw in label_values(pr, prefix)) if value]


def reviewer_families(pr):
    """Map reviewer id -> the distinct families stamped for it.

    A list rather than a single value on purpose. Two conflicting
    reviewer-family labels for one id are themselves evidence of the id-reissue
    defect this module exists to detect, so letting the last one win would hide
    the very signal worth reporting.
    """
    families = {}
    for value in label_values(pr, REVIEWER_FAMILY_LABEL):
        agent_id, _, family = value.partition(":")
        if agent_id and family:
            families.setdefault(agent_id, [])
            if family not in families[agent_id]:
                families[agent_id].append(family)
    return {agent_id: sorted(values) for agent_id, values in families.items()}


def classify_reviewers(pr, reviewers, author):
    """Split reviewers into genuine peers, id collisions, and unresolvable ones.

    Identity in this framework is the pair (id, family). Agents all authenticate
    as one GitHub user, so the labels are the only thing that tells them apart,
    and the gate previously compared the id alone. That makes a
    same-id/different-family reviewer -- which is evidence of the #304 id-reissue
    defect, not a self-review -- indistinguishable from the author reviewing its
    own work.

    Family is consulted only where identity is actually contested. A reviewer
    whose id differs from the author's is a peer whatever its family, so PRs
    predating family stamping keep merging exactly as before. Where the ids do
    match, a missing family is reported rather than guessed at.

    Returns (peers, collisions, unresolved):
      peers       reviewer ids that are genuinely somebody else
      collisions  (id, author_family, reviewer_family) - one id, two agents
      unresolved  (id, [what is missing]) - cannot be decided, fails closed
    """
    author_families = sorted(set(label_values(pr, FAMILY_LABEL)))
    families = reviewer_families(pr)
    peers, collisions, unresolved = [], [], []
    for agent_id in reviewers:
        if agent_id != author:
            peers.append(agent_id)
            continue
        reviewer_values = families.get(agent_id, [])
        # An identity stamped with two different families is not a family we can
        # compare; it is an ambiguity, and reporting it as one beats picking
        # either and describing the wrong situation.
        if len(author_families) > 1 or len(reviewer_values) > 1:
            ambiguous = (f"the PR ({', '.join(author_families)})"
                         if len(author_families) > 1
                         else f"reviewer '{agent_id}' ({', '.join(reviewer_values)})")
            collisions.append((agent_id, f"ambiguous on {ambiguous}", "unresolvable"))
            continue
        author_family = author_families[0] if author_families else ""
        reviewer_family = reviewer_values[0] if reviewer_values else ""
        if not author_family or not reviewer_family:
            missing = []
            if not author_family:
                missing.append(f"{FAMILY_LABEL}<family> on the PR")
            if not reviewer_family:
                missing.append(
                    f"{REVIEWER_FAMILY_LABEL}{agent_id}:<family> for the review"
                )
            unresolved.append((agent_id, missing))
        elif reviewer_family != author_family:
            collisions.append((agent_id, author_family, reviewer_family))
    return peers, collisions, unresolved


def id_collision_message(collisions):
    """Refusal text for one agent id stamped as both author and reviewer.

    Deliberately neither an acceptance nor a self-review rejection: the two
    families prove two different agents are answering to one id, so the honest
    report is that the id namespace broke, not that somebody reviewed its own
    work. Blocking here is what stops the #304 collision from merging.
    """
    agent_id, author_family, reviewer_family = collisions[0]
    return (
        f"Agent id '{agent_id}' is stamped as both the author "
        f"(family:{author_family}) and a reviewer (family:{reviewer_family}) of "
        "this PR. Two agents are sharing one id, so no attribution on it can be "
        "trusted - this is neither a self-review nor a valid peer review. "
        "Reissue one of them a distinct id (see #304) and re-review."
    )


def self_review_message(author, unresolved):
    """Refusal text when the author's id is the only attribution on the PR.

    When the family labels needed to rule out an id collision are absent, the
    gate says so instead of quietly assuming the two are the same agent. It
    still refuses either way, so the caveat costs nothing and names exactly what
    an operator must stamp to tell the two situations apart.
    """
    message = (f"The only review is from '{author}', who wrote this PR. "
               "A self-review does not satisfy the gate.")
    if unresolved:
        _agent_id, missing = unresolved[0]
        message += (
            f" Note: {' and '.join(missing)} is missing, so a second agent "
            "sharing this id (#304) cannot be ruled out - stamp the family "
            "labels if that is what happened. The gate will not assume a family."
        )
    return message


def _attested_head_peers(evidence, peers):
    """Attributed peer agents whose completion stamp matches this exact head."""
    if not evidence or "review_attestations" not in evidence:
        return None
    head = evidence.get("head_oid")
    if not isinstance(head, str) or not head:
        return []
    peer_set = set(peers)
    return sorted({
        item.get("agent") for item in evidence.get("review_attestations") or []
        if isinstance(item, dict)
        and item.get("agent") in peer_set
        and item.get("head") == head.lower()
    })


def _evidence_note(evidence):
    """What actually satisfied the review gate, for the audit line.

    A later reader needs to tell "every finding was fixed" from "the findings
    were withdrawn", because those justify a merge very differently.
    """
    out_addressed = evidence.get("outdated_addressed", 0) if evidence else 0
    head = evidence.get("head_oid") if evidence else None
    head_reviewers = _current_head_reviewers(evidence)
    if head and head_reviewers:
        head_note = (
            f"current head {head[:12]} has substantive independent review from "
            f"{', '.join(head_reviewers)}"
        )
    else:
        # Compatibility for pure unit callers whose handcrafted evidence
        # predates commit/actor details. Live evidence always uses the branch
        # above, keeping attribution and commit freshness visibly separate.
        head_note = "reviewed at head"
    if out_addressed > 0:
        parts = [
            head_note,
            f"no blocking unresolved threads ({out_addressed} outdated with commit evidence)",
        ]
    else:
        parts = [head_note, "no unresolved threads"]
    if evidence and evidence.get("body_addressed"):
        parts.append(
            f"{evidence['body_addressed']} finding(s) addressed by relevant PR body edit"
        )
    if evidence and evidence.get("withdrawn"):
        parts.append(f"{evidence['withdrawn']} finding(s) withdrawn, not fixed")
    return ", ".join(parts) + "."


def check_reviews(pr, evidence):  # noqa: C901, PLR0912
    # Prefer the same explicitly paginated review history used for current-head
    # evidence. The PR snapshot remains a compatibility fallback for pure
    # unit-level callers that supply handcrafted evidence.
    reviews = (
        evidence.get("reviews")
        if evidence is not None and "reviews" in evidence
        else pr.get("reviews")
    ) or []
    substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
    if not substantive:
        return False, "No review on this PR. At least one review is required."
    verdicts = latest_state_per_reviewer(reviews)
    if verdicts is None:
        return False, (
            "Could not establish an unambiguous latest review verdict; "
            "refusing rather than trusting review page order."
        )
    advisory_accounts = {
        ((r.get("author") or {}).get("login") or "").lower()
        for r in substantive if is_advisory_review_actor(r)
    }
    blocking = [
        who for who, state in verdicts.items()
        if state == "CHANGES_REQUESTED"
        and who.lower() not in advisory_accounts
        and not is_advisory_review_account(who)
    ]
    if blocking:
        return False, (f"{', '.join(blocking)} requested changes and has not re-approved.")
    if evidence is None:
        return False, "Could not determine review-thread state; refusing rather than guessing."
    unres = evidence.get("unresolved", 0)
    out_unfixed = evidence.get("outdated_unfixed", 0)
    if unres > 0 or out_unfixed > 0:
        total = unres + out_unfixed
        if unres > 0 and out_unfixed > 0:
            return False, f"{total} unresolved review thread(s) ({out_unfixed} outdated without evidence)."
        elif unres > 0:
            return False, f"{unres} unresolved review thread(s)."
        else:
            return False, f"{out_unfixed} outdated review thread(s) without evidence."

    # Resolving a thread is a UI toggle with no relationship to the diff, so
    # zero-unresolved alone certified PR #62's five blocking findings as
    # addressed while every one of them survived to main. A finding must have
    # been fixed - some commit followed it - or explicitly withdrawn.
    if evidence["unfixed"] > 0:
        return False, (
            f"{evidence['unfixed']} resolved thread(s) have no evidence that the "
            "finding was addressed: no commit after the finding was raised, no relevant "
            "size-waiver or verification-evidence body edit after it was raised, "
            "and no reply starting with 'Withdrawn:'. Push the fix, apply the "
            "documented body-only gate remedy when it matches the finding, or "
            "withdraw the finding with a reason."
        )

    # A claim means an independent agent is still reviewing. It must block
    # before any external-account or completed-attribution shortcut, otherwise
    # a bot comment can make the PR mergeable while that reviewer is working.
    claimants = label_values(pr, REVIEW_CLAIM_LABEL)
    if claimants:
        return False, (
            f"Review is still in progress: {', '.join(claimants)} holds a "
            f"{REVIEW_CLAIM_LABEL}<agent> claim. Complete the review with "
            "`claim_issue.py --pr <n> --agent <id> --complete-review`, or "
            "release the claim if no review was performed."
        )

    # GitHub cannot tell a self-review from a peer review here: every agent
    # authenticates as the same user, so every review looks like it came from
    # the same person who opened the PR. The agent identity labels are the only
    # thing that distinguishes them.
    # A review from a different non-automation GitHub account is provably not a
    # self-review, but only its latest APPROVED verdict counts. Unconfigured
    # review apps stay advisory. An App login named in ARU_REVIEW_APP_LOGIN is
    # the #123 reviewer identity and counts as that external account.
    pr_login = ((pr.get("author") or {}).get("login") or "").lower()
    other_accounts = sorted({
        ((r.get("author") or {}).get("login") or "").lower()
        for r in substantive if not is_advisory_review_actor(r)
    } - {"", pr_login})

    # Authorship is required before any approval path can pass. Without the
    # governed author stamp, even a genuine external approval cannot prove the
    # PR did not bypass create_pr.py or establish who must be excluded from
    # same-account agent review.
    authors = identity_values(pr, AUTHOR_LABEL)
    if not authors:
        return False, (
            "PR has no author:<id> label, so the gate cannot prove that the "
            "reviewer is independent. Create PRs with "
            "`scripts/create_pr.py --issue <n> --agent <id>`; stamp the verified "
            "author on a legacy PR before retrying."
        )
    if len(set(authors)) > 1:
        # Resolving this by position would pick an author arbitrarily, and the
        # whole peer comparison below rests on knowing who wrote the PR.
        return False, (
            f"PR carries {len(set(authors))} different {AUTHOR_LABEL} labels "
            f"({', '.join(sorted(set(authors)))}), so who wrote it cannot be "
            "established. Two agents likely adopted it concurrently; remove the "
            "stale label before merging."
        )
    author = authors[0]

    current_head_reviewers = set(_current_head_reviewers(evidence))
    legacy_head_evidence = (
        "review_attestations" not in evidence and evidence.get("reviewed_head")
    )
    external_approvers = sorted(
        who for who, state in verdicts.items()
        if who.lower() in other_accounts
        and (who in current_head_reviewers or legacy_head_evidence)
        and state == "APPROVED"
        and not is_advisory_review_account(who)
    )
    if external_approvers:
        note = (
            f"Approved by external reviewer(s) {', '.join(external_approvers)}, "
            f"{_evidence_note(evidence)}"
        )
        if any((lab.get("name") or "") == "same-family-review"
               for lab in (pr.get("labels") or [])):
            note += " ⚠️  Same-family review: no cross-family agent was available."
        return True, note

    # Everything below is the same-account case: agents all authenticate as one
    # GitHub user, so only the identity labels can tell them apart.
    # Only completed attribution counts. Active reviewer claims were rejected
    # above because they represent work still in progress, not attestation.
    reviewers = identity_values(pr, REVIEWED_BY_LABEL)
    peers, collisions, unresolved = classify_reviewers(pr, reviewers, author)
    # A collision blocks even when a genuine peer also reviewed: the operator
    # needs to know the id namespace broke. A merely unstamped family does not,
    # or every PR predating family stamping would stop merging.
    if collisions:
        return False, id_collision_message(collisions)
    if reviewers and not peers:
        return False, self_review_message(author, unresolved)
    if not reviewers:
        advisory = sorted(a for a in advisory_accounts if a and a != pr_login)
        if advisory:
            return False, (
                f"Automated review from {', '.join(advisory)} is advisory; no "
                f"{REVIEWED_BY_LABEL}<agent> label attributes a completed independent "
                "agent review."
            )
        return False, (f"A review exists but no {REVIEWED_BY_LABEL}<agent> label identifies "
                       f"who left it, so it cannot be distinguished from a self-review by "
                       f"'{author}'. The reviewing agent must finish with "
                       f"`claim_issue.py --pr <n> --agent <id> --complete-review`.")

    attested_peers = _attested_head_peers(evidence, peers)
    if attested_peers is not None and not attested_peers:
        head = evidence.get("head_oid")
        head_text = (
            f"current head {head[:12]}"
            if isinstance(head, str) and head else "the current head"
        )
        return False, (
            f"Completed peer attribution exists for {', '.join(peers)}, but "
            f"none is bound to {head_text}. The attribution may be stale; "
            "the peer must re-review and complete the current commit."
        )
    if attested_peers and not evidence.get("reviewed_head"):
        return False, (
            f"Peer attribution for {', '.join(attested_peers)} names the current "
            "head, but no substantive review targets that commit. Re-review the "
            "current commit."
        )

    # Compatibility for pure unit callers predating the attestation field.
    # Live review_evidence always includes it, so the production merge path
    # cannot fall back to an unbound reviewed-by label.
    if attested_peers is None and not evidence["reviewed_head"]:
        return False, (
            "Every review predates the current head, so no reviewer has seen "
            "what would merge. Re-review the current commit."
        )

    note = (
        f"Peer attribution: {', '.join(attested_peers or peers)}; "
        f"{_evidence_note(evidence)}"
    )
    if any((lab.get("name") or "") == "same-family-review" for lab in (pr.get("labels") or [])):
        note += " ⚠️  Same-family review: no cross-family agent was available."
    return True, note


def _behind_by(base_ref, head_sha):
    """Commits `head_sha` is behind `base_ref`, or None if it cannot be determined.

    GitHub only sets mergeStateStatus to BEHIND when the base branch has
    protection requiring branches to be up to date. On an unprotected repo --
    the default for a newly governed project -- that field never expresses
    staleness at all, so a gate reading it alone can never fail. The compare
    API reports behind_by unconditionally.
    """
    if not base_ref or not head_sha:
        return None
    slug = get_repo_slug()
    if not slug:
        return None
    data = _gh_json(["gh", "api", f"repos/{slug}/compare/{base_ref}...{head_sha}"])
    if not isinstance(data, dict):
        return None
    behind = data.get("behind_by")
    # bool is an int subclass; True would otherwise read as "1 commit behind".
    if isinstance(behind, bool) or not isinstance(behind, int) or behind < 0:
        return None
    return behind


def check_rebased(pr, behind_resolver=None):
    state = (pr.get("mergeStateStatus") or "").upper()
    if state == "BEHIND":
        return False, "Branch is behind the base. Rebase on main and re-run."
    if state == "DIRTY":
        return False, "Branch has merge conflicts with the base."
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "Branch conflicts with the base."
    # The checks above are a fast path, not the authority: they only fire on
    # repos configured to surface staleness. Ask for ancestry directly, and
    # treat unknown as not-current. A gate that cannot fail is worse than an
    # absent one, because the Definition of Done then asserts a property
    # nothing verified.
    resolve = behind_resolver or _behind_by
    try:
        behind = resolve(pr.get("baseRefName"), pr.get("headRefOid"))
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        return False, (
            f"Could not determine whether the branch is current with the base "
            f"({type(exc).__name__}: {exc}). Refusing to merge on unverified ancestry."
        )
    if behind is None:
        return False, (
            "Could not determine whether the branch is current with the base. "
            "Refusing to merge on unverified ancestry."
        )
    if behind > 0:
        plural = "commit" if behind == 1 else "commits"
        return False, (
            f"Branch is {behind} {plural} behind the base. Rebase on main and re-run."
        )
    return True, "Branch is current with the base."


def check_issue_link(pr):
    issues = linked_issues(pr.get("body"))
    if not issues:
        return False, "PR body has no 'Closes #<issue>'. Every PR must close a tracked issue."
    return True, "Linked to " + ", ".join(f"#{i}" for i in issues) + "."


def parse_verification_evidence(body):  # noqa: C901
    """Parses the marker-delimited verification JSON without scraping prose."""
    body = body or ""
    start_count = body.count(VERIFICATION_EVIDENCE_START)
    end_count = body.count(VERIFICATION_EVIDENCE_END)
    if start_count == 0 and end_count == 0:
        return None, "missing"
    if start_count != 1 or end_count != 1:
        return None, "verification evidence markers are unmatched or duplicated"
    if body.index(VERIFICATION_EVIDENCE_START) > body.index(VERIFICATION_EVIDENCE_END):
        return None, "verification evidence markers are inverted"
    _before, marker, remainder = body.partition(VERIFICATION_EVIDENCE_START)
    payload, end_marker, _after = remainder.partition(VERIFICATION_EVIDENCE_END)
    payload = payload.strip()
    if payload.startswith("```json"):
        payload = payload[len("```json"):].lstrip()
    if payload.endswith("```"):
        payload = payload[:-3].rstrip()

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        evidence = json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"invalid verification JSON: {exc}"
    if not isinstance(evidence, dict):
        return None, "verification evidence must be a JSON object"
    if evidence.get("schema") != VERIFICATION_EVIDENCE_SCHEMA:
        return None, "unsupported verification evidence schema"
    return evidence, None


def check_verification(pr):  # noqa: C901, PLR0912
    """Validates recorded commands while warning on legacy or not-run PRs."""
    evidence, error = parse_verification_evidence(pr.get("body") or "")
    if error == "missing":
        return True, "⚠️  No verification evidence block; legacy warning only."
    if error:
        return False, f"Verification evidence is malformed: {error}."

    status = evidence.get("status")
    commands = evidence.get("commands")
    evidence_head = evidence.get("head_sha")
    pr_head = pr.get("headRefOid")
    if not isinstance(evidence_head, str) or not evidence_head:
        return False, "Verification evidence is missing the tested head SHA."
    if not isinstance(pr_head, str) or not pr_head:
        return False, "The live PR head SHA is unavailable; verification cannot be bound."
    if evidence_head != pr_head:
        return False, (
            f"Verification was recorded for {evidence_head}, but the PR head is {pr_head}; "
            "refresh evidence with create_pr.py --refresh-pr."
        )
    if not isinstance(commands, list):
        return False, "Verification evidence commands must be a JSON array."
    if status == "not_run":
        if commands:
            return False, "Verification status is not_run but command records are present."
        return True, "⚠️  Local verification was explicitly not run; warning-only rollout."
    if status not in {"passed", "failed"}:
        return False, f"Unknown verification status: {status!r}."
    if not commands:
        return False, f"Verification status is {status} but no commands were recorded."

    for record in commands:
        if not isinstance(record, dict):
            return False, "Each verification command record must be a JSON object."
        if (not isinstance(record.get("command"), list)
                or not record["command"]
                or not all(type(arg) is str for arg in record["command"])):
            return False, "A verification command is missing its argv array."
        if type(record.get("exit_code")) is not int:
            return False, "A verification command is missing an integer exit_code."
        duration = record.get("duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            return False, "A verification command has an invalid duration_seconds."
        expected = "passed" if record["exit_code"] == 0 else "failed"
        if record.get("status") != expected:
            return False, "A verification command status disagrees with its exit_code."

    failed = [record for record in commands if record["exit_code"] != 0]
    if status == "failed" or failed:
        return False, f"Local verification recorded {len(failed)} failing command(s)."
    return True, f"Local verification passed {len(commands)} recorded command(s)."


def ensure_pr_head_checkout(pr, repo_root=None):
    """Materializes a clean detached checkout of the exact PR head SHA.

    Never falls back to the caller's cwd: a merger clone often has only
    ``main``, and running verify: commands there can false-pass or false-fail.
    Never shallow-fetches into that clone: worktrees share its Git metadata,
    and ``--depth=1`` writes ``.git/shallow`` for every worktree.
    """
    sha = (pr or {}).get("headRefOid")
    if not isinstance(sha, str) or not sha:
        return None, "PR is missing a head SHA"
    repo_root = repo_root or repository_root() or os.getcwd()
    dest = tempfile.mkdtemp(prefix=f"aru-accept-{sha[:12]}-")
    # Never pass --depth=1 here: every worktree shares this clone's Git
    # metadata, and a shallow fetch writes `.git/shallow`, which breaks
    # merge-base / rebase / diff for the rest of the factory.
    fetch_code, _, fetch_err = run_cmd(
        ["git", "fetch", "--no-tags", "origin", sha],
        check=False,
        cwd=repo_root,
    )
    if fetch_code != 0:
        ref = (pr or {}).get("headRefName")
        if ref:
            fetch_code, _, fetch_err = run_cmd(
                ["git", "fetch", "--no-tags", "origin", ref],
                check=False,
                cwd=repo_root,
            )
    add_code, _, add_err = run_cmd(
        ["git", "worktree", "add", "--detach", dest, sha],
        check=False,
        cwd=repo_root,
    )
    if add_code != 0:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (add_err or fetch_err or "worktree add failed").strip()
        return None, f"could not materialize PR head {sha}: {detail}"
    code, head, _ = run_cmd(["git", "rev-parse", "HEAD"], check=False, cwd=dest)
    if code != 0 or head.strip() != sha:
        release_pr_head_checkout(dest, repo_root)
        return None, f"checkout HEAD {head.strip() or 'unknown'} does not match {sha}"
    code, status, _ = run_cmd(["git", "status", "--porcelain"], check=False, cwd=dest)
    if code != 0 or status.strip():
        release_pr_head_checkout(dest, repo_root)
        return None, "materialized checkout is not clean"
    return dest, None


def release_pr_head_checkout(path, repo_root=None):
    """Removes a temporary acceptance checkout and its worktree registration."""
    if not path:
        return
    repo_root = repo_root or repository_root() or os.getcwd()
    run_cmd(["git", "worktree", "remove", "--force", path], check=False, cwd=repo_root)
    shutil.rmtree(path, ignore_errors=True)


def persist_acceptance_evidence(pr_id, pr, records):
    """Writes live verify: records into the PR's durable evidence block."""
    if not records:
        return True, "no acceptance records to persist"
    sha = pr.get("headRefOid")
    fresh = _gh_json(["gh", "pr", "view", str(pr_id), "--json", "body,headRefOid"])
    if not fresh:
        return False, "could not re-read the PR body to persist acceptance evidence"
    if fresh.get("headRefOid") != sha:
        return False, "PR head changed while acceptance commands ran"
    body = fresh.get("body") or ""
    existing, error = parse_verification_evidence(body)
    merged = acceptance_runner.merge_into_evidence(
        None if error == "missing" else existing,
        records,
    )
    merged["head_sha"] = sha
    if error == "missing":
        updated = body + render_verification_evidence(merged)
    else:
        if error:
            return False, f"verification evidence is malformed: {error}"
        updated = replace_verification_evidence(body, merged)
        if updated is None:
            return False, "could not replace the verification evidence block"
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_id), "--body", updated],
        check=False,
    )
    if code != 0:
        return False, f"could not persist acceptance evidence: {err.strip()}"
    return True, f"persisted {len(records)} acceptance record(s)"


def check_acceptance(issue_num, issue_body, cwd=None, execute=False, run_cmd_fn=None, records_out=None):
    if execute and not cwd:
        return False, (
            f"Issue #{issue_num}: no verified PR-head checkout; "
            "refusing to run verify: commands against an unknown tree"
        )
    ok, message, result = acceptance_runner.evaluate_issue(
        issue_body, cwd=cwd, execute=execute, run_cmd_fn=run_cmd_fn
    )
    if records_out is not None:
        records_out.extend(result["records"])
    if not ok:
        pending = [
            item.text
            for item in result["criteria"]
            if item.argv is None and not item.ticked
        ]
        if pending and "unticked" in message:
            preview = "\n      ".join(pending[:5])
            more = (
                f"\n      ... and {len(pending) - 5} more" if len(pending) > 5 else ""
            )
            return False, (
                f"Issue #{issue_num} has {len(pending)} unticked acceptance criteria:\n"
                f"      {preview}{more}"
            )
        return False, f"Issue #{issue_num}: {message}"
    ran = sum(1 for item in result["criteria"] if item.argv)
    if execute and ran:
        return True, (
            f"Acceptance criteria on #{issue_num} passed ({ran} verify: command(s))."
        )
    if ran:
        return True, (
            f"Acceptance criteria on #{issue_num} validated "
            f"({ran} verify: command(s); execution deferred to merge)."
        )
    return True, f"All acceptance criteria on #{issue_num} are ticked."


def check_size(pr):
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if total > SIZE_LIMIT:
        waiver = SIZE_WAIVER_REGION_RE.search(pr.get("body") or "")
        if not waiver:
            return False, (
                f"Diff is {total} lines, over the {SIZE_LIMIT}-line limit. "
                "Split the PR or add 'size-waiver: <rationale>' to its body."
            )
        return True, (
            f"Diff is {total} lines with explicit size waiver: "
            f"{waiver.group(1).strip()}"
        )
    return True, f"Diff is {total} lines."


def _is_rework_review(review):
    """Whether a review submission counts as a rework round (issue #98).

    Mirrors ``fleet_status._is_rework_review`` without importing that module
    (outside this issue's touches declaration).
    """
    state = (review.get("state") or "").upper()
    if state == "CHANGES_REQUESTED":
        return True
    if state != "COMMENTED":
        return False
    body = review.get("body") or ""
    return bool(_REWORK_BLOCKING_RE.search(body))


def count_review_rounds(pr):
    """Count rework review rounds visible on a PR snapshot."""
    return sum(1 for review in (pr.get("reviews") or []) if _is_rework_review(review))


def check_review_rounds(pr):
    """Soft visibility gate: always passes; never escalates to a human.

    Round count raises evidence and triggers automated split guidance via
    ``--emit-review-split``. It is audit data, not merge authority.
    """
    rounds = count_review_rounds(pr)
    if rounds >= REVIEW_ROUND_THRESHOLD:
        return True, (
            f"{rounds} review round(s) (threshold {REVIEW_ROUND_THRESHOLD}). "
            "Automated scope-reduction guidance applies — run "
            "`merge_pr.py --pr <n> --emit-review-split`. Round count alone "
            "never creates a human gate."
        )
    return True, f"{rounds} review round(s) (threshold {REVIEW_ROUND_THRESHOLD})."


def fetch_unresolved_finding_summaries(pr_id, limit=8):  # noqa: C901, PLR0912
    """Load short unresolved review-thread summaries for split guidance."""
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          reviewThreads(first:50, after:$cursor) {
            nodes {
              isResolved
              isOutdated
              path
              comments(first:1) { nodes { body } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen = set()
    summaries = []
    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list) or not isinstance(has_next, bool):
            return None
        for node in nodes:
            if bool(node.get("isResolved")) or bool(node.get("isOutdated")):
                continue
            comments = ((node.get("comments") or {}).get("nodes") or [])
            body = ""
            if comments and isinstance(comments[0], dict):
                body = (comments[0].get("body") or "").strip()
            path = node.get("path") or ""
            line = " ".join(body.split())
            if len(line) > 160:
                line = line[:157] + "..."
            if path and line:
                summaries.append(f"{path}: {line}")
            elif line:
                summaries.append(line)
            elif path:
                summaries.append(path)
            if len(summaries) >= limit:
                return summaries
        if not has_next:
            return summaries
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen:
            return None
        seen.add(next_cursor)
        cursor = next_cursor


def _split_item_marker(pr_num, idx):
    return REVIEW_ROUND_SPLIT_ITEM_FMT.format(pr=pr_num, idx=idx)


def build_review_round_split_plan(pr, findings=None, source_issue=None):
    """Build idempotent split guidance for a PR that crossed the round threshold.

    Returns a dict describing the comment body and proposed follow-up issues.
    Does not mutate GitHub. ``findings`` is an optional list of short strings.

    Follow-ups carry ``depends-on: #<Closes issue>`` (the original linked work
    item), not the PR number — Aru's picker and triage resolve depends-on as
    issue prerequisites.
    """
    rounds = count_review_rounds(pr)
    findings = list(findings or [])
    pr_num = pr.get("number")
    source = source_issue or (linked_issues(pr.get("body") or "")[:1] or [None])[0]
    follow_ups = []
    for idx, finding in enumerate(findings, start=1):
        title = f"Split from PR #{pr_num}: finding {idx}"
        if len(finding) < 80:
            title = f"Split from PR #{pr_num}: {finding}"
        if len(title) > 120:
            title = title[:117] + "..."
        body_lines = [
            "## User Story",
            "",
            "As the factory, I want a separable review finding tracked as its "
            "own issue so the original PR can shrink to the smallest coherent change.",
            "",
            "## Background",
            "",
            f"Automated split guidance from PR #{pr_num} after "
            f"{rounds} review round(s) (threshold {REVIEW_ROUND_THRESHOLD}).",
            "",
            f"Finding: {finding}",
            "",
            "## Acceptance Criteria",
            "",
            "- [ ] The finding is addressed or explicitly withdrawn with evidence.",
            "",
            "## Dependencies",
            "",
            f"depends-on: #{source}" if source else "depends-on:",
            "touches: `TBD`  # author must declare paths before Ready",
            "parallel-eligible: false",
            "",
            f"Provenance: automated review-round split from PR #{pr_num}",
            _split_item_marker(pr_num, idx),
        ]
        follow_ups.append({
            "idx": idx,
            "title": title,
            "body": "\n".join(body_lines),
        })
    if not follow_ups and source:
        idx = 1
        follow_ups.append({
            "idx": idx,
            "title": f"Split remainder from PR #{pr_num}",
            "body": "\n".join([
                "## User Story",
                "",
                "As the factory, I want remaining out-of-scope work from an "
                "over-reviewed PR tracked separately so the original PR can shrink.",
                "",
                "## Background",
                "",
                f"Automated split guidance from PR #{pr_num} after "
                f"{rounds} review round(s) (threshold {REVIEW_ROUND_THRESHOLD}). "
                "No unresolved thread summaries were available; the author should "
                "narrow the PR and move separable remainder here.",
                "",
                "## Acceptance Criteria",
                "",
                "- [ ] Remainder scope is defined with acceptance criteria and touches.",
                "",
                "## Dependencies",
                "",
                f"depends-on: #{source}",
                "touches: `TBD`",
                "parallel-eligible: false",
                "",
                f"Provenance: automated review-round split from PR #{pr_num}",
                _split_item_marker(pr_num, idx),
            ]),
        })
    finding_block = "\n".join(f"- {item}" for item in findings) if findings else (
        "- (no unresolved thread summaries available; author should still shrink scope)"
    )
    source_line = (
        f"(each carries `depends-on: #{source}` to the original linked issue)."
        if source
        else "."
    )
    comment = "\n".join([
        REVIEW_ROUND_SPLIT_MARKER,
        f"## Automated review-round split guidance ({rounds} rounds)",
        "",
        f"This PR crossed the review-round threshold of {REVIEW_ROUND_THRESHOLD}.",
        "Round count is audit data — it does **not** create a human approval gate "
        "and does **not** change merge authority (`merge_pr.py` remains the only merge path).",
        "",
        "### Required author action",
        "1. Shrink this PR to the smallest coherent change that can pass review.",
        f"2. Leave separable findings on the follow-up issues listed below {source_line}",
        "3. Continue the agent review loop; do not escalate to a human for round count.",
        "",
        "### Unresolved findings considered",
        finding_block,
        "",
        "### Proposed follow-up issues",
    ])
    for item in follow_ups:
        comment += f"\n- {item['title']}"
    return {
        "rounds": rounds,
        "threshold": REVIEW_ROUND_THRESHOLD,
        "crossed": rounds >= REVIEW_ROUND_THRESHOLD,
        "source_issue": source,
        "findings": findings,
        "follow_ups": follow_ups,
        "comment": comment,
        "marker": REVIEW_ROUND_SPLIT_MARKER,
    }


def _flatten_comment_pages(data):
    """Flatten ``gh api --paginate --slurp`` (or a single-page list) to bodies."""
    if not isinstance(data, list):
        return []
    if data and isinstance(data[0], dict):
        return [c.get("body") or "" for c in data if isinstance(c, dict)]
    bodies = []
    for page in data:
        if isinstance(page, list):
            bodies.extend(c.get("body") or "" for c in page if isinstance(c, dict))
    return bodies


def _pr_comments_bodies(pr_num):
    slug = get_repo_slug()
    if not slug:
        return None
    # --slurp yields one JSON array of pages; without it, --paginate concatenates
    # arrays and a single json.loads fails after the first page.
    data = _gh_json([
        "gh", "api", f"repos/{slug}/issues/{pr_num}/comments",
        "--paginate", "--slurp",
    ])
    if data is None:
        return None
    return _flatten_comment_pages(data)


def _parse_issue_number_from_create(out):
    match = re.search(r"/issues/(\d+)", out or "")
    return int(match.group(1)) if match else None


def _issue_url(slug, number):
    return f"https://github.com/{slug}/issues/{number}"


def _find_existing_split_follow_ups(pr_num):
    """Map follow-up idx -> {number, url} for issues already filed for this PR.

    Returns None on query failure (fail closed — do not create duplicates).
    """
    data = _gh_json([
        "gh", "issue", "list",
        "--state", "all",
        "--limit", "100",
        "--search", f"aru-review-round-split-item:pr={pr_num} in:body",
        "--json", "number,body,url",
    ])
    if data is None:
        return None
    if not isinstance(data, list):
        return {}
    found = {}
    for issue in data:
        if not isinstance(issue, dict):
            continue
        body = issue.get("body") or ""
        match = REVIEW_ROUND_SPLIT_ITEM_RE.search(body)
        if not match:
            continue
        if int(match.group(1)) != int(pr_num):
            continue
        idx = int(match.group(2))
        number = issue.get("number")
        if not number:
            continue
        url = issue.get("url") or ""
        if not url:
            slug = get_repo_slug()
            url = _issue_url(slug, number) if slug else f"#{number}"
        # Prefer the lowest issue number if duplicates somehow exist.
        prior = found.get(idx)
        if prior is None or number < prior["number"]:
            found[idx] = {"number": number, "url": url}
    return found


def _attach_follow_up_to_board(issue_num):
    """Place a split follow-up on the governed board as Backlog."""
    return update_status(issue_num, "Backlog", require_board=True)


def emit_review_round_split(pr, findings=None, *, apply=True):  # noqa: C901, PLR0912
    """Post split guidance and file follow-up issues when the threshold is crossed.

    Idempotent: if ``REVIEW_ROUND_SPLIT_MARKER`` is already present on the PR,
    returns without creating duplicate issues. Retries after partial issue
    creation reuse provenance-tagged issues. When ``apply`` is False, returns
    the plan only.
    """
    if findings is None and apply:
        fetched = fetch_unresolved_finding_summaries(pr.get("number"))
        findings = fetched if fetched is not None else []
    plan = build_review_round_split_plan(pr, findings=findings)
    if not plan["crossed"]:
        return {
            "emitted": False,
            "reason": "below threshold",
            "plan": plan,
            "created_issues": [],
        }
    if not apply:
        return {
            "emitted": False,
            "reason": "dry plan",
            "plan": plan,
            "created_issues": [],
        }
    pr_num = pr.get("number")
    bodies = _pr_comments_bodies(pr_num)
    if bodies is None:
        return {
            "emitted": False,
            "reason": "could not read PR comments",
            "plan": plan,
            "created_issues": [],
        }
    if any(REVIEW_ROUND_SPLIT_MARKER in body for body in bodies):
        return {
            "emitted": False,
            "reason": "already emitted",
            "plan": plan,
            "created_issues": [],
        }
    existing = _find_existing_split_follow_ups(pr_num)
    if existing is None:
        return {
            "emitted": False,
            "reason": "could not query existing split follow-ups",
            "plan": plan,
            "created_issues": [],
        }
    slug = get_repo_slug()
    filed = []
    for item in plan["follow_ups"]:
        idx = item["idx"]
        prior = existing.get(idx)
        if prior:
            if not _attach_follow_up_to_board(prior["number"]):
                return {
                    "emitted": False,
                    "reason": (
                        f"board attach failed for existing #{prior['number']}"
                    ),
                    "plan": plan,
                    "created_issues": filed,
                }
            filed.append(prior["url"])
            continue
        code, out, err = run_cmd(
            [
                "gh", "issue", "create",
                "--title", item["title"],
                "--body", item["body"],
                "--label", "status:backlog",
            ],
            check=False,
        )
        if code != 0:
            return {
                "emitted": False,
                "reason": f"issue create failed: {err.strip()}",
                "plan": plan,
                "created_issues": filed,
            }
        issue_num = _parse_issue_number_from_create(out)
        if issue_num is None:
            return {
                "emitted": False,
                "reason": f"issue create returned unparseable ref: {out.strip()}",
                "plan": plan,
                "created_issues": filed,
            }
        if not _attach_follow_up_to_board(issue_num):
            return {
                "emitted": False,
                "reason": f"board attach failed for #{issue_num}",
                "plan": plan,
                "created_issues": filed,
            }
        url = out.strip() or (_issue_url(slug, issue_num) if slug else f"#{issue_num}")
        filed.append(url)
        existing[idx] = {"number": issue_num, "url": url}
    comment = plan["comment"]
    if filed:
        comment += "\n\n### Filed\n" + "\n".join(f"- {url}" for url in filed)
    code, _, err = run_cmd(
        ["gh", "pr", "comment", str(pr_num), "--body", comment],
        check=False,
    )
    if code != 0:
        return {
            "emitted": False,
            "reason": f"comment failed: {err.strip()}",
            "plan": plan,
            "created_issues": filed,
        }
    return {
        "emitted": True,
        "reason": "posted",
        "plan": plan,
        "created_issues": filed,
    }


def check_test_coverage(pr):
    """Require a changed test whenever production Python roots are changed."""
    files = pr.get("files") or []
    changed_files = pr.get("changedFiles")
    if isinstance(changed_files, int) and changed_files > len(files):
        return False, (
            f"Changed-file data is truncated ({len(files)} of {changed_files}); "
            "split the PR so test coverage can be evaluated completely."
        )
    paths = [entry.get("path", "") for entry in files if isinstance(entry, dict)]
    production = [path for path in paths if path.startswith(("src/", "scripts/"))]
    if not production:
        return True, "No src/ or scripts/ changes require test coverage."

    tests = [
        entry.get("path", "")
        for entry in files
        if isinstance(entry, dict)
        and entry.get("path", "").startswith("tests/")
        and not ((entry.get("additions") or 0) == 0 and (entry.get("deletions") or 0) > 0)
    ]
    if not tests:
        return False, (
            "Production changes under src/ or scripts/ require a changed, non-deleted "
            "test file under tests/."
        )
    return True, f"Production changes include test coverage in {len(tests)} test file(s)."


def check_spec_sync(pr, repo_dir=None):
    """Require specifications and CLI arguments to remain synchronized with implementation on PR head."""
    merge_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(merge_dir, "sync_spec.py"),
        os.path.join(os.environ.get("ARU_SDLC_HOME", ""), "scripts", "sync_spec.py"),
        os.path.join(repo_dir or ".", "scripts", "sync_spec.py"),
    ]
    sync_script = next((c for c in candidates if c and os.path.exists(c)), None)
    if not sync_script:
        return False, "Required sync_spec.py engine is missing; cannot audit specification synchronization."

    head_sha = (pr or {}).get("headRefOid") if isinstance(pr, dict) else None
    cmd = [sys.executable, sync_script, "--check"]
    if repo_dir:
        cmd.extend(["--repo-dir", repo_dir])
    if head_sha:
        check_proc = subprocess.run(
            ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
            cwd=repo_dir or ".",
            capture_output=True,
            check=False,
        )
        if check_proc.returncode == 0:
            cmd.extend(["--head", head_sha])

    code, out, err = run_cmd(cmd, check=False)
    if code == 0:
        return True, "Specifications and code are synchronized."
    return False, f"Specification drift detected: {out or err}"


def is_merged(pr):
    return (pr.get("state") or "").upper() == "MERGED" or bool(pr.get("mergedAt"))


def closeout_incomplete(pr):
    """True when a merged PR still needs ``merge_pr.py`` close-out resumed.

    Server-side merge removes the PR from ``gh pr list --state open``. Without
    this check, a crash after merge but before Done/claim cleanup leaves the
    board permanently stranded. Signals: a lingering ``merger:`` claim, any
    linked ``Closes #N`` issue that is still open, or a closed issue that never
    received ``status:done``.
    """
    if not is_merged(pr):
        return False
    labels = [lab.get("name", "") for lab in (pr.get("labels") or [])]
    if any(name.startswith(MERGER_CLAIM_LABEL) for name in labels):
        return True
    for num in linked_issues(pr.get("body")):
        issue = _gh_json(
            ["gh", "issue", "view", str(num), "--json", "state,labels"]
        )
        if issue is None:
            # Fail closed: an unreadable linked issue must be treated as unfinished.
            return True
        if (issue.get("state") or "").upper() == "OPEN":
            return True
        issue_labels = {
            lab.get("name", "") for lab in (issue.get("labels") or [])
        }
        if "status:done" not in issue_labels:
            return True
    return False


def merge_commit_oid(pr):
    value = pr.get("mergeCommit")
    if isinstance(value, dict):
        return value.get("oid") or ""
    return value or ""


def head_repository_slug(pr):
    repository = pr.get("headRepository") or {}
    owner = pr.get("headRepositoryOwner") or {}
    return repository.get("nameWithOwner") or (
        f"{owner.get('login')}/{repository.get('name')}"
        if owner.get("login") and repository.get("name") else ""
    )


def repository_root():
    """Returns the primary worktree root even when invoked from a linked one."""
    code, common_dir, _ = run_cmd(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False,
    )
    if code != 0 or not common_dir:
        return None
    common_dir = os.path.abspath(common_dir.strip())
    return os.path.dirname(common_dir) if os.path.basename(common_dir) == ".git" else None


def _acquire_repository_merge_lock():
    """Acquire this host's cross-clone merge lock for the current repository."""
    slug = get_repo_slug()
    if not slug:
        return None, "could not resolve repository identity for merge serialization"
    state_root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ).expanduser().resolve()
    directory = state_root / "aru-factory" / "merge-locks"
    descriptor = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        identity = hashlib.sha256(slug.lower().encode("utf-8")).hexdigest()[:24]
        path = directory / f"{identity}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        return None, f"could not open repository merge lock: {exc}"
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, "another merge is executing for this repository"
    except OSError as exc:
        handle.close()
        return None, f"could not acquire repository merge lock: {exc}"
    return handle, "repository merge execution serialized"


@contextmanager
def repository_merge_lock():
    """Hold the machine-global repository merge lock for one execution window."""
    handle, message = _acquire_repository_merge_lock()
    try:
        yield handle is not None, message
    finally:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def execute_merge(pr_id, pr, merge_method):
    """Runs only the server-side merge, then re-reads authoritative PR state.

    The merge command deliberately does not delete either branch. Cleanup is a
    separate, resumable phase. A non-zero command may still mean GitHub merged
    successfully, so the return code is never interpreted without a re-read.
    """
    env = dict(os.environ, ARU_ALLOW_MAIN_PUSH="1")
    merge_cmd = ["gh", "pr", "merge", str(pr_id), f"--{merge_method}"]
    head_sha = pr.get("headRefOid")
    if head_sha:
        merge_cmd += ["--match-head-commit", head_sha]
    else:
        print("[WARN] No head SHA available; merging without pinning it.", file=sys.stderr)

    proc = subprocess.run(merge_cmd, capture_output=True, text=True, env=env, check=False)
    fresh = fetch_pr(pr_id)
    if not fresh:
        return None, "Could not re-read the PR after the merge command."
    if not is_merged(fresh):
        detail = (proc.stderr or proc.stdout or "merge command returned no detail").strip()
        return None, f"GitHub still reports {fresh.get('state', '?')}; {detail}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no command detail").strip()
        return fresh, (
            f"GitHub reports merged even though the merge command exited "
            f"{proc.returncode}: {detail}"
        )
    return fresh, "GitHub accepted the merge."


def find_branch_worktree(porcelain, branch):
    """Returns the exact branch's worktree path and HEAD from porcelain data."""
    expected_ref = f"refs/heads/{branch}"
    for block in porcelain.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
        if fields.get("branch") == expected_ref:
            return fields.get("worktree"), fields.get("HEAD")
    return None, None


def prune_worktree(repo_root, branch, expected_sha):  # noqa: C901, PLR0912, PLR0915
    """Deregisters the exact worktree after atomically retaining its directory."""
    if not branch or not expected_sha:
        return False, "Branch and gated head SHA are required; no worktree removed."
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root)
    if code != 0:
        return False, "Could not list worktrees."
    path, actual_sha = find_branch_worktree(out, branch)
    if not path:
        return True, "Worktree already absent."
    if actual_sha != expected_sha:
        return False, (
            f"Worktree {path} now points to {actual_sha or 'unknown'}, not gated head "
            f"{expected_sha}; left untouched."
        )
    if os.path.abspath(path) == os.path.abspath(repo_root):
        return False, "Refusing to remove the primary worktree."
    retained_root = os.path.join(repo_root, ".worktrees", ".retained")
    retained_path = os.path.join(
        retained_root, f"{expected_sha[:12]}-{os.path.basename(path)}"
    )
    if not os.path.exists(path):
        if not os.path.isdir(retained_path):
            return False, f"Worktree path {path} disappeared; no retained copy found."
        code, _, err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if code == 0:
            return True, f"Worktree already retained at {retained_path}; registration pruned."
        return False, f"Worktree retained at {retained_path}; deregistration failed: {err.strip()}"
    marker = os.path.join(path, ".git")
    try:
        with open(marker, encoding="utf-8") as marker_file:
            marker_text = marker_file.read().strip()
    except OSError as exc:
        return False, f"Could not read worktree metadata {marker}: {exc}"
    if not marker_text.startswith("gitdir: "):
        return False, f"Unexpected worktree metadata in {marker}; left untouched."
    admin_dir = os.path.realpath(marker_text.split(": ", 1)[1])
    allowed_admin_root = os.path.realpath(
        os.path.join(repo_root, ".git", "worktrees")
    )
    try:
        inside_admin_root = os.path.commonpath(
            [admin_dir, allowed_admin_root]
        ) == allowed_admin_root
    except ValueError:
        inside_admin_root = False
    if not inside_admin_root:
        return False, f"Worktree metadata points outside {allowed_admin_root}; left untouched."

    head_lock = os.path.join(admin_dir, "HEAD.lock")
    try:
        lock_fd = os.open(head_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return False, f"Could not lock worktree HEAD for exact ownership check: {exc}"
    ref_lock_fd = None
    ref_lock = ""
    try:
        ref_lock_root = os.path.realpath(
            os.path.join(repo_root, ".git", "refs", "heads")
        )
        ref_lock = os.path.realpath(
            os.path.join(ref_lock_root, f"{branch}.lock")
        )
        try:
            inside_ref_root = os.path.commonpath(
                [ref_lock, ref_lock_root]
            ) == ref_lock_root
        except ValueError:
            inside_ref_root = False
        if not inside_ref_root:
            return False, f"Branch lock points outside {ref_lock_root}; left untouched."
        try:
            os.makedirs(os.path.dirname(ref_lock), exist_ok=True)
            ref_lock_fd = os.open(
                ref_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except OSError as exc:
            return False, f"Could not lock branch ref for exact ownership check: {exc}"

        ref_code, current_ref, ref_err = run_cmd(
            ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
            check=False,
            cwd=path,
        )
        sha_code, current_sha, sha_err = run_cmd(
            ["git", "rev-parse", "HEAD"], check=False, cwd=path
        )
        if ref_code != 0 or sha_code != 0:
            detail = ref_err.strip() or sha_err.strip()
            return False, f"Could not revalidate locked worktree ownership: {detail}"
        if current_ref.strip() != f"refs/heads/{branch}" or current_sha.strip() != expected_sha:
            return False, (
                f"Worktree ownership changed to {current_ref.strip()} at "
                f"{current_sha.strip()}; left untouched."
            )
        status_code, status, status_err = run_cmd(
            [
                "git", "status", "--porcelain", "--untracked-files=all",
                "--ignored=matching",
            ],
            check=False,
            cwd=path,
        )
        if status_code != 0:
            return False, f"Could not inspect worktree {path}: {status_err.strip()}"
        from cleanup_worktrees import (
            porcelain_blocks_prune,
            porcelain_dirty_except_manifest,
            remove_retain_manifest,
            write_retain_manifest,
        )
        if porcelain_blocks_prune(status, path):
            return False, (
                f"Worktree {path} has tracked or untracked files; left untouched."
            )
        if os.path.lexists(retained_path):
            return False, f"Retention destination already exists: {retained_path}"
        try:
            write_retain_manifest(path)
        except OSError as exc:
            return False, f"Could not snapshot worktree {path} for retention: {exc}"
        status_code, status, status_err = run_cmd(
            [
                "git", "status", "--porcelain", "--untracked-files=all",
                "--ignored=matching",
            ],
            check=False,
            cwd=path,
        )
        if status_code != 0:
            remove_retain_manifest(path)
            return False, f"Could not inspect worktree {path}: {status_err.strip()}"
        blocked = porcelain_dirty_except_manifest(status, path)
        if blocked is not False:
            remove_retain_manifest(path)
            if blocked is None:
                return False, f"Could not inspect worktree {path} after snapshot."
            return False, (
                f"Worktree {path} has tracked or untracked files; left untouched."
            )
        try:
            os.makedirs(retained_root, exist_ok=True)
            os.rename(path, retained_path)
        except OSError as exc:
            remove_retain_manifest(path)
            return False, f"Could not atomically retain worktree {path}: {exc}"
        code, _, err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if code == 0:
            return True, f"Retained worktree at {retained_path}; registration pruned."
        return False, (
            f"Worktree retained at {retained_path}; deregistration failed: {err.strip()}"
        )
    finally:
        if ref_lock_fd is not None:
            os.close(ref_lock_fd)
            if os.path.exists(ref_lock):
                os.unlink(ref_lock)
        os.close(lock_fd)
        if os.path.exists(head_lock):
            os.unlink(head_lock)


def is_valid_branch_name(branch: str) -> bool:
    """Validates that branch is a safe, non-empty Git branch name."""
    if not branch or branch.startswith("-") or branch.endswith((".lock", "/")):
        return False
    if any(c in branch for c in " ~^:?*[\t\r\n\\") or ".." in branch or "@{" in branch or "//" in branch:
        return False
    return True


def cleanup_local_branch(repo_root, branch, expected_sha):
    """Deletes the local branch using an atomic compare-and-delete leased to expected_sha.

    A compare-and-delete protects the ref OID against reuse races, but it cannot
    atomically stop another process from attaching a new worktree to that ref. Any
    worktree attachment observed here leaves the branch untouched and returns a
    failure so close-out remains incomplete and the merger claim remains discoverable.
    When unattached, deletion uses ``git update-ref -d refs/heads/<branch> <expected_sha>``
    to guarantee the ref is only deleted if it still equals the exact gated head SHA.
    If the lease fails because the ref moved or was recreated, the ref is preserved
    and a close-out failure is returned.
    """
    if not branch or not expected_sha:
        return False, "Branch and gated head SHA are required; local branch state is unknown."
    if not is_valid_branch_name(branch):
        return False, f"Invalid local branch ref name: {branch!r}."
    ref = f"refs/heads/{branch}"
    code, actual_sha, _ = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return True, "Local branch already absent."
    if actual_sha.strip() != expected_sha:
        return False, (
            f"Local branch {branch} was reused at {actual_sha.strip() or 'unknown'}; "
            "lease mismatch -- unrelated ref retained."
        )
    from cleanup_worktrees import attached_branches
    if branch in attached_branches(repo_root):
        return False, (
            f"Retained local branch {branch}; branch is attached to a worktree."
        )
    code, _, err = run_cmd(
        ["git", "update-ref", "-d", ref, expected_sha],
        check=False, cwd=repo_root,
    )
    if code == 0:
        return True, f"Deleted local branch {branch}; worktree registration was already gone."
    # If update-ref failed, check if another process deleted it concurrently
    # or if the ref moved / was recreated with a different SHA.
    check_code, current_sha, _ = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        check=False, cwd=repo_root,
    )
    if check_code != 0:
        return True, "Local branch already absent."
    if current_sha.strip() != expected_sha:
        return False, (
            f"Local branch {branch} lease failed on {expected_sha} "
            f"(now at {current_sha.strip() or 'unknown'}): {err.strip()}; ref retained."
        )
    return False, f"Could not delete local branch {branch}: {err.strip()}"


def delete_remote_branch(repo_root, branch, expected_sha, head_repo_slug):
    if not branch or not expected_sha or not head_repo_slug:
        return False, (
            "Branch, gated head SHA, and head repository are required; "
            "no remote branch removed."
        )
    base_repo_slug = get_repo_slug()
    if not base_repo_slug:
        return False, "Could not identify the base repository; no remote branch removed."
    remote = (
        "origin"
        if head_repo_slug == base_repo_slug
        else f"https://github.com/{head_repo_slug}.git"
    )
    ref = f"refs/heads/{branch}"
    code, out, err = run_cmd(
        ["git", "ls-remote", "--heads", remote, ref], check=False, cwd=repo_root
    )
    if code != 0:
        return False, f"Could not inspect {head_repo_slug} branch {branch}: {err.strip()}"
    if not out:
        return True, "Remote branch already absent."
    actual_sha = out.split()[0] if out.split() else ""
    if actual_sha != expected_sha:
        return False, (
            f"Remote branch {head_repo_slug}:{branch} now points to "
            f"{actual_sha or 'unknown'}, not gated head {expected_sha}; left untouched."
        )
    code, _, err = run_cmd(
        [
            "git", "push", f"--force-with-lease={ref}:{expected_sha}",
            remote, f":{ref}",
        ],
        check=False,
        cwd=repo_root,
    )
    if code == 0:
        return True, f"Deleted remote branch {head_repo_slug}:{branch}."
    return False, (
        f"Could not atomically delete remote branch {head_repo_slug}:{branch}; "
        f"it may have changed: {err.strip()}"
    )


def ensure_issue_closed(issue_num):
    issue = _gh_json(["gh", "issue", "view", str(issue_num), "--json", "state"])
    if issue is None:
        return False, f"Could not read issue #{issue_num}."
    if (issue.get("state") or "").upper() == "CLOSED":
        return True, f"Issue #{issue_num} already closed."
    code, _, err = run_cmd(
        ["gh", "issue", "close", str(issue_num), "--reason", "completed"], check=False
    )
    if code == 0:
        return True, f"Closed issue #{issue_num}."
    return False, f"Could not close issue #{issue_num}: {err.strip()}"


def reconcile_issue_done(issue_num):
    if update_status(issue_num, "Done", require_board=True):
        return True, f"Issue #{issue_num} board and status label reconciled to Done."
    return False, f"Could not reconcile issue #{issue_num} to Done."


def clear_labels(kind, number, prefix, cwd=None):
    data = _gh_json(["gh", kind, "view", str(number), "--json", "labels"], cwd=cwd)
    if data is None:
        return False, f"Could not read {kind} #{number} labels."
    names = [
        label.get("name", "") for label in (data.get("labels") or [])
        if label.get("name", "").startswith(prefix)
    ]
    for name in names:
        code, _, err = run_cmd(
            ["gh", kind, "edit", str(number), "--remove-label", name],
            check=False, cwd=cwd,
        )
        if code != 0:
            return False, f"Could not remove {name} from {kind} #{number}: {err.strip()}"
    noun = "claims" if names else "claim"
    return True, f"{kind.title()} #{number} {prefix}{noun} cleared or already absent."


def clear_issue_claims(issue_num, cwd=None):
    return clear_labels("issue", issue_num, "agent:", cwd=cwd)


def clear_review_claims(pr_num, cwd=None):
    return clear_labels("pr", pr_num, REVIEW_CLAIM_LABEL, cwd=cwd)


def clear_merger_claims(pr_num, cwd=None):
    return clear_labels("pr", pr_num, MERGER_CLAIM_LABEL, cwd=cwd)


def evaluate_dod(pr, issue_bodies, evidence, behind_resolver=None):
    """Runs every Definition-of-Done check without merging.

    Returns ``(ok, gates)`` where ``gates`` is a list of
    ``(name, passed, message)`` in evaluation order. Shared by ``--dry-run``
    and the merge work picker so eligibility cannot drift from the gate.

    ``behind_resolver`` is threaded to :func:`check_rebased` so a caller with no
    repository to interrogate -- a hermetic fleet simulation -- can state
    ancestry directly. Production callers omit it and get the fail-closed git
    path, which is the point of the gate.
    """
    issue_nums = linked_issues(pr.get("body"))
    gates = [
        ("open", *check_open(pr)),
        ("issue link", *check_issue_link(pr)),
        ("verification", *check_verification(pr)),
        ("ci", *check_ci(pr)),
        ("review", *check_reviews(pr, evidence)),
        ("rebased", *check_rebased(pr, behind_resolver)),
        ("size", *check_size(pr)),
        ("tests", *check_test_coverage(pr)),
        ("spec-sync", *check_spec_sync(pr)),
        ("review rounds", *check_review_rounds(pr)),
    ]
    for num in issue_nums:
        gates.append(
            (f"accept #{num}", *check_acceptance(num, issue_bodies.get(num, ""), execute=False))
        )
    ok = all(passed for _, passed, _ in gates)
    return ok, gates


def dry_run_json_payload(pr, gates, ok):
    """Serialize a dry-run DoD evaluation for ``--dry-run --json`` callers."""
    first_blocking = next((name for name, passed, _ in gates if not passed), None)
    return {
        "pr": pr.get("number"),
        "title": pr.get("title") or "",
        "ok": bool(ok),
        "gates": [
            {"name": name, "passed": bool(passed), "message": message}
            for name, passed, message in gates
        ],
        "first_blocking": first_blocking,
    }


def dod_status(pr_id):
    """Fetch-and-evaluate helper for callers that only need pass/fail + reason.

    Returns ``(ok, reason)``. ``ok`` is True only when every gate passes.
    Fetch or thread-query failures fail closed with ``ok=False``.
    """
    pr = fetch_pr(pr_id)
    if not pr:
        return False, "could not fetch pull request"
    if is_merged(pr):
        if closeout_incomplete(pr):
            return True, "merged; close-out incomplete — resume merge_pr.py"
        return False, "merged and close-out already complete"
    issue_nums = linked_issues(pr.get("body"))
    if not issue_nums:
        return False, "PR body has no Closes #<issue>"
    issue_bodies = {}
    for num in issue_nums:
        issue = _gh_json(["gh", "issue", "view", str(num), "--json", "body"])
        if issue is None:
            return False, f"could not read issue #{num}"
        issue_bodies[num] = issue.get("body") or ""
    evidence = review_evidence(pr_id)
    evidence_head = evidence.get("head_oid") if evidence else None
    snapshot_head = pr.get("headRefOid")
    if not heads_match(snapshot_head, evidence_head):
        return False, (
            f"review evidence covers {evidence_head or 'unknown'}, but the PR "
            f"snapshot is {snapshot_head or 'unknown'}"
        )
    ok, gates = evaluate_dod(pr, issue_bodies, evidence)
    if ok:
        return True, "every Definition-of-Done gate passed"
    blocked = [name for name, passed, _ in gates if not passed]
    return False, f"unmet: {', '.join(blocked)}"


def run_closeout(pr, issue_nums, repo_root, failures=None):  # noqa: C901, PLR0912
    """Runs every idempotent close-out step, even after an earlier failure."""
    try:
        os.chdir(repo_root)
    except OSError as exc:
        print(f"\n=== Post-merge close-out ===\n  ❌ working directory  {exc}")
        if failures is not None:
            failures.append(f"working directory: {exc}")
        return False
    branch = pr.get("headRefName") or ""
    expected_sha = pr.get("headRefOid") or ""
    head_repo_slug = head_repository_slug(pr)
    steps = [
        ("worktree", lambda: prune_worktree(repo_root, branch, expected_sha)),
        ("local branch", lambda: cleanup_local_branch(repo_root, branch, expected_sha)),
        ("remote branch", lambda: delete_remote_branch(
            repo_root, branch, expected_sha, head_repo_slug
        )),
    ]
    for num in issue_nums:
        steps.extend([
            (f"close #{num}", lambda num=num: ensure_issue_closed(num)),
            (f"done #{num}", lambda num=num: reconcile_issue_done(num)),
            (f"issue claim #{num}", lambda num=num: clear_issue_claims(num)),
        ])
    steps.append(("review claim", lambda: clear_review_claims(pr.get("number"))))

    all_ok = True
    print("\n=== Post-merge close-out ===")
    for name, action in steps:
        try:
            ok, message = action()
        except Exception as exc:  # Keep later recovery steps running.
            ok, message = False, f"Unexpected close-out error: {exc}"
        print(f"  {'✅' if ok else '❌'} {name:<18} {message}")
        if not ok and failures is not None:
            failures.append(f"{name}: {message}")
        all_ok = all_ok and ok

    # Keep merger:<id> until the current PR's leftovers are observably gone so
    # the picker can rediscover incomplete close-out. Clearing it before the
    # janitor runs made a leftover local branch invisible to recovery.
    try:
        retain = None if all_ok else pr.get("number")
        ok, message = sweep_leftovers(repo_root, retain_merger_pr=retain)
    except Exception as exc:
        ok, message = False, f"janitor skipped: {exc}"
    print(f"  {'✅' if ok else '❌'} {'janitor':<18} {message}")
    if not ok and failures is not None:
        failures.append(f"janitor: {message}")
    all_ok = all_ok and ok
    if all_ok:
        try:
            ok, message = clear_merger_claims(pr.get("number"))
        except Exception as exc:
            ok, message = False, f"Unexpected close-out error: {exc}"
        print(f"  {'✅' if ok else '❌'} {'merger claim':<18} {message}")
        if not ok and failures is not None:
            failures.append(f"merger claim: {message}")
        all_ok = all_ok and ok
    else:
        print("  ⏳ merger claim      retained so recovery remains discoverable")
    return all_ok


def sweep_leftovers(repo_root, retain_merger_pr=None):
    """Leftover sweep. Operational failures are returned to close-out."""
    from cleanup_worktrees import sweep
    return sweep(repo_root, retain_merger_pr=retain_merger_pr)


def run_closeout_with_retries(pr, issue_nums, repo_root, sleep_fn=None):
    """Retry idempotent close-out before declaring operator intervention.

    A harmless orphan local branch -- worktree registration already gone,
    remote branch/issue/board lifecycle independently verified complete --
    must never be the sole reason a completed merge turns into a stuck
    ``merger:`` claim or a human-intervention report. Once the bounded
    retries below are exhausted, a last attempt whose only recorded failure
    is the "local branch" step releases the claim directly and reports a
    non-blocking warning instead. Active worktree attachment or a lease
    mismatch (reused/moved ref) are never waived.
    """
    sleep_fn = sleep_fn or time.sleep
    failed_attempts = []
    last_failures = []
    for attempt in range(len(CLOSEOUT_RETRY_DELAYS) + 1):
        if attempt:
            delay = CLOSEOUT_RETRY_DELAYS[attempt - 1]
            print(f"\n⏳ Close-out retry {attempt}/{len(CLOSEOUT_RETRY_DELAYS)} in {delay}s...")
            sleep_fn(delay)
        failures = []
        if run_closeout(pr, issue_nums, repo_root, failures=failures):
            return True, failed_attempts
        last_failures = failures or ["close-out returned failure without step evidence"]
        failed_attempts.append(last_failures)
    if last_failures and all(item.startswith("local branch:") for item in last_failures):
        if not any(
            "attached" in item or "lease" in item or "reused" in item or "invalid" in item
            for item in last_failures
        ):
            try:
                ok, message = clear_merger_claims(pr.get("number"))
            except Exception as exc:
                ok, message = False, f"Unexpected claim clearance error: {exc}"
            print(f"  {'✅' if ok else '❌'} {'merger claim':<18} {message}")
            print(
                f"  ⚠️  {'local cleanup':<18} {last_failures[0]}; "
                "non-blocking -- remote lifecycle already verified complete"
            )
            if ok:
                return True, failed_attempts
    return False, failed_attempts


def intervention_command():
    """The exact secret-free merge command the operator can safely resume."""
    return shlex.join([sys.executable, os.path.realpath(__file__), *sys.argv[1:]])


def surviving_worktree(repo_root, branch):
    """Describe the branch worktree without guessing when inspection fails."""
    if not repo_root:
        return "unavailable (repository root could not be resolved)"
    code, out, err = run_cmd(
        ["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root
    )
    if code != 0:
        return f"unavailable ({err.strip() or 'git worktree list failed'})"
    path, head = find_branch_worktree(out, branch)
    if not path:
        return "absent (already pruned or not registered)"
    return f"{path} at {head or 'unknown'}"


def human_intervention_body(
    pr, repo_root, gated_head, merged_sha, failed_attempts, command,
    blocked_before_closeout=None,
):
    """Build the durable evidence required when close-out cannot self-heal."""
    live_pr = fetch_pr(pr.get("number")) or pr
    claims = label_values(live_pr, MERGER_CLAIM_LABEL)
    attempt_lines = []
    for index, failures in enumerate(failed_attempts, start=1):
        attempt_lines.append(f"- attempt {index}: {'; '.join(failures)}")
    if blocked_before_closeout:
        attempt_lines.append(f"- close-out not attempted: {blocked_before_closeout}")
    elif not attempt_lines:
        attempt_lines.append("- no close-out attempt evidence was available")
    branch = pr.get("headRefName") or "unknown"
    if failed_attempts:
        remediation = (
            f"{len(failed_attempts)} close-out attempt(s); bounded retry delays "
            f"were {', '.join(map(str, CLOSEOUT_RETRY_DELAYS))} seconds"
        )
    else:
        remediation = "0 close-out attempts; bounded retries were not run"
    return "\n".join([
        "## Human intervention required",
        "",
        "GitHub has accepted the merge, but automated close-out could not "
        "finish safely. This is not a successful factory completion.",
        "",
        "### Failed command",
        "",
        f"- command: `{command}`",
        f"- exit code: `{EXIT_ERROR}`",
        f"- attempted remediation: {remediation}",
        "",
        "### Preserved artifacts",
        "",
        f"- gated head SHA: `{gated_head}`",
        f"- merged SHA: `{merged_sha}`",
        f"- branch: `{branch}`",
        f"- worktree: {surviving_worktree(repo_root, branch)}",
        f"- remaining merger claims: "
        f"{', '.join(f'`{MERGER_CLAIM_LABEL}{value}`' for value in claims) or 'none visible'}",
        "",
        "### Attempt evidence",
        "",
        *attempt_lines,
        "",
        "### Operator action",
        "",
        f"- Correct the final close-out failure above, then run `{command}`.",
        "",
    ])


def post_human_intervention(
    pr, issue_nums, repo_root, gated_head, merged_sha, failed_attempts, command,
    blocked_before_closeout=None,
):
    """Post the same authoritative intervention evidence to PR and issues."""
    body = human_intervention_body(
        pr, repo_root, gated_head, merged_sha, failed_attempts, command,
        blocked_before_closeout=blocked_before_closeout,
    )
    targets = [("pr", pr.get("number"))]
    targets.extend(("issue", number) for number in issue_nums)
    all_ok = True
    for kind, number in targets:
        code, _, err = run_cmd(
            ["gh", kind, "comment", str(number), "--body", body], check=False
        )
        if code == 0:
            print(f"  ✅ intervention       recorded on {kind} #{number}")
        else:
            print(
                f"  ❌ intervention       could not comment on {kind} #{number}: "
                f"{err.strip()}",
                file=sys.stderr,
            )
            all_ok = False
    if not all_ok:
        print(
            "[ERROR] Human intervention evidence was not durable on every "
            f"GitHub target. Local evidence follows:\n{body}",
            file=sys.stderr,
        )
    return all_ok


def heads_match(live_sha, expected_sha):
    """True when the live head is exactly the picker-selected head."""
    return bool(live_sha) and bool(expected_sha) and live_sha == expected_sha


def gate_verdict_path(repo_root, pr_num):
    """Where this PR's evaluated verdicts are parked between invocations.

    Lives in the git common directory so every worktree of the repository sees
    one file, and so it is never mistaken for repository content.
    """
    code, out, _ = run_cmd(
        ["git", "rev-parse", "--git-common-dir"], check=False, cwd=repo_root
    )
    if code != 0 or not out.strip():
        return ""
    common = out.strip()
    if not os.path.isabs(common):
        common = os.path.join(repo_root, common)
    return os.path.join(common, f"aru-gates-{pr_num}.json")


def save_gate_verdicts(repo_root, pr_num, gates):
    """Persists verdicts at evaluation time so a resumed close-out can use them.

    Without this the only invocation that ever tags a hiccuped merge is the
    resumed one, which never evaluated the gates — so every merge whose
    close-out stumbled would carry a permanently verdict-less checkpoint.
    """
    path = gate_verdict_path(repo_root, pr_num)
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([[n, bool(p), d] for n, p, d in gates], handle)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_gate_verdicts(repo_root, pr_num):
    """Reads back parked verdicts. None when absent or malformed.

    None is the honest answer: the checkpoint then records that the verdicts
    are not reproducible rather than inventing a set that was never evaluated.
    """
    path = gate_verdict_path(repo_root, pr_num)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None
    gates = []
    for row in data:
        if not isinstance(row, list) or len(row) != 3:
            return None
        gates.append((row[0], bool(row[1]), row[2]))
    return gates


def discard_gate_verdicts(repo_root, pr_num):
    """Removes the parked verdicts once a checkpoint has recorded them."""
    path = gate_verdict_path(repo_root, pr_num)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def checkpoint_tag_name(pr_num, merged_sha):
    """Names the checkpoint after the merge commit it marks.

    Deriving the name from the merged SHA rather than a counter is what makes a
    resumed close-out idempotent: the same merge always computes the same name,
    so the existence check in ``write_checkpoint_tag`` recognises its own
    earlier work instead of minting a second checkpoint for one merge.
    """
    if not merged_sha or merged_sha == "unknown":
        return ""
    return f"{CHECKPOINT_PREFIX}{pr_num}-{merged_sha[:7]}"


def checkpoint_message(pr, issue_nums, gates, gated_head, merged_sha):
    """Builds the annotated tag body: what landed, who touched it, what was checked."""
    issues = ", ".join(f"#{n}" for n in issue_nums) or "none"
    authors = ", ".join(label_values(pr, "author:")) or "unknown"
    reviewers = ", ".join(label_values(pr, "reviewed-by:")) or "none recorded"
    lines = [
        f"checkpoint: PR #{pr.get('number')} — {pr.get('title') or ''}".rstrip(" —"),
        "",
        f"issues:      {issues}",
        f"author:      {authors}",
        f"reviewed-by: {reviewers}",
        f"gated head:  {gated_head}",
        f"merged as:   {merged_sha}",
        "",
        "gate verdicts:",
    ]
    if gates is None:
        # The resume path never evaluates the gates, and re-deriving them now
        # would be actively false: check_open fails on an already-closed PR, so
        # a re-derived block would record failures that never happened. A
        # checkpoint that admits the gap beats one that lies about it.
        lines.append(
            "  not reproducible — written by a resumed close-out; the gates "
            "were evaluated by the original invocation."
        )
    else:
        lines.extend(
            f"  {'✅' if passed else '❌'} {name}: {detail}"
            for name, passed, detail in gates
        )
    return "\n".join(lines) + "\n"


def write_checkpoint_tag(repo_root, pr, issue_nums, gates, gated_head, merged_sha):
    """Writes the annotated checkpoint tag. Returns ``(ok, message)``; never raises.

    Call this only after close-out succeeds, so a checkpoint can never claim a
    success that did not happen. Every failure is reported and swallowed: an
    already-merged, already-closed-out PR must not be failed retroactively
    because a tag write did not land, matching the close-out behaviour in #41.
    """
    name = checkpoint_tag_name(pr.get("number"), merged_sha)
    if not name:
        return False, "No merge commit SHA; no checkpoint written."
    ref = f"refs/tags/{name}"
    try:
        # Fetch before asking whether the tag exists, not after. `git fetch`
        # auto-follows tags reachable from the history it downloads, so a check
        # placed first concludes "absent" and is then contradicted by the fetch
        # three lines later — `git tag` fails with "already exists" and a
        # healthy merge reports a checkpoint failure. Routine with the isolated
        # clones `launch_fleet.sh -n N` hands out.
        run_cmd(["git", "fetch", "--quiet", "origin"], check=False, cwd=repo_root)

        code, _, _ = run_cmd(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            check=False, cwd=repo_root,
        )
        existed = code == 0

        if not existed:
            code, _, _ = run_cmd(
                ["git", "cat-file", "-e", f"{merged_sha}^{{commit}}"],
                check=False, cwd=repo_root,
            )
            if code != 0:
                return False, (
                    f"Merge commit {merged_sha} is not present locally; "
                    f"checkpoint {name} not written."
                )

            message = checkpoint_message(pr, issue_nums, gates, gated_head, merged_sha)
            # Never -f. An existing checkpoint is history; moving it would
            # destroy the very record this tag exists to preserve.
            code, _, err = run_cmd(
                ["git", "tag", "-a", name, merged_sha, "-m", message],
                check=False, cwd=repo_root,
            )
            if code != 0:
                # A concurrent close-out may have created it between the check
                # and here. The tag existing is the outcome we wanted, so
                # confirm rather than report a failure that did not occur.
                code, _, _ = run_cmd(
                    ["git", "rev-parse", "--verify", "--quiet", ref],
                    check=False, cwd=repo_root,
                )
                if code != 0:
                    return False, (
                        f"Could not write checkpoint {name}: {err or 'git tag failed'}"
                    )
                existed = True

        # Push on every path, including when the tag already existed locally.
        # Returning early on "already recorded" would mean a single transient
        # push failure un-publishes the checkpoint permanently: every later
        # resumed close-out would short-circuit before reaching the push, and
        # nothing would ever reconcile it. Pushing a tag origin already holds is
        # a no-op, so retrying costs nothing and makes the grid self-healing.
        code, _, err = run_cmd(
            ["git", "push", "--quiet", "origin", ref],
            check=False, cwd=repo_root,
        )
        verb = "already recorded" if existed else "written"
        if code != 0:
            return True, (
                f"Checkpoint {name} {verb} locally; push failed "
                f"({err or 'unknown'}). A later close-out retries the push."
            )
        return True, f"Checkpoint {name} {verb} and published."
    except Exception as exc:  # A tag must never take down a completed merge.
        return False, f"Unexpected checkpoint error: {exc}"


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Merge a PR only if the Definition of Done is met.")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--dry-run", action="store_true", help="Run every check, merge nothing")
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --dry-run, emit machine-readable gate results instead of text",
    )
    parser.add_argument("--merge-method", default="merge", choices=["squash", "merge", "rebase"])
    parser.add_argument(
        "--expected-head",
        default=None,
        metavar="SHA",
        help="Head SHA selected by the picker; refuse if the live head differs",
    )
    parser.add_argument(
        "--emit-review-split",
        action="store_true",
        help=(
            "When review rounds cross the threshold, post automated split "
            "guidance and file follow-up issues with depends-on edges. "
            "Never creates a human gate. Compatible with --dry-run (plan only)."
        ),
    )
    args = parser.parse_args()

    if args.json and not args.dry_run and not args.emit_review_split:
        print("[ERROR] --json requires --dry-run (refusing to emit JSON for a live merge).", file=sys.stderr)
        return EXIT_ERROR

    if args.emit_review_split and args.expected_head:
        print(
            "[ERROR] --emit-review-split cannot be combined with --expected-head.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    pr = fetch_pr(args.pr)
    if not pr:
        return EXIT_ERROR

    if args.emit_review_split:
        result = emit_review_round_split(pr, apply=not args.dry_run)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            plan = result["plan"]
            print(
                f"=== Review-round split — PR #{args.pr}: "
                f"{plan['rounds']} round(s), threshold {plan['threshold']} ==="
            )
            print(f"  crossed: {plan['crossed']}")
            print(f"  emitted: {result['emitted']} ({result['reason']})")
            for url in result.get("created_issues") or []:
                print(f"  filed: {url}")
            if args.dry_run or not result["emitted"]:
                print("\n--- plan comment preview ---")
                print(plan["comment"])
        reason = str(result["reason"])
        fail_prefixes = (
            "issue create failed",
            "comment failed",
            "board attach failed",
            "could not query existing split follow-ups",
            "issue create returned unparseable",
        )
        if result["reason"] in {"could not read PR comments"} or any(
            reason.startswith(p) for p in fail_prefixes
        ):
            return EXIT_ERROR
        return EXIT_OK

    issue_nums = linked_issues(pr.get("body"))
    if not issue_nums:
        print("[ERROR] PR body has no 'Closes #<issue>'; close-out target is unknown.", file=sys.stderr)
        return EXIT_ERROR

    gated_head = pr.get("headRefOid") or "unknown"
    gated_base = pr.get("baseRefOid") or "unknown"
    # Stays None on the resume path, where no gate is evaluated. The checkpoint
    # records that gap rather than inventing a verdict set.
    gates = None
    if args.expected_head and not is_merged(pr):
        if not heads_match(gated_head, args.expected_head):
            print(
                f"[ERROR] Live head {gated_head} does not match picker-selected "
                f"--expected-head {args.expected_head}. Refusing to merge a "
                "different commit than the one that was claimed.",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    if is_merged(pr):
        final_pr = pr
        if args.dry_run:
            if args.json:
                # JSON mode must emit only a parseable document on stdout.
                print(json.dumps({
                    "pr": args.pr,
                    "title": pr.get("title") or "",
                    "ok": True,
                    "gates": [],
                    "first_blocking": None,
                    "already_merged": True,
                }))
            else:
                print(f"=== Merge execution — PR #{args.pr}: already merged; resuming close-out ===")
                print("No mutations performed in --dry-run mode.")
            return EXIT_OK
        print(f"=== Merge execution — PR #{args.pr}: already merged; resuming close-out ===")
    else:
        issue_bodies = {}
        for num in issue_nums:
            issue = _gh_json(["gh", "issue", "view", str(num), "--json", "body"])
            if issue is None:
                return EXIT_ERROR
            issue_bodies[num] = issue.get("body") or ""

        evidence = review_evidence(args.pr)
        evidence_head = evidence.get("head_oid") if evidence else None
        if not heads_match(gated_head, evidence_head):
            reason = (
                f"[ERROR] Review evidence covers head {evidence_head or 'unknown'}, "
                f"but the gated PR snapshot is {gated_head}. Refusing to combine "
                "evidence from different commits."
            )
            if args.json:
                print(json.dumps(dry_run_json_payload(
                    pr,
                    [("review head", False, reason.removeprefix("[ERROR] "))],
                    False,
                )))
            else:
                print(reason, file=sys.stderr)
            return EXIT_BLOCKED
        ok, gates = evaluate_dod(pr, issue_bodies, evidence)

        if args.json:
            print(json.dumps(dry_run_json_payload(pr, gates, ok)))
            return EXIT_OK if ok else EXIT_BLOCKED

        print(f"=== Definition of Done — PR #{args.pr}: {pr.get('title','')} ===")
        blocked = []
        for name, passed, message in gates:
            print(f"  {'✅' if passed else '❌'} {name:<11} {message}")
            if not passed:
                blocked.append(name)
        if blocked:
            print(f"\n🚫 Not merged. Unmet: {', '.join(blocked)}.")
            return EXIT_BLOCKED
        if args.dry_run:
            print("\n✅ Every gate passed. --dry-run, so nothing was merged.")
            return EXIT_OK

        needs_verify = any(
            item.argv
            for body in issue_bodies.values()
            for item in acceptance_runner.parse_criteria(body)
        )
        if needs_verify:
            checkout, checkout_err = ensure_pr_head_checkout(pr)
            if checkout_err:
                print(f"\n🚫 Not merged. Unmet: accept. {checkout_err}")
                return EXIT_BLOCKED
            try:
                records = []
                for num in issue_nums:
                    passed, message = check_acceptance(
                        num, issue_bodies.get(num, ""), cwd=checkout, execute=True,
                        records_out=records,
                    )
                    print(f"  {'✅' if passed else '❌'} accept #{num:<4} {message}")
                    if not passed:
                        persist_acceptance_evidence(args.pr, pr, records)
                        return EXIT_BLOCKED
                persisted, persist_msg = persist_acceptance_evidence(args.pr, pr, records)
                print(f"  {'✅' if persisted else '❌'} evidence    {persist_msg}")
                if not persisted:
                    return EXIT_BLOCKED
            finally:
                release_pr_head_checkout(checkout)

        # Serialize the final authoritative reread plus server merge across all
        # local clones of this repository. Releasing PR-open path reservations
        # is safe only if another merge cannot change the base between this
        # check and execution.
        with repository_merge_lock() as (locked, lock_message):
            if not locked:
                print(f"[ERROR] {lock_message}. No merge command was run.", file=sys.stderr)
                return EXIT_BLOCKED
            fresh = fetch_pr(args.pr)
            if not fresh:
                return EXIT_ERROR
            live = fresh.get("headRefOid") or "unknown"
            if not heads_match(live, gated_head):
                print(
                    f"[ERROR] Head moved to {live} after DoD checks; gated head was "
                    f"{gated_head}. No merge command was run.",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            live_base = fresh.get("baseRefOid") or "unknown"
            if (
                gated_base == "unknown"
                or live_base == "unknown"
                or not heads_match(live_base, gated_base)
            ):
                print(
                    f"[ERROR] Base moved to {live_base} after DoD checks; gated base was "
                    f"{gated_base}. Rebase and reverify before merging.",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            rebased, rebased_message = check_rebased(fresh)
            if not rebased:
                print(
                    f"[ERROR] Final rebased check failed: {rebased_message}",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            pr = fresh

            print(f"  ✅ merge lock          {lock_message}")
            print(f"  ✅ final base check    {rebased_message}")
            print("\n=== Merge execution ===")
            final_pr, outcome = execute_merge(args.pr, pr, args.merge_method)
            if not final_pr:
                print(f"  ❌ not merged          {outcome}", file=sys.stderr)
                return EXIT_ERROR
            print(f"  ✅ server merge        {outcome}")

    merged_sha = merge_commit_oid(final_pr) or "unknown"
    audit_ok = merged_sha != "unknown"
    print(
        f"AUDIT pr=#{args.pr} gated_head_sha={gated_head} "
        f"merged_sha={merged_sha}"
    )
    if not audit_ok:
        print("[ERROR] GitHub reported merged but supplied no merge commit SHA.", file=sys.stderr)

    root = repository_root()
    command = intervention_command()
    if not root:
        failure = "repository root: could not resolve the primary worktree"
        evidence_ok = post_human_intervention(
            final_pr, issue_nums, None, gated_head, merged_sha, [], command,
            blocked_before_closeout=failure,
        )
        print(
            "[ERROR] Merge succeeded but repository root could not be resolved; "
            f"intervention evidence {'was recorded' if evidence_ok else 'could not be fully recorded'}.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not audit_ok:
        failure = "merge audit: GitHub supplied no merge commit SHA"
        evidence_ok = post_human_intervention(
            final_pr, issue_nums, root, gated_head, merged_sha, [], command,
            blocked_before_closeout=failure,
        )
        print(
            "[ERROR] Merge audit is incomplete; intervention evidence "
            f"{'was recorded' if evidence_ok else 'could not be fully recorded'}.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    # Park the verdicts the moment we hold them, and read them back on a
    # resumed close-out. Re-deriving them post-merge is not an option:
    # check_open fails on a closed PR, so a re-evaluated block would record
    # failures that never happened.
    if gates is not None:
        save_gate_verdicts(root, args.pr, gates)
    else:
        gates = load_gate_verdicts(root, args.pr)

    closeout_ok, failed_attempts = run_closeout_with_retries(
        final_pr, issue_nums, root
    )
    if not closeout_ok:
        evidence_ok = post_human_intervention(
            final_pr, issue_nums, root, gated_head, merged_sha,
            failed_attempts, command,
        )
        print(
            "\n❌ Merge is complete, but close-out is incomplete after bounded "
            "retries. Human intervention evidence "
            f"{'was recorded' if evidence_ok else 'could not be fully recorded'}."
        )
        return EXIT_ERROR

    # Only here: after close-out succeeded, so no checkpoint can ever claim a
    # success that did not happen. A failed write is a warning, not a failure —
    # the merge is already complete and must not be reported as broken.
    tag_ok, tag_message = write_checkpoint_tag(
        root, final_pr, issue_nums, gates, gated_head, merged_sha
    )
    # A push failure still returns ok, so mark the line by what it reports.
    icon = "✅" if tag_ok and "push failed" not in tag_message else (
        "⚠️ " if tag_ok else "❌"
    )
    print(f"  {icon} {'checkpoint':<18} {tag_message}")
    if tag_ok:
        discard_gate_verdicts(root, args.pr)

    print("\n✅ Merge and every close-out step completed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
