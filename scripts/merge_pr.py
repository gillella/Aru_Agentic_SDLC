#!/usr/bin/env python3
# line-ceiling: 4650
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
from create_pr import (
    render_verification_evidence,
    replace_verification_evidence,
)
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
CODERABBIT_LOGINS = {"coderabbitai", "coderabbitai[bot]"}
CODERABBIT_APP_SLUGS = {"coderabbitai"}
CODERABBIT_ACTOR_TYPES = {"Bot"}
REVIEW_SERVICE_LABELS = ("review:coderabbit",)
CODERABBIT_FULL_REVIEW_REQUEST = "@coderabbitai full review"
CODERABBIT_FULL_REVIEW_FINISHED = "Full review finished."
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
GITHUB_ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/jobs/\d+)?(?:$|[?#/])")
_REWORK_BLOCKING_RE = re.compile(
    r"changes[\s_-]*requested|(?<![Nn]o )blocking findings?|\*\*blocking:\*\*",
    re.I,
)

# One initial close-out attempt plus these bounded retries.  Keeping the policy
# in the merge authority prevents desktop clients from disagreeing about when
# a transient cleanup failure becomes exceptional operator intervention.
CLOSEOUT_RETRY_DELAYS = (5, 15, 45)

# `execute_merge` prefixes every refusal it reaches *before* running the merge
# command with this. It lets `main` report a gate refusal as blocked rather
# than as a merge failure without taking a second base read, which would
# reopen the very window the refusal exists to close (#371 review).
MERGE_NOT_ATTEMPTED = "No merge command was run:"

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


def _normalize_comment_body(body):
    """Collapse whitespace so command comments compare deterministically."""
    return re.sub(r"\s+", " ", (body or "").strip())


def _coderabbit_full_review_comment_kind(body):
    """Identify exact full-review request/completion comments, if any."""
    normalized = _normalize_comment_body(body).casefold()
    if normalized == CODERABBIT_FULL_REVIEW_REQUEST:
        return "request"
    if normalized == CODERABBIT_FULL_REVIEW_FINISHED.casefold():
        return "completion"
    return None


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
    """Return complete review history only while every page has one head."""
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
    """Read paginated head attestations and exact CodeRabbit command comments."""
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          comments(first:100, after:$cursor) {
            nodes { body createdAt author { login __typename } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    prefix = f"<!-- {REVIEW_HEAD_ATTESTATION_VERSION} "
    cursor = None
    seen_cursors = set()
    attestations = []
    coderabbit_full_review_comments = []
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
            comment_kind = _coderabbit_full_review_comment_kind(body)
            if comment_kind is not None:
                created_at = _parse_ts(node.get("createdAt"))
                author = node.get("author")
                if (
                    created_at is None
                    or not isinstance(author, dict)
                    or not isinstance(author.get("login"), str)
                    or not author["login"]
                    or author.get("__typename") not in KNOWN_REVIEW_ACTOR_TYPES
                ):
                    return None
                coderabbit_full_review_comments.append({
                    "kind": comment_kind,
                    "body": body,
                    "createdAt": node["createdAt"],
                    "author": {
                        "login": author["login"],
                        "__typename": author["__typename"],
                    },
                })
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
            return {
                "attestations": attestations,
                "coderabbit_full_review_comments": coderabbit_full_review_comments,
            }
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str) or not next_cursor
            or next_cursor in seen_cursors
        ):
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def review_evidence(pr_id):  # noqa: C901, PLR0912, PLR0915
    """Return complete, head-stable review/thread facts, or ``None``."""
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    review_result = _reviewed_current_head(owner, name, pr_id)
    if review_result is None:
        return None
    expected_head, reviewed_head, reviews = review_result
    comment_evidence = _review_head_attestations(
        owner, name, pr_id, expected_head
    )
    if comment_evidence is None:
        return None
    if isinstance(comment_evidence, list):
        review_attestations = comment_evidence
        coderabbit_full_review_comments = []
    else:
        review_attestations = comment_evidence["attestations"]
        coderabbit_full_review_comments = comment_evidence[
            "coderabbit_full_review_comments"
        ]
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
              comments(first:50) {
                totalCount
                nodes { createdAt body author { login __typename } }
                pageInfo { hasNextPage endCursor }
              }
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
    service_threads = {
        "coderabbit": {"unresolved": 0, "unfixed": 0, "outdated_unfixed": 0},
    }

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
            if not isinstance(node, dict):
                return None
            resolved = node.get("isResolved")
            outdated = node.get("isOutdated")
            comment_connection = node.get("comments")
            if (
                not isinstance(resolved, bool)
                or not isinstance(outdated, bool)
                or not isinstance(comment_connection, dict)
            ):
                return None
            comments = comment_connection.get("nodes")
            total_comments = comment_connection.get("totalCount")
            comment_page = comment_connection.get("pageInfo")
            if (
                not isinstance(comments, list)
                or type(total_comments) is not int
                or not isinstance(comment_page, dict)
                or not isinstance(comment_page.get("hasNextPage"), bool)
                or comment_page["hasNextPage"]
                or total_comments != len(comments)
            ):
                return None
            if any(
                not isinstance(comment, dict)
                or not isinstance(comment.get("body"), str)
                or _parse_ts(comment.get("createdAt")) is None
                or not isinstance(comment.get("author"), dict)
                or not isinstance(comment["author"].get("login"), str)
                or not comment["author"]["login"]
                or comment["author"].get("__typename") not in KNOWN_REVIEW_ACTOR_TYPES
                for comment in comments
            ):
                return None
            thread_service = None
            if comments:
                root = comments[0]
                author = root.get("author")
                if not isinstance(author, dict):
                    author = {}
                login = str(author.get("login") or "").lower()
                if author.get("__typename") != "Bot":
                    thread_service = None
                elif login in CODERABBIT_LOGINS:
                    thread_service = "coderabbit"

            if not resolved and not outdated:
                if thread_service is None:
                    return None
                unresolved += 1
                service_threads[thread_service]["unresolved"] += 1
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
                    if thread_service:
                        service_threads[thread_service]["outdated_unfixed"] += 1
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
                    if thread_service:
                        service_threads[thread_service]["unfixed"] += 1

        if not has_next:
            return {
                "head_oid": expected_head,
                "head_commit_committed_at": (
                    commit_times[-1].isoformat() if commit_times else None
                ),
                "github_review_evidence": True,
                "reviews": reviews,
                "review_attestations": review_attestations,
                "coderabbit_full_review_comments": (
                    coderabbit_full_review_comments
                ),
                "unresolved": unresolved,
                "unfixed": unfixed,
                "outdated_unfixed": outdated_unfixed,
                "outdated_addressed": outdated_addressed,
                "body_addressed": body_addressed,
                "withdrawn": withdrawn,
                "reviewed_head": reviewed_head,
                "service_threads": service_threads,
            }
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


def _utc_stamp(raw):
    """A GitHub ISO-8601 timestamp as an aware UTC-comparable datetime, or None.

    Naive values are read as UTC, which is what the API emits. Everything that
    orders runs or compares a run against a commit goes through here, so a
    format the parser cannot read is "unusable" everywhere rather than usable
    in one caller and not another.
    """
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


def _check_time(check):
    """When this run finished, for ordering runs of the same check.

    Falls back to the start time when a run has not completed. Returns None
    when neither timestamp is usable, which the caller treats as "cannot be
    ordered" rather than "is current".
    """
    return _utc_stamp(check.get("completedAt") or check.get("startedAt"))


def _check_start_time(check):
    """When this run began, which is the only stamp that bounds what it read.

    Deliberately not `_check_time`: a run that *finished* after the base moved
    may well have checked the merge ref out before it moved, so a completion
    stamp cannot show that a run saw the base advance. The start stamp can.
    """
    return _utc_stamp(check.get("startedAt"))


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


def assigned_review_service(pr):
    """Return CodeRabbit only for one exact, well-formed authority label."""
    if not isinstance(pr, dict) or not isinstance(pr.get("labels"), list):
        return None
    labels = []
    for label in pr["labels"]:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            return None
        if label["name"].startswith("review:"):
            labels.append(label["name"])
    if labels != [REVIEW_SERVICE_LABELS[0]]:
        return None
    return "coderabbit"


def _service_thread_counts(evidence, service):
    threads = evidence.get("service_threads") if isinstance(evidence, dict) else None
    if isinstance(threads, dict) and isinstance(threads.get(service), dict):
        return threads[service]
    return evidence if isinstance(evidence, dict) else None


def _thread_gate_message(counts):
    values = tuple(counts.get(key, 0) for key in ("unresolved", "outdated_unfixed", "unfixed"))
    if any(type(value) is not int or value < 0 for value in values):
        return False, "Could not determine trustworthy review-thread counts."
    unresolved, outdated_unfixed, unfixed = values
    if unresolved > 0 or outdated_unfixed > 0:
        total = unresolved + outdated_unfixed
        if unresolved > 0 and outdated_unfixed > 0:
            return False, f"{total} unresolved review thread(s) ({outdated_unfixed} outdated without evidence)."
        if unresolved > 0:
            return False, f"{unresolved} unresolved review thread(s)."
        return False, f"{outdated_unfixed} outdated review thread(s) without evidence."
    if unfixed > 0:
        return False, (
            f"{unfixed} resolved thread(s) have no evidence that the finding was addressed: "
            "no commit after the finding was raised, no relevant size-waiver or verification-evidence "
            "body edit after it was raised, and no reply starting with 'Withdrawn:'. "
            "Push the fix, apply the documented body-only gate remedy when it matches the finding, "
            "or withdraw the finding with a reason."
        )
    return True, ""


def latest_state_per_reviewer(reviews):
    """Collapse full review history to each reviewer's unambiguous last verdict."""
    latest = {}
    for review in reviews:
        state = (review.get("state") or "").upper()
        if state in {"PENDING", "COMMENTED"}:
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
    """Known human accounts with accepted reviews on the evidenced head."""
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
    """Return non-empty, trimmed agent identities from labels."""
    return [value for value in
            (raw.strip() for raw in label_values(pr, prefix)) if value]


def reviewer_families(pr):
    """Map each reviewer id to every distinct stamped model family."""
    families = {}
    for value in label_values(pr, REVIEWER_FAMILY_LABEL):
        agent_id, _, family = value.partition(":")
        if agent_id and family:
            families.setdefault(agent_id, [])
            if family not in families[agent_id]:
                families[agent_id].append(family)
    return {agent_id: sorted(values) for agent_id, values in families.items()}


def classify_reviewers(pr, reviewers, author):
    """Split reviewers into peers, id/family collisions, and unknown identity."""
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
    """Explain an agent id stamped to distinct author/reviewer families."""
    agent_id, author_family, reviewer_family = collisions[0]
    return (
        f"Agent id '{agent_id}' is stamped as both the author "
        f"(family:{author_family}) and a reviewer (family:{reviewer_family}) of "
        "this PR. Two agents are sharing one id, so no attribution on it can be "
        "trusted - this is neither a self-review nor a valid peer review. "
        "Reissue one of them a distinct id (see #304) and re-review."
    )


def self_review_message(author, unresolved):
    """Explain self-review or the labels needed to rule out an id collision."""
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
    """Describe the exact evidence that satisfied the review gate."""
    out_addressed = evidence.get("outdated_addressed", 0) if evidence else 0
    head = evidence.get("head_oid") if evidence else None
    head_reviewers = _current_head_reviewers(evidence)
    if head and head_reviewers:
        head_note = (
            f"current head {head[:12]} has accepted independent review from "
            f"{', '.join(head_reviewers)}"
        )
    else:
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


def _coderabbit_status_evidence(owner, name, pr_id, expected_head):
    """Read every typed status page while proving one stable PR head."""
    if not isinstance(expected_head, str) or not expected_head:
        return None
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          commits(last:1) { nodes { commit { statusCheckRollup { contexts(first:100, after:$cursor) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on CheckRun { name status conclusion checkSuite { app { slug } } }
              ... on StatusContext { context state creator { login __typename } }
            }
          } } } } }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    contexts = []
    expected_total = None
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
            commits = pull["commits"]["nodes"]
            connection = commits[0]["commit"]["statusCheckRollup"]["contexts"]
            page_nodes = connection["nodes"]
            total_count = connection["totalCount"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, IndexError, TypeError):
            return None
        if (
            pull.get("headRefOid") != expected_head
            or not isinstance(page_nodes, list)
            or type(total_count) is not int
            or not isinstance(has_next, bool)
            or expected_total not in {None, total_count}
        ):
            return None
        expected_total = total_count
        contexts.extend(page_nodes)
        if not has_next:
            return contexts if expected_total == len(contexts) else None
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in seen_cursors
        ):
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _with_coderabbit_status(pr_id, evidence):
    """Bind review evidence to an authoritative, producer-identified status."""
    if not isinstance(evidence, dict):
        return None
    # Pure unit callers use compact handcrafted evidence; live evidence always
    # carries this marker from review_evidence().
    if not evidence.get("github_review_evidence"):
        return evidence
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    statuses = _coderabbit_status_evidence(owner, name, pr_id, evidence.get("head_oid"))
    if statuses is None:
        return None
    combined = dict(evidence)
    combined["coderabbit_status"] = statuses
    return combined


def _coderabbit_check(evidence):  # noqa: C901, PLR0912
    """Return an authenticated exact-head CodeRabbit status, or ``None``."""
    if not isinstance(evidence, dict):
        return None
    rollup = evidence.get("coderabbit_status")
    if not isinstance(rollup, list):
        return None
    matches = []
    for item in rollup:
        if not isinstance(item, dict):
            return None
        name = item.get("name") or item.get("context")
        if isinstance(name, str) and name.strip().lower() == "coderabbit":
            matches.append(item)
    if len(matches) != 1:
        return None
    check = matches[0]
    kind = check.get("__typename") or check.get("type")
    if kind == "CheckRun":
        suite = check.get("checkSuite") or {}
        slug = str((suite.get("app") or {}).get("slug") or "").lower()
        if slug not in CODERABBIT_APP_SLUGS:
            return None
    elif kind == "StatusContext":
        creator = check.get("creator")
        if (not isinstance(creator, dict)
                or str(creator.get("login") or "").lower() not in CODERABBIT_LOGINS
                or creator.get("__typename") not in CODERABBIT_ACTOR_TYPES):
            return None
    else:
        return None
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
    if (check.get("__typename") or check.get("type")) == "CheckRun":
        return status == "COMPLETED" and conclusion == "SUCCESS"
    if status and status != "COMPLETED":
        return False
    return conclusion == "SUCCESS"


def _parse_coderabbit_full_review_comment(comment):
    """Parse one exact-match full-review request/completion comment."""
    if not isinstance(comment, dict):
        return False
    kind = comment.get("kind") or _coderabbit_full_review_comment_kind(
        comment.get("body")
    )
    if kind is None:
        return None
    created_at = _parse_ts(comment.get("createdAt"))
    author = comment.get("author")
    if (
        created_at is None
        or not isinstance(author, dict)
        or not isinstance(author.get("login"), str)
        or not author["login"]
        or author.get("__typename") not in KNOWN_REVIEW_ACTOR_TYPES
    ):
        return False
    login = author["login"].lower()
    actor_type = author["__typename"]
    if kind == "request":
        if actor_type != "User" or login in CODERABBIT_LOGINS:
            return False
    elif kind == "completion":
        if actor_type not in CODERABBIT_ACTOR_TYPES or login not in CODERABBIT_LOGINS:
            return False
    else:
        return False
    return kind, created_at


def _coderabbit_full_review_comment_times(evidence):
    """Return validated request/completion timestamps, or None on ambiguity."""
    comments = evidence.get("coderabbit_full_review_comments")
    if not isinstance(comments, list):
        return None
    requests = []
    completions = []
    for comment in comments:
        parsed = _parse_coderabbit_full_review_comment(comment)
        if parsed is False:
            return None
        if parsed is None:
            continue
        kind, created_at = parsed
        if kind == "request":
            requests.append(created_at)
        else:
            completions.append(created_at)
    return requests, completions


def _unique_selected_timestamp(timestamps, *, select):
    """Return a uniquely newest/oldest timestamp, else None."""
    if not timestamps:
        return None
    selected = select(timestamps)
    if sum(ts == selected for ts in timestamps) != 1:
        return None
    return selected


def _coderabbit_no_findings_full_review(review, evidence):
    """Accept an empty COMMENTED review only with full-review completion evidence."""
    if not isinstance(review, dict) or not isinstance(evidence, dict):
        return False
    if str(review.get("state") or "").upper() != "COMMENTED":
        return False
    body = review.get("body")
    if not isinstance(body, str) or body.strip():
        return False
    review_time = _parse_review_ts(review.get("submittedAt"))
    head_commit_time = _parse_ts(evidence.get("head_commit_committed_at"))
    comments = evidence.get("coderabbit_full_review_comments")
    if review_time is None or head_commit_time is None or comments is None:
        return False
    parsed = _coderabbit_full_review_comment_times(evidence)
    if parsed is None:
        return False
    requests, completions = parsed

    eligible_requests = [created_at for created_at in requests
                         if head_commit_time <= created_at < review_time]
    if _unique_selected_timestamp(eligible_requests, select=max) is None:
        return False
    if any(created_at >= review_time for created_at in requests):
        return False

    eligible_completions = [ts for ts in completions if review_time < ts]
    return _unique_selected_timestamp(eligible_completions, select=min) is not None


def _coderabbit_latest_review(evidence):  # noqa: C901, PLR0912
    """Select and validate CodeRabbit's unique newest completed review verdict."""
    if not isinstance(evidence, dict):
        return None
    head = evidence.get("head_oid")
    if not isinstance(head, str) or not head:
        return None
    candidates = []
    for review in evidence.get("reviews") or []:
        if not isinstance(review, dict):
            return None
        author = review.get("author") or {}
        if not isinstance(author, dict):
            return None
        login = str(author.get("login") or "").lower()
        if login not in CODERABBIT_LOGINS:
            continue
        if author.get("__typename") not in CODERABBIT_ACTOR_TYPES:
            return None
        state = str(review.get("state") or "").upper()
        submitted = _parse_review_ts(review.get("submittedAt"))
        body = review.get("body")
        oid = (review.get("commit") or {}).get("oid")
        if state == "PENDING":
            return None
        if not isinstance(oid, str) or not oid:
            return None
        if state == "DISMISSED":
            continue
        if (state not in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
                or submitted is None or not isinstance(body, str)):
            return None
        review_id = review.get("id")
        if not isinstance(review_id, str) or not review_id:
            return None
        candidates.append((submitted, review_id, review))
    newest = max((candidate[0] for candidate in candidates), default=None)
    candidates = [candidate for candidate in candidates if candidate[0] == newest]
    if len(candidates) != 1:
        return None
    selected = candidates[0][2]
    if (selected.get("commit") or {}).get("oid") != head:
        return None
    if (str(selected.get("state") or "").upper() == "COMMENTED"
            and not selected["body"].strip()
            and not _coderabbit_no_findings_full_review(selected, evidence)):
        return None
    return selected


def has_authoritative_coderabbit_review(pr, evidence):
    """True only when CodeRabbit reviewed this PR and attested the current head."""
    if not isinstance(pr, dict) or not isinstance(evidence, dict):
        return False
    review = _coderabbit_latest_review(evidence)
    if not isinstance(review, dict):
        return False
    if str(review.get("state") or "").upper() == "CHANGES_REQUESTED":
        return False
    return _coderabbit_check(evidence) is True


def check_reviews(pr, evidence):  # noqa: C901, PLR0912
    service = assigned_review_service(pr) if isinstance(pr, dict) else None
    if service is None:
        return False, (
            "PR must carry exactly one authoritative review label: "
            "review:coderabbit. Missing, legacy, unknown, mixed, or duplicate "
            "review labels block merge."
        )
    counts = _service_thread_counts(evidence, service)
    if not isinstance(counts, dict):
        return False, f"Could not determine {service.title()} review-thread state; refusing rather than guessing."
    ok, thread_message = _thread_gate_message(counts)
    if not ok:
        return False, thread_message

    # Prefer the same explicitly paginated review history used for current-head
    # evidence. The PR snapshot remains a compatibility fallback for pure
    # unit-level callers that supply handcrafted evidence.
    reviews = (
        evidence.get("reviews")
        if evidence is not None and "reviews" in evidence
        else pr.get("reviews")
    ) or []
    if not isinstance(reviews, list) or any(not isinstance(r, dict) for r in reviews):
        return False, "Could not establish trustworthy review history."
    submitted = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]

    verdicts = latest_state_per_reviewer(reviews)
    if verdicts is None:
        return False, (
            "Could not establish an unambiguous latest review verdict; "
            "refusing rather than trusting review page order."
        )
    advisory_accounts = {
        ((r.get("author") or {}).get("login") or "").lower()
        for r in submitted if is_advisory_review_actor(r)
    }
    blocking = [
        who for who, state in verdicts.items()
        if state == "CHANGES_REQUESTED"
        and who.lower() not in advisory_accounts
        and not is_advisory_review_account(who)
    ]
    if blocking:
        return False, (f"{', '.join(blocking)} requested changes and has not re-approved.")
    ok, thread_message = _thread_gate_message(evidence)
    if not ok:
        return False, thread_message

    if not submitted:
        return False, "No review on this PR. At least one review is required."

    latest_coderabbit_review = _coderabbit_latest_review(evidence)
    if latest_coderabbit_review is None:
        return False, (
            "CodeRabbit has not supplied one accepted completed review verdict in "
            "this PR's review history plus a successful authoritative "
            "current-head CodeRabbit status. Missing, malformed, pending, "
            "ambiguous, or spoofed evidence blocks merge."
        )
    if str(latest_coderabbit_review.get("state") or "").upper() == "CHANGES_REQUESTED":
        return False, "CodeRabbit requested changes and has not re-approved."
    if not has_authoritative_coderabbit_review(pr, evidence):
        return False, (
            "CodeRabbit has not supplied a successful authoritative "
            "current-head CodeRabbit status tied to the latest accepted "
            "review verdict for this PR. Missing, pending, failed, "
            "rate-limited, stale, ambiguous, or spoofed evidence blocks "
            "merge."
        )
    return True, (
        f"CodeRabbit status is complete on current head "
        f"{evidence['head_oid'][:12]}; {_evidence_note(evidence)}"
    )


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


# GitHub's compare endpoint returns at most this many file entries. A response
# sitting exactly at the cap may have been truncated, and truncation can only
# ever make two change sets look *more* disjoint than they really are -- which
# is the one direction this gate must never be wrong in. Treat it as unknown.
COMPARE_FILE_LIMIT = 300


def _compare_paths(base_ref, head_sha):
    """Paths changed from the merge-base of the two refs up to `head_sha`.

    This is GitHub's three-dot compare, the same endpoint and direction
    `_behind_by` already uses. Calling it with the arguments swapped yields the
    other side of the fork -- what the base advanced by -- so both sides of the
    overlap test come from one code path, with one rename rule and one
    truncation rule rather than two of each.

    Returns None for "could not determine", which every caller must treat as
    unverifiable rather than as an empty set. Renamed entries contribute both
    their old and new path, so a rename can never hide an overlap.
    """
    if not base_ref or not head_sha:
        return None
    slug = get_repo_slug()
    if not slug:
        return None
    data = _gh_json(["gh", "api", f"repos/{slug}/compare/{base_ref}...{head_sha}"])
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILE_LIMIT:
        return None
    paths = set()
    for entry in files:
        if not isinstance(entry, dict):
            return None
        name = entry.get("filename")
        if not isinstance(name, str) or not name:
            return None
        paths.add(name)
        previous = entry.get("previous_filename")
        if isinstance(previous, str) and previous:
            paths.add(previous)
    # An empty set means the compare reported no files at all. A real branch
    # always changes something, so this is malformed data, not a clean pass.
    return paths or None


def _overlap_with_base_advance(pr, paths_resolver=None):
    """Paths this branch and the base both changed since their merge-base.

    Returns a sorted list (empty when the two change sets are disjoint), or
    None when either side could not be determined.
    """
    resolve = paths_resolver or _compare_paths
    base_ref = pr.get("baseRefName")
    head_sha = pr.get("headRefOid")
    ours = resolve(base_ref, head_sha)
    if ours is None:
        return None
    # Swapped arguments: merge-base -> base tip, i.e. what the base advanced by.
    theirs = resolve(head_sha, base_ref)
    if theirs is None:
        return None
    return sorted(ours & theirs)


def _base_advance_time(base_ref, head_sha):
    """When the base advance this PR is behind by landed, or None if unknown.

    The three-dot compare with the arguments swapped -- the same call
    `_overlap_with_base_advance` uses for the base side -- lists exactly the
    commits the base gained since the merge-base. The newest stamp among them
    is the earliest moment a CI run could have seen the whole advance.

    Both the committer and the author date of every commit are read, and the
    maximum of all of them wins. `main` here only ever advances through
    GitHub's server-side merge, which sets the committer date to server time,
    so the committer date is the honest one. Taking the maximum over both means
    a commit carrying a *later* fabricated stamp than it deserves can only
    demand fresher CI, never accept staler CI. Any commit whose dates are
    missing or unparseable returns None, because a merge must not be approved
    against an advance whose age is unknown.

    The same rule governs a short list. This request sends no `per_page`, so
    GitHub caps `commits` at 250 while `total_commits` keeps counting the whole
    advance. Truncation is the dangerous direction here --
    the endpoint returns commits in chronological order, so the entries dropped
    are the newest ones, and the newest stamp is the entire answer. A truncated
    array would silently lower the freshness bar and admit CI that predates the
    advance. Refuse unless GitHub's own count matches what it actually sent.
    """
    if not base_ref or not head_sha:
        return None
    slug = get_repo_slug()
    if not slug:
        return None
    data = _gh_json(["gh", "api", f"repos/{slug}/compare/{head_sha}...{base_ref}"])
    if not isinstance(data, dict):
        return None
    commits = data.get("commits")
    # A branch reported behind must have something on the other side. An empty
    # or absent list contradicts that, so it is malformed data, not "no advance".
    if not isinstance(commits, list) or not commits:
        return None
    total = data.get("total_commits")
    # bool is an int subclass; True would otherwise satisfy a one-commit
    # advance. An absent, non-integer, or negative count fails the equality
    # too, so every unreadable form lands on the same refusal.
    if isinstance(total, bool) or not isinstance(total, int) or total != len(commits):
        return None
    newest = None
    for entry in commits:
        if not isinstance(entry, dict):
            return None
        commit = entry.get("commit")
        if not isinstance(commit, dict):
            return None
        for role in ("committer", "author"):
            who = commit.get(role)
            if not isinstance(who, dict):
                return None
            when = _utc_stamp(who.get("date"))
            if when is None:
                return None
            newest = when if newest is None else max(newest, when)
    return newest


def _ci_saw_base_advance(pr, advance_resolver=None, run_resolver=None):
    """Whether the CI on this head was produced after the base advance landed.

    Returns ``(ok, reason)``; ``reason`` is the fragment `check_rebased` quotes
    when this refuses.

    The gap being closed: GitHub triggers no new `pull_request` run when the
    base moves, so a PR that went green, then fell behind, keeps advertising
    that same green rollup. Those checks describe a merge with the *old* base.
    Disjoint paths do not repair that -- they show the two sides touched no
    common file, not that the older run exercised the newer base.

    A run that started strictly after the newest advance stamp is the first
    timing proof: it excludes any check that definitely began before the base
    changed. GitHub Actions workflow re-runs are a special case, though:
    GitHub documents that a re-run keeps the original event's `GITHUB_SHA` and
    `GITHUB_REF`, so a later `startedAt` alone does not prove that the run saw
    the current synthetic merge ref. For those checks, this function therefore
    also verifies that the underlying workflow run is an attempt-1
    `pull_request` run for this PR's current head whose own event-time
    `created_at` and `run_started_at` both postdate the base advance.

    The residual this still does *not* close is GitHub's own merge-ref
    recomputation lag: a fresh `pull_request` event can still begin in the
    seconds before the merge ref itself is rebuilt. That window is orders of
    magnitude smaller than the unbounded one above, but the run metadata still
    does not name the exact merge commit the runner checked out, so the literal
    tested-merge identity proof still cannot be derived from here. `check_rebased`
    closes that residual separately, right after this returns fresh, by reading
    the PR's own test-merge commit and checking its parents directly
    (`_merge_commit_parents`, issue #371).
    """
    resolve = advance_resolver or _base_advance_time
    advanced = resolve(pr.get("baseRefName"), pr.get("headRefOid"))
    if advanced is None:
        return False, "when the base advance landed could not be determined"

    # Advisory review bots are not build checks anywhere else in this file, so
    # they neither prove freshness nor block on lacking it (see check_ci).
    def required(names):
        return sorted(name for name in names
                      if (name or "").lower() not in ADVISORY_CHECK_CONTEXTS)

    current, unorderable = _current_runs(pr.get("statusCheckRollup") or [])
    undecidable = required(unorderable)
    if undecidable:
        return False, (
            f"which run is current is undecidable for {', '.join(undecidable)}, "
            f"so their age is unknown"
        )
    started = {name: _check_start_time(current[name]) for name in required(current)}
    if not started:
        return False, "no required check is reported on this head at all"

    undated = [name for name, when in started.items() if when is None]
    if undated:
        return False, (
            f"no usable start time is recorded for {', '.join(undated)}, so "
            f"whether those checks ran after the base advance is unverified"
        )
    stale = [name for name, when in started.items() if when <= advanced]
    if stale:
        shown = ", ".join(stale[:3])
        more = f" (+{len(stale) - 3} more)" if len(stale) > 3 else ""
        return False, (
            f"{shown}{more} started no later than the base advance at "
            f"{advanced.isoformat()}, so the green result describes a merge "
            f"with the superseded base"
        )
    run_cache = {}
    for name in required(current):
        evidence = _github_actions_current_base_evidence(
            pr, current[name], advanced, run_resolver=run_resolver, cache=run_cache,
        )
        if evidence is None:
            continue
        ok, reason = evidence
        if not ok:
            return False, f"{name} {reason}"
    return True, f"every required check started after the base advance at {advanced.isoformat()}"


def _github_actions_run_id(check):
    """The workflow-run id for a GitHub Actions check, or None when not one."""
    url = check.get("detailsUrl")
    if not isinstance(url, str):
        return None
    match = GITHUB_ACTIONS_RUN_RE.search(url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _github_actions_run(run_id):
    """GitHub Actions workflow-run metadata, or None on any unreadable state."""
    if not isinstance(run_id, int) or run_id <= 0:
        return None
    slug = get_repo_slug()
    if not slug:
        return None
    return _gh_json(["gh", "api", f"repos/{slug}/actions/runs/{run_id}"])


def _github_actions_run_timestamps(data, advanced):
    """Workflow-run creation/start stamps, validated against the base advance."""
    created = _utc_stamp(data.get("created_at"))
    started = _utc_stamp(data.get("run_started_at"))
    if created is None or started is None:
        return False, (
            "does not report usable event-time timestamps for created_at and "
            "run_started_at"
        )
    if started < created:
        return False, (
            "reports event-time timestamps out of order: "
            f"created_at {created.isoformat()}, run_started_at {started.isoformat()}"
        )
    if created <= advanced or started <= advanced:
        return False, (
            f"was created at {created.isoformat()} and started at "
            f"{started.isoformat()}, no later than the base advance at "
            f"{advanced.isoformat()}"
        )
    return True, ""


def _github_actions_current_base_evidence(pr, check, advanced, run_resolver=None,
                                          cache=None):
    """Current-base proof for one GitHub Actions check run, or None if not one.

    Plain workflow re-runs reuse the original event's `GITHUB_SHA` and
    `GITHUB_REF`, so a later `startedAt` is not enough to prove that a GitHub
    Actions run tested the current merge ref. For Actions runs, accept only
    genuine event-time metadata: the workflow run must be an attempt-1
    `pull_request` run for this PR's current head, and its own creation/start
    stamps must both land after the base advance.

    Deliberately do *not* read `pull_requests[].base/head` here. GitHub's REST
    workflow-run payload live-resolves those nested PR objects; historical run
    32576962919 in this repository reports event-time `head_sha` 5b727c16...,
    while its nested `pull_requests[0].head.sha` now resolves to a later head.
    Data that drifts with the current PR cannot prove what happened at run time.
    """
    run_id = _github_actions_run_id(check)
    if run_id is None:
        return None
    resolve = run_resolver or _github_actions_run
    store = cache if isinstance(cache, dict) else {}
    if run_id not in store:
        store[run_id] = resolve(run_id)
    data = store[run_id]
    if not isinstance(data, dict):
        return False, "comes from a GitHub Actions run whose event-time metadata could not be verified"
    event = data.get("event")
    if event != "pull_request":
        shown = str(event or "unknown")
        return False, (
            f"comes from a GitHub Actions run triggered by {shown!r} rather than "
            "a pull_request event"
        )
    attempt = data.get("run_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        return False, "comes from a GitHub Actions run whose run_attempt is unreadable"
    if attempt != 1:
        return False, (
            f"comes from GitHub Actions run attempt {attempt}, and GitHub "
            "re-runs preserve the original pull_request event SHA/ref"
        )
    current_head = pr.get("headRefOid")
    if not isinstance(current_head, str) or not current_head:
        return False, "comes from a GitHub Actions run whose event-time metadata could not be verified"
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return False, (
            "comes from a GitHub Actions run that does not report a usable "
            "event-time head SHA"
        )
    if head_sha != current_head:
        return False, (
            f"records event-time head {head_sha} instead of the current head "
            f"{current_head}"
        )
    return _github_actions_run_timestamps(data, advanced)


# How many times the identity proof re-reads the base branch tip when the two
# reads bracketing the merge-parent lookup disagree. GitHub's REST reads are
# eventually consistent, so one disagreement can be replica lag rather than a
# real push; a base that is actually moving keeps disagreeing and is refused.
MERGE_PARENT_BASE_RECHECK_ATTEMPTS = 3


def _current_base_tip(pr):
    """The base branch's live tip SHA, or None if it cannot be read.

    `pr["baseRefOid"]` is a snapshot taken before the merge-parent lookup, so
    on its own it cannot witness a base that advanced since (#371 review).
    Reading the ref itself lets the identity proof bracket that lookup with two
    live reads and refuse when the base moves underneath it.

    Returns None for "could not determine" -- an unresolvable slug or base
    branch, an unreadable ref, or a ref payload without a non-empty string SHA.
    Every caller must treat None as unverifiable, never as a passing merge.
    """
    branch = pr.get("baseRefName")
    slug = get_repo_slug()
    if not branch or not slug:
        return None
    ref = _gh_json(["gh", "api", f"repos/{slug}/git/ref/heads/{branch}"])
    if not isinstance(ref, dict):
        return None
    obj = ref.get("object")
    if not isinstance(obj, dict):
        return None
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha:
        return None
    return sha


def _merge_commit_parents(pr):
    """Parent SHAs of the PR's current test-merge commit, or None if unusable.

    REST's `merge_commit_sha` is not GraphQL's `mergeCommit` -- the latter
    stays null until the PR is actually merged. For an open, mergeable PR,
    GitHub keeps `merge_commit_sha` pointed at a two-parent commit merging the
    current head onto the current base tip: the same `refs/pull/N/merge` ref
    every job in ci.yml checks out, since none overrides the checkout ref.

    This supplies the literal identity proof `_ci_saw_base_advance` cannot
    give on its own (issue #371): reading a commit's own parents names exactly
    what it merges, rather than inferring freshness from when a check started.

    Returns None for "could not determine" -- an unresolvable PR or slug, an
    absent or non-string `merge_commit_sha`, an unresolvable commit, or a
    commit that does not report exactly two parents with string SHAs. Every
    caller must treat None as unverifiable, never as a passing merge.
    """
    number = pr.get("number")
    slug = get_repo_slug()
    if not number or not slug:
        return None
    pull = _gh_json(["gh", "api", f"repos/{slug}/pulls/{number}"])
    if not isinstance(pull, dict):
        return None
    merge_sha = pull.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not merge_sha:
        return None
    commit = _gh_json(["gh", "api", f"repos/{slug}/commits/{merge_sha}"])
    if not isinstance(commit, dict):
        return None
    parents = commit.get("parents")
    if not isinstance(parents, list) or len(parents) != 2:
        return None
    shas = []
    for entry in parents:
        if not isinstance(entry, dict):
            return None
        sha = entry.get("sha")
        if not isinstance(sha, str) or not sha:
            return None
        shas.append(sha)
    return shas


def check_rebased(pr, behind_resolver=None, paths_resolver=None, advance_resolver=None,
                  run_resolver=None, merge_parents_resolver=None,
                  base_tip_resolver=None):
    """Staleness gate.

    A branch behind the base is not automatically stale. Requiring a literal
    zero-behind branch deadlocks the factory, because a rebase rewrites the head
    SHA and the review gate binds its attestation to an exact SHA -- so rebasing
    destroys the review evidence of the very PR it was run on, and every merge
    forces every other open PR to do it (issue #369).

    Be precise about what this rule buys, because it is narrower than "the
    evidence describes the merged content". It establishes that no file was
    changed on both sides, so the merge is textually non-interfering. It does
    NOT establish semantic independence: the base can change a signature in one
    file while this branch changes a caller in another, and the paths stay
    disjoint.

    What bounds that residual risk is that CI here runs on `refs/pull/N/merge`,
    a genuine two-parent merge commit -- no job in ci.yml overrides the checkout
    ref -- so a green check already describes a merged tree rather than this
    branch alone.

    That only holds for a merge the checks actually saw, which is why accepting
    a behind branch requires `_ci_saw_base_advance` as well as disjointness
    (issue #371). GitHub triggers no new run when the base moves, so without
    that second proof a PR that went green and *then* fell behind would merge on
    checks computed against the superseded base -- the disjointness rule would
    be reading evidence about a tree nobody built. A plain GitHub Actions re-run
    is not enough, because GitHub replays the original event SHA/ref. The
    preserving-head remedy when this refuses is a fresh `pull_request` event on
    the same head, not a rebase.

    Timing alone still cannot name the exact commit a check tested, so once
    `_ci_saw_base_advance` is satisfied this also reads the PR's own
    test-merge commit and requires its parents to be literally the current
    base tip and this head (`_merge_commit_parents`, issue #371). That closes
    the identity gap timing leaves open -- including GitHub's own merge-ref
    recomputation lag -- without ever asking for a rebase either.
    """
    state = (pr.get("mergeStateStatus") or "").upper()
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
        try:
            overlap = _overlap_with_base_advance(pr, paths_resolver)
        except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
            return False, (
                f"Branch is {behind} {plural} behind the base and the overlap with "
                f"the base advance could not be determined "
                f"({type(exc).__name__}: {exc}). Rebase on main and re-run."
            )
        if overlap is None:
            return False, (
                f"Branch is {behind} {plural} behind the base and its changed-file "
                f"data is unavailable or truncated, so the overlap with the base "
                f"advance is unverified. Rebase on main and re-run."
            )
        if overlap:
            shown = ", ".join(overlap[:3])
            more = f" (+{len(overlap) - 3} more)" if len(overlap) > 3 else ""
            return False, (
                f"Branch is {behind} {plural} behind the base and both changed "
                f"{shown}{more}. Rebase on main and re-run."
            )
        # Disjointness is necessary but not sufficient: it says nothing about
        # whether the recorded CI ever saw this advance. Prove that too, last,
        # so a branch rejected above costs no extra API call.
        try:
            fresh, reason = _ci_saw_base_advance(pr, advance_resolver, run_resolver)
        except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
            fresh, reason = False, (
                f"the age of the base advance could not be determined "
                f"({type(exc).__name__}: {exc})"
            )
        if not fresh:
            return False, (
                f"Branch is {behind} {plural} behind the base with disjoint "
                f"changes, but {reason}. Trigger a fresh pull_request event on "
                f"this head (for example close and reopen the PR) so GitHub "
                f"recomputes the pull-request merge ref against the current "
                f"base. A small merge-ref lag window remains, because the run "
                f"metadata still does not identify the exact merge commit the "
                f"runner checked out. Do not rebase: that rewrites the head "
                f"SHA and destroys the review attestation bound to it."
            )
        # Timing proves the checks started late enough to have read the
        # advance; it does not name the commit they actually tested (issue
        # #371). Read the PR's own test-merge commit and confirm its parents
        # are literally the current base tip and this head -- the identity
        # proof timing alone cannot supply.
        identity_ok, identity_reason = _merge_commit_matches_base(
            pr, behind, plural, merge_parents_resolver, base_tip_resolver)
        if not identity_ok:
            return False, identity_reason
        return True, (
            f"Branch is {behind} {plural} behind the base, but its changes are "
            f"disjoint from the base advance, {reason}, and its tested merge "
            f"commit names the current base tip as a parent."
        )
    return True, "Branch is current with the base."


def _identity_refusal(behind, plural, detail):
    """Composes one refusal from the merge-parent identity proof.

    Every refusal on this path states the same three things -- what was already
    proven, what could not be proven, and that the remedy is never a rebase --
    so the shared wording is built once here instead of being restated at each
    exit. `detail` supplies only the middle part and must end its own sentence.
    A rebase is always the wrong remedy: it rewrites the head SHA and destroys
    the head-bound review attestation this gate exists to preserve (#369).
    """
    return (
        f"Branch is {behind} {plural} behind the base with disjoint "
        f"changes and CI that started after the advance, but {detail} Do "
        f"not rebase: that rewrites the head SHA and destroys the review "
        f"attestation bound to it."
    )


def _well_formed_parents(parents):
    """True only for a list of exactly two non-empty `str` parent SHAs.

    The resolver is injectable and its live implementation reads JSON GitHub
    produced, so `parents` is untrusted input: it may be any object at all.
    Checking the shape before the set comparison and before formatting keeps a
    malformed value from raising `TypeError` (unhashable entries in `set()`,
    non-`str` entries in `", ".join`) out of the fail-closed handlers, which
    would crash the gate instead of refusing the merge (#371 review).

    `type(x) is not str` rather than `isinstance` on purpose: a `str` subclass
    can override `__eq__`/`__hash__` and compare equal to a SHA it does not
    contain, which is precisely the deception this proof must not accept.
    """
    return (
        isinstance(parents, list)
        and len(parents) == 2
        and all(type(parent) is str and parent for parent in parents)
    )


def _show_parents(parents):
    """A display form for `parents` that is safe for any object.

    Only joins when every entry is genuinely a `str`; anything else is shown
    through `repr`, so a malformed resolver result can still be reported to the
    operator without the message construction itself raising.
    """
    if isinstance(parents, list) and all(type(p) is str for p in parents):
        return ", ".join(parents)
    return repr(parents)


def _proved_base_and_parents(pr, resolve_base, resolve_parents):
    """Reads the merge parents bracketed by two live reads of the base tip.

    Returns `(base_tip, parents, failure)`, where `failure` is None on success
    and otherwise a sentence naming why no proof could be taken.

    The bracketing is the whole point (#371 review). `pr["baseRefOid"]` is a
    snapshot taken before this lookup, so comparing the parents against it
    cannot witness a base that advanced in between: the stale merge commit
    still matches the stale snapshot and the gate passes, after which the merge
    itself runs against the newer base that CI never saw. Two live reads either
    side of the lookup make that movement observable, and a parent list read
    across a moving base is attributable to neither end of the move.

    Retries are bounded and never weaken the proof. GitHub's REST reads are
    eventually consistent, so a single disagreement between the two reads can
    be replica lag rather than a real push; a base that is genuinely moving
    keeps disagreeing and is refused once the attempts run out.
    """
    failure = None
    for _attempt in range(MERGE_PARENT_BASE_RECHECK_ATTEMPTS):
        before = resolve_base(pr)
        if not isinstance(before, str) or not before:
            return None, None, "the base branch tip could not be read."
        parents = resolve_parents(pr)
        after = resolve_base(pr)
        if not isinstance(after, str) or not after:
            return None, None, "the base branch tip could not be re-read."
        if before == after:
            return after, parents, None
        failure = (
            f"the base branch kept advancing (last seen moving from "
            f"{before[:12]} to {after[:12]}) while the tested merge commit "
            f"was being read, so the merge commit's parents cannot be "
            f"attributed to any one base tip. Wait for the base to settle "
            f"and retry."
        )
    return None, None, failure


def _merge_commit_matches_base(pr, behind, plural, merge_parents_resolver,
                               base_tip_resolver=None):
    """The literal identity proof `check_rebased` needs for a behind branch.

    Confirms the PR's test-merge commit is a genuine two-parent merge of the
    current base tip and this head, per issue #371. Split out of
    `check_rebased` to keep its branch count within the complexity ceiling.

    The base tip used for that comparison is re-read live around the parent
    lookup rather than taken from the PR snapshot, and the snapshot must still
    agree with it, so a base that advanced after the snapshot is refused
    instead of being merged into on evidence that predates it (#371 review).
    """
    snapshot_base = pr.get("baseRefOid")
    head_sha = pr.get("headRefOid")
    resolve_parents = merge_parents_resolver or _merge_commit_parents
    resolve_base = base_tip_resolver or _current_base_tip
    if not snapshot_base or not head_sha:
        return False, _identity_refusal(behind, plural, (
            "the current base tip or head SHA is unknown, so the tested "
            "merge commit's parents cannot be verified. Refusing to merge "
            "on unverified evidence."
        ))
    try:
        base_tip, parents, failure = _proved_base_and_parents(
            pr, resolve_base, resolve_parents)
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        return False, _identity_refusal(behind, plural, (
            f"the pull request's tested merge commit could not be resolved "
            f"({type(exc).__name__}: {exc}). Wait for GitHub to finish "
            f"computing the merge ref and retry."
        ))
    if failure:
        return False, _identity_refusal(behind, plural, failure)
    # The rest of the gate -- disjointness and the CI-timing proof -- was all
    # computed against the snapshot base. If the live base is no longer that
    # commit, every one of those findings describes a base that no longer
    # exists, so the parents matching the live tip would still not make the
    # merge safe. Refuse and let the caller re-read the PR.
    if base_tip != snapshot_base:
        return False, _identity_refusal(behind, plural, (
            f"the base branch advanced from {snapshot_base[:12]} to "
            f"{base_tip[:12]} after this pull request was read, so the "
            f"disjointness and CI-freshness evidence describes a superseded "
            f"base. Re-run the merge so every check is taken against the "
            f"current base tip."
        ))
    if parents is None:
        return False, _identity_refusal(behind, plural, (
            "the pull request's tested merge commit could not be resolved. "
            "Wait for GitHub to finish computing the merge ref and retry."
        ))
    # A set comparison alone would let a malformed, duplicate-padded list
    # like [base_tip, base_tip, head_sha] slip through as a clean match,
    # since sets discard the extra copy -- require the shape too. The shape
    # check must come first: `set()` on unhashable entries and `", ".join` on
    # non-string ones both raise TypeError outside the handlers above.
    if not _well_formed_parents(parents) or set(parents) != {base_tip, head_sha}:
        return False, _identity_refusal(behind, plural, (
            f"the pull request's tested merge commit does not name exactly "
            f"the current base tip {base_tip[:12]} and head {head_sha[:12]} "
            f"as its parents (got: {_show_parents(parents)}). GitHub may "
            f"still be recomputing the merge ref against the latest base. "
            f"Wait for it to finish and retry."
        ))
    return True, "tested merge commit names the current base tip as a parent"


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


def _base_tip_unchanged(pr, expected_base, base_tip_resolver=None):
    """Fail-closed proof that the base is still the tip the gates were taken against.

    Neither merge API GitHub exposes has a base-side precondition, so there is
    no server-side compare-and-swap to ask for. GraphQL's `mergePullRequest`
    input accepts only `expectedHeadOid` -- "OID that the pull request head ref
    must match to allow merge" -- and REST's
    `PUT /repos/{slug}/pulls/{n}/merge` only the equivalent `sha`, which is
    exactly what `gh pr merge --match-head-commit` sends. The head is pinned;
    the base is whatever the server holds when it runs the merge.

    The intervening base move therefore has to be blocked here instead (#371
    review). Every gate -- path disjointness, CI freshness, and the tested
    merge commit's parents -- was computed against one base tip. If the base
    advanced after that, GitHub would merge the reviewed head into a tree no
    check ever built, so refuse rather than treat a non-atomic proof as though
    it still described the merge the server is about to perform.

    Returns `(ok, detail)`. Anything unreadable, absent, or moved is not ok:
    the caller must never merge on a base it could not re-prove.
    """
    if not isinstance(expected_base, str) or not expected_base:
        return False, (
            "no proved base tip was supplied to pin the merge against, so an "
            "advance since the gates ran could not be ruled out."
        )
    resolve = base_tip_resolver or _current_base_tip
    try:
        live = resolve(pr)
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        return False, (
            f"the base branch tip could not be re-read immediately before the "
            f"merge ({type(exc).__name__}: {exc})."
        )
    if not isinstance(live, str) or not live:
        return False, (
            "the base branch tip could not be re-read immediately before the "
            "merge."
        )
    if live != expected_base:
        return False, (
            f"the base branch advanced from {expected_base[:12]} to "
            f"{live[:12]} after the final gate reread, so the merge would "
            f"combine the reviewed head with a base tip no check ever tested. "
            f"Re-run the merge so every gate is taken against the current base "
            f"tip. Do not rebase: that rewrites the head SHA and destroys the "
            f"review attestation bound to it."
        )
    return True, f"base tip still {live[:12]}"


def execute_merge(pr_id, pr, merge_method, expected_base, base_tip_resolver=None):
    """Runs only the server-side merge, then re-reads authoritative PR state.

    The merge command deliberately does not delete either branch. Cleanup is a
    separate, resumable phase. A non-zero command may still mean GitHub merged
    successfully, so the return code is never interpreted without a re-read.

    `expected_base` is the base tip every gate was proved against. It is
    re-proved live here, as the last action before the merge command rather
    than at the caller, because the command can pin only the head and anything
    interposed between the proof and the command reopens the window it closes
    (`_base_tip_unchanged`, #371 review). A missing expectation is itself a
    refusal: unpinned means unverified.
    """
    unchanged, base_detail = _base_tip_unchanged(pr, expected_base, base_tip_resolver)
    if not unchanged:
        return None, f"{MERGE_NOT_ATTEMPTED} {base_detail}"

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
    list_code, porcelain, list_err = run_cmd(
        ["git", "worktree", "list", "--porcelain"],
        check=False, cwd=repo_root,
    )
    if list_code != 0:
        return False, (
            f"Could not enumerate worktrees to prove {branch} is unattached: "
            f"{list_err.strip()}"
        )
    if find_branch_worktree(porcelain, branch)[0]:
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
    return False, (
        f"Orphan local branch {branch} deletion failed after unattached validation: "
        f"{err.strip()}"
    )


def is_harmless_orphan_branch_failure(failure: str) -> bool:
    """True only for bounded fallback-safe orphan local-branch deletion failures."""
    prefix = "local branch: Orphan local branch "
    return failure.startswith(prefix) and "unattached validation" in failure


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


def evaluate_dod(pr, issue_bodies, evidence, behind_resolver=None,
                 paths_resolver=None, advance_resolver=None,
                 merge_parents_resolver=None, base_tip_resolver=None):
    """Runs every Definition-of-Done check without merging.

    Returns ``(ok, gates)`` where ``gates`` is a list of
    ``(name, passed, message)`` in evaluation order. Shared by ``--dry-run``
    and the merge work picker so eligibility cannot drift from the gate.

    ``behind_resolver``, ``paths_resolver``, ``advance_resolver``,
    ``merge_parents_resolver`` and ``base_tip_resolver`` are threaded to
    :func:`check_rebased` so a caller with no repository to interrogate -- a
    hermetic fleet simulation -- can state ancestry, changed paths, when the
    base advanced, the tested merge commit's parents, and the live base tip
    directly. Production callers omit all five and get the fail-closed
    git/GitHub path, which is the point of the gate.
    """
    issue_nums = linked_issues(pr.get("body"))
    gates = [
        ("open", *check_open(pr)),
        ("issue link", *check_issue_link(pr)),
        ("verification", *check_verification(pr)),
        ("ci", *check_ci(pr)),
        ("review", *check_reviews(pr, evidence)),
        ("rebased", *check_rebased(pr, behind_resolver, paths_resolver, advance_resolver,
                                   merge_parents_resolver=merge_parents_resolver,
                                   base_tip_resolver=base_tip_resolver)),
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
    evidence = _with_coderabbit_status(pr_id, review_evidence(pr_id))
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
    if last_failures and all(
        is_harmless_orphan_branch_failure(item) for item in last_failures
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

        evidence = _with_coderabbit_status(args.pr, review_evidence(args.pr))
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
            # A base that advanced between the initial DoD gate and this lock
            # is not itself disqualifying (issue #371): check_rebased
            # re-derives freshness -- disjointness, CI timing, and the tested
            # merge commit's parents -- against whatever the base is right
            # now. Blocking here unconditionally would have to tell the
            # operator to rebase, which destroys the head-bound review
            # attestation this gate exists to protect.
            rebased, rebased_message = check_rebased(fresh)
            if not rebased:
                print(
                    f"[ERROR] Final rebased check failed: {rebased_message}",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            # Review, status, and thread evidence can change without moving the
            # head. Re-read it under the merge lock immediately before the
            # server-side mutation, then rerun every gate that consumes it.
            final_evidence = _with_coderabbit_status(
                args.pr, review_evidence(args.pr)
            )
            final_head = final_evidence.get("head_oid") if final_evidence else None
            if not heads_match(live, final_head):
                print(
                    "[ERROR] Final review evidence is unavailable or stale. "
                    "No merge command was run.",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            final_ok, final_gates = evaluate_dod(fresh, issue_bodies, final_evidence)
            if not final_ok:
                final_blocked = ", ".join(
                    name for name, passed, _ in final_gates if not passed
                )
                print(
                    f"[ERROR] Final Definition-of-Done reread failed: {final_blocked}. "
                    "No merge command was run.",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
            pr = fresh
            # Every gate above -- disjointness, CI freshness, the tested merge
            # commit's parents, review evidence -- was computed against this
            # exact base tip. execute_merge re-proves it is still live right
            # before the server merge, because `gh pr merge` pins only the head
            # and GitHub would otherwise merge into whatever base it holds at
            # execution time (#371 review).
            gated_base = fresh.get("baseRefOid")

            print(f"  ✅ merge lock          {lock_message}")
            print(f"  ✅ final base check    {rebased_message}")
            print("\n=== Merge execution ===")
            final_pr, outcome = execute_merge(
                args.pr, pr, args.merge_method, gated_base
            )
            if not final_pr:
                print(f"  ❌ not merged          {outcome}", file=sys.stderr)
                # A refusal taken before the command ran is a gate block, not a
                # failed merge: nothing was mutated and re-running is the remedy.
                if outcome.startswith(MERGE_NOT_ATTEMPTED):
                    return EXIT_BLOCKED
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
