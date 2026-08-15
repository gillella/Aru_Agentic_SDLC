#!/usr/bin/env python3
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
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime

from common import (
    VERIFICATION_EVIDENCE_END,
    VERIFICATION_EVIDENCE_SCHEMA,
    VERIFICATION_EVIDENCE_START,
    get_repo_slug,
    run_cmd,
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
REVIEWED_BY_LABEL = "reviewed-by:"
# The transient claim, written by claim_review. Deliberately NOT accepted here:
# it records that an agent took the PR off the queue, not that it read anything.
# Treating it as attestation would let an author's own same-account review plus
# any peer's claim satisfy the gate before that peer had looked at the diff.
REVIEW_CLAIM_LABEL = "reviewer:"
# Transient merge-execution claim from claim_merge. Cleared on close-out; never
# treated as review attestation.
MERGER_CLAIM_LABEL = "merger:"

# Review apps can add useful findings, but their comments are not independent
# approval. GitHub exposes some bot logins with a ``[bot]`` suffix and the
# Codex connector without one, so both forms must be recognized explicitly.
ADVISORY_REVIEW_ACCOUNTS = {"chatgpt-codex-connector"}

# Large diffs remain visible in the audit output. The separate independent-
# review gate, not a blanket human-review assertion, owns review quality.
SIZE_SOFT_LIMIT = 400

PR_FIELDS = (
    "number,title,body,state,isDraft,mergeable,mergeStateStatus,baseRefName,author,"
    "headRefName,headRefOid,additions,deletions,reviews,statusCheckRollup,labels,"
    "mergedAt,mergeCommit,headRepository,headRepositoryOwner,isCrossRepository"
)


def _gh_json(args):
    """Runs a gh command expected to emit JSON. Returns None on any failure."""
    code, out, err = run_cmd(args, check=False)
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


def _parse_ts(value):
    """ISO-8601 from the GitHub API to a comparable datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def review_evidence(pr_id):
    """Facts the review gate needs beyond a count of open threads.

    Returns ``None`` on any query failure - an unknown review state must never
    merge - otherwise a dict:

      ``unresolved``  open, non-outdated threads.
      ``unfixed``     threads resolved with no commit after the finding was
                      raised and no explicit withdrawal. This is the shape that
                      let PR #62 merge with five blocking findings intact:
                      resolving a thread is a UI toggle and proves nothing
                      about the code.
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
    query = """
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          headRefOid
          reviews(first:100) {
            nodes { state author { login } commit { oid } }
          }
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
    commit_times = None
    reviewed_head = False

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

        # Commits and reviews do not change between thread pages; read once.
        if commit_times is None:
            head = pull.get("headRefOid")
            try:
                commit_times = sorted(
                    ts for ts in (
                        _parse_ts(((c or {}).get("commit") or {}).get("committedDate"))
                        for c in (pull.get("commits") or {}).get("nodes") or []
                    ) if ts is not None
                )
            except (AttributeError, TypeError):
                return None
            for review in (pull.get("reviews") or {}).get("nodes") or []:
                if (review.get("state") or "").upper() == "PENDING":
                    continue
                who = ((review.get("author") or {}).get("login") or "")
                if is_advisory_review_account(who):
                    continue
                if head and ((review.get("commit") or {}).get("oid")) == head:
                    reviewed_head = True

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
                if not has_commit_after:
                    unfixed += 1

        if not has_next:
            return {
                "unresolved": unresolved,
                "unfixed": unfixed,
                "outdated_unfixed": outdated_unfixed,
                "outdated_addressed": outdated_addressed,
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
    """
    if not issue_body:
        return []
    section = re.split(
        r"^\s*#{1,4}\s*acceptance criteria\s*$", issue_body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(section) < 2:
        return []
    # Stop at the next heading.
    tail = re.split(r"^\s*#{1,4}\s+", section[1], flags=re.MULTILINE)[0]
    return [
        line.strip()
        for line in tail.splitlines()
        if re.match(r"^\s*[-*]\s*\[\s\]", line)
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


def check_ci(pr):
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return False, "No CI checks reported on the head commit. A PR with no checks is not verified."
    # Allowlist, not denylist. Enumerating the failure conclusions let unknown
    # ones - STARTUP_FAILURE, STALE, anything GitHub adds later - fall through
    # to "green" and merge an unverified head. Only these three mean "passed";
    # every other completed conclusion fails closed.
    passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    in_progress = {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}

    failing, pending = [], []
    for check in rollup:
        # Check runs use 'conclusion'; legacy statuses use 'state'.
        status = (check.get("status") or "").upper()
        result = (check.get("conclusion") or check.get("state") or "").upper()
        name = check.get("name") or check.get("context") or "check"
        if status and status != "COMPLETED" and not result or result in in_progress:
            pending.append(name)
        elif result not in passing:
            failing.append(f"{name}={result.lower() or 'unknown'}")
    if failing:
        return False, f"CI is red: {', '.join(failing)}."
    if pending:
        return False, f"CI has not finished: {', '.join(pending)}."
    return True, f"CI green ({len(rollup)} checks)."


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
        who = ((review.get("author") or {}).get("login")
               or review.get("id") or "unknown")
        latest[who] = (review.get("submittedAt") or "", state)
    return {who: state for who, (_, state) in latest.items()}


def is_advisory_review_account(login):
    """Whether a GitHub reviewer identity belongs to review automation."""
    normalized = (login or "").lower()
    return normalized.endswith("[bot]") or normalized in ADVISORY_REVIEW_ACCOUNTS


def _evidence_note(evidence):
    """What actually satisfied the review gate, for the audit line.

    A later reader needs to tell "every finding was fixed" from "the findings
    were withdrawn", because those justify a merge very differently.
    """
    out_addressed = evidence.get("outdated_addressed", 0) if evidence else 0
    if out_addressed > 0:
        parts = [
            "reviewed at head",
            f"no blocking unresolved threads ({out_addressed} outdated with commit evidence)",
        ]
    else:
        parts = ["reviewed at head", "no unresolved threads"]
    if evidence and evidence.get("withdrawn"):
        parts.append(f"{evidence['withdrawn']} finding(s) withdrawn, not fixed")
    return ", ".join(parts) + "."


def check_reviews(pr, evidence):
    reviews = pr.get("reviews") or []
    substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
    if not substantive:
        return False, "No review on this PR. At least one review is required."
    verdicts = latest_state_per_reviewer(reviews)
    blocking = [
        who for who, state in verdicts.items()
        if state == "CHANGES_REQUESTED"
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
            f"{evidence['unfixed']} resolved thread(s) have no commit after the "
            "finding was raised and were not withdrawn, so nothing shows the "
            "finding was addressed. Push the fix, or reply to the thread "
            "starting with 'Withdrawn:' and why."
        )

    # A review attests to the commit it was submitted against. Once head moves
    # the attestation covers code that is no longer proposed, which otherwise
    # lets a reviewed PR be force-pushed and merged on the stale verdict.
    if not evidence["reviewed_head"]:
        return False, (
            "Every review predates the current head, so no reviewer has seen "
            "what would merge. Re-review the current commit."
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
    # self-review, but only its latest APPROVED verdict counts. Review apps are
    # advisory: their comments and approvals can inform an agent review, but
    # cannot satisfy the independent-review gate themselves.
    pr_login = ((pr.get("author") or {}).get("login") or "").lower()
    other_accounts = sorted({
        ((r.get("author") or {}).get("login") or "").lower()
        for r in substantive
    } - {"", pr_login})

    # Authorship is required before any approval path can pass. Without the
    # governed author stamp, even a genuine external approval cannot prove the
    # PR did not bypass create_pr.py or establish who must be excluded from
    # same-account agent review.
    authors = label_values(pr, "author:")
    if not authors:
        return False, (
            "PR has no author:<id> label, so the gate cannot prove that the "
            "reviewer is independent. Create PRs with "
            "`scripts/create_pr.py --issue <n> --agent <id>`; stamp the verified "
            "author on a legacy PR before retrying."
        )
    author = authors[0]

    external_approvers = sorted(
        who for who, state in verdicts.items()
        if who.lower() in other_accounts
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
    reviewers = label_values(pr, REVIEWED_BY_LABEL)
    peers = [r for r in reviewers if r != author]
    if reviewers and not peers:
        return False, (f"The only review is from '{author}', who wrote this PR. "
                       "A self-review does not satisfy the gate.")
    if not reviewers:
        advisory = [a for a in other_accounts if is_advisory_review_account(a)]
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

    note = (
        f"{len(substantive)} review(s) from {', '.join(peers)}, "
        f"{_evidence_note(evidence)}"
    )
    if any((lab.get("name") or "") == "same-family-review" for lab in (pr.get("labels") or [])):
        note += " ⚠️  Same-family review: no cross-family agent was available."
    return True, note


def check_rebased(pr):
    state = (pr.get("mergeStateStatus") or "").upper()
    if state == "BEHIND":
        return False, "Branch is behind the base. Rebase on main and re-run."
    if state == "DIRTY":
        return False, "Branch has merge conflicts with the base."
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "Branch conflicts with the base."
    return True, "Branch is current with the base."


def check_issue_link(pr):
    issues = linked_issues(pr.get("body"))
    if not issues:
        return False, "PR body has no 'Closes #<issue>'. Every PR must close a tracked issue."
    return True, "Linked to " + ", ".join(f"#{i}" for i in issues) + "."


def parse_verification_evidence(body):
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


def check_verification(pr):
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


def check_acceptance(issue_num, issue_body):
    pending = unticked_criteria(issue_body)
    if pending:
        preview = "\n      ".join(pending[:5])
        more = f"\n      ... and {len(pending) - 5} more" if len(pending) > 5 else ""
        return False, (
            f"Issue #{issue_num} has {len(pending)} unticked acceptance criteria:\n"
            f"      {preview}{more}"
        )
    return True, f"All acceptance criteria on #{issue_num} are ticked."


def check_size(pr):
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if total > SIZE_SOFT_LIMIT:
        return True, (
            f"Diff is {total} lines, over the {SIZE_SOFT_LIMIT}-line soft limit. "
            "Independent review remains mandatory through the separate review gate."
        )
    return True, f"Diff is {total} lines."


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


def prune_worktree(repo_root, branch, expected_sha):
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
        if status:
            return False, (
                f"Worktree {path} has tracked, untracked, or ignored files; left untouched."
            )
        if os.path.exists(retained_path):
            return False, f"Retention destination already exists: {retained_path}"
        try:
            os.makedirs(retained_root, exist_ok=True)
            os.rename(path, retained_path)
        except OSError as exc:
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


def retain_local_branch(repo_root, branch, expected_sha):
    """Leaves the local ref intact because Git cannot lease worktree attachment.

    A compare-and-delete can protect the ref OID, but it cannot atomically stop
    another process from attaching a new worktree to that ref. Keeping the
    local branch is the only fail-closed behavior in a concurrent factory.
    """
    if not branch or not expected_sha:
        return False, "Branch and gated head SHA are required; local branch state is unknown."
    code, actual_sha, _ = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return True, "Local branch already absent."
    if actual_sha.strip() != expected_sha:
        return True, (
            f"Local branch {branch} was reused at {actual_sha.strip() or 'unknown'}; "
            "unrelated ref retained."
        )
    return True, (
        f"Retained local branch {branch}; Git cannot atomically lease worktree "
        "attachment during ref deletion."
    )


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


def clear_labels(kind, number, prefix):
    data = _gh_json(["gh", kind, "view", str(number), "--json", "labels"])
    if data is None:
        return False, f"Could not read {kind} #{number} labels."
    names = [
        label.get("name", "") for label in (data.get("labels") or [])
        if label.get("name", "").startswith(prefix)
    ]
    for name in names:
        code, _, err = run_cmd(
            ["gh", kind, "edit", str(number), "--remove-label", name], check=False
        )
        if code != 0:
            return False, f"Could not remove {name} from {kind} #{number}: {err.strip()}"
    noun = "claims" if names else "claim"
    return True, f"{kind.title()} #{number} {prefix}{noun} cleared or already absent."


def clear_issue_claims(issue_num):
    return clear_labels("issue", issue_num, "agent:")


def clear_review_claims(pr_num):
    return clear_labels("pr", pr_num, REVIEW_CLAIM_LABEL)


def clear_merger_claims(pr_num):
    return clear_labels("pr", pr_num, MERGER_CLAIM_LABEL)


def evaluate_dod(pr, issue_bodies, evidence):
    """Runs every Definition-of-Done check without merging.

    Returns ``(ok, gates)`` where ``gates`` is a list of
    ``(name, passed, message)`` in evaluation order. Shared by ``--dry-run``
    and the merge work picker so eligibility cannot drift from the gate.
    """
    issue_nums = linked_issues(pr.get("body"))
    gates = [
        ("open", *check_open(pr)),
        ("issue link", *check_issue_link(pr)),
        ("verification", *check_verification(pr)),
        ("ci", *check_ci(pr)),
        ("review", *check_reviews(pr, evidence)),
        ("rebased", *check_rebased(pr)),
        ("size", *check_size(pr)),
    ]
    for num in issue_nums:
        gates.append((f"accept #{num}", *check_acceptance(num, issue_bodies.get(num, ""))))
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
    ok, gates = evaluate_dod(pr, issue_bodies, evidence)
    if ok:
        return True, "every Definition-of-Done gate passed"
    blocked = [name for name, passed, _ in gates if not passed]
    return False, f"unmet: {', '.join(blocked)}"


def run_closeout(pr, issue_nums, repo_root):
    """Runs every idempotent close-out step, even after an earlier failure."""
    try:
        os.chdir(repo_root)
    except OSError as exc:
        print(f"\n=== Post-merge close-out ===\n  ❌ working directory  {exc}")
        return False
    branch = pr.get("headRefName") or ""
    expected_sha = pr.get("headRefOid") or ""
    head_repo_slug = head_repository_slug(pr)
    steps = [
        ("worktree", lambda: prune_worktree(repo_root, branch, expected_sha)),
        ("local branch", lambda: retain_local_branch(repo_root, branch, expected_sha)),
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
        all_ok = all_ok and ok

    # Keep merger:<id> until every prior step succeeds so the picker can still
    # rediscover incomplete close-out. Clearing it after a board/Done failure
    # would make recovery invisible once the linked issue is CLOSED.
    if all_ok:
        try:
            ok, message = clear_merger_claims(pr.get("number"))
        except Exception as exc:
            ok, message = False, f"Unexpected close-out error: {exc}"
        print(f"  {'✅' if ok else '❌'} {'merger claim':<18} {message}")
        all_ok = all_ok and ok
    else:
        print("  ⏳ merger claim      retained so recovery remains discoverable")
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


def main():
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
    args = parser.parse_args()

    if args.json and not args.dry_run:
        print("[ERROR] --json requires --dry-run (refusing to emit JSON for a live merge).", file=sys.stderr)
        return EXIT_ERROR

    pr = fetch_pr(args.pr)
    if not pr:
        return EXIT_ERROR

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

        evidence = review_evidence(args.pr)
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

        # Re-check head immediately before the merge command in case a push
        # landed between DoD evaluation and execution.
        fresh = fetch_pr(args.pr)
        if not fresh:
            return EXIT_ERROR
        live = fresh.get("headRefOid") or "unknown"
        if args.expected_head and not heads_match(live, args.expected_head):
            print(
                f"[ERROR] Head moved to {live} after DoD checks; expected "
                f"{args.expected_head}. No merge command was run.",
                file=sys.stderr,
            )
            return EXIT_BLOCKED
        pr = fresh
        gated_head = live

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
    if not root:
        print("[ERROR] Merge succeeded but repository root could not be resolved; rerun close-out.", file=sys.stderr)
        return EXIT_ERROR
    # Park the verdicts the moment we hold them, and read them back on a
    # resumed close-out. Re-deriving them post-merge is not an option:
    # check_open fails on a closed PR, so a re-evaluated block would record
    # failures that never happened.
    if gates is not None:
        save_gate_verdicts(root, args.pr, gates)
    else:
        gates = load_gate_verdicts(root, args.pr)

    closeout_ok = run_closeout(final_pr, issue_nums, root)
    if not closeout_ok or not audit_ok:
        print("\n❌ Merge is complete, but close-out is incomplete. Re-run this command to resume.")
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
