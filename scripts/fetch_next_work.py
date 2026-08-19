#!/usr/bin/env python3
"""fetch_next_work.py - answers "what should I do next?" for one agent.

The issue picker only ever answered "which issue do I implement?", so a fleet
of agents that all prefer fresh issues buries the board in unreviewed PRs.
Review is not a CI job here - agents run this loop under their own
subscriptions and no provider API keys exist - so review has to be work an
agent claims off the board like anything else.

Three work types, in strict priority order:

  1. feedback  - a PR I authored has requested changes or unresolved threads
  2. merge     - a PR whose Definition-of-Done gates already pass
  3. review    - an eligible PR is waiting for someone to review it
  4. issue     - nothing to finish, so start something new

Finishing beats starting. That ordering is the whole point: it is what stops
the review queue growing faster than it drains, and what carries independently
reviewed work through gated merge without a human pressing the button.

  python3 fetch_next_work.py --agent agent-1 --json
  python3 fetch_next_work.py --agent agent-1 --family anthropic --claim

Review eligibility:

  | rule                          | hard? |
  |-------------------------------|-------|
  | nobody else holds reviewer:*  | hard  |
  | author:<id> is not me         | hard  |
  | family:<f> is not mine        | soft  |
  | CI red                        | hard  |
  | not a draft                   | hard  |

CI pending or absent does not block a review claim. Pickup latency is the
queue, and the reviewer already re-runs tests in a worktree. Merge still
requires green CI. A red check still refuses review so the author fixes
first.

The family rule must be soft. An all-Claude fleet with a hard rule has zero
eligible reviewers, nothing gets reviewed, and merge_pr.py blocks everything -
a deadlock. After a PR has waited past the threshold, any *different agent* may
review it and the PR is labelled `same-family-review` so the degradation shows.

A different agent is worth a great deal on its own: a fresh session has no
memory of writing the code and no attachment to its choices. A different family
adds diverse blind spots on top of that; it is not the whole value.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_issue import (
    EXIT_CONFLICT,
    EXIT_OK,
    claim_merge,
    claim_review,
    merge_claimant,
    reap_stale_merges,
    reap_stale_reviews,
    reviewed_by,
)
from common import (
    get_repo_slug,
    list_open_issues,
    run_cmd,
    label_names as issue_label_names,
)
from fetch_next_issue import (
    attach_open_pr_file_snapshots,
    build_candidates,
    pr_files_by_issue_from_prs,
    reap_stale_claims,
)
from fetch_pr_feedback import fetch_active_review_feedback
from merge_pr import closeout_incomplete, dod_status, is_merged, linked_issues
# _attested_head_peers is private, and importing it across modules is normally a
# smell. It is imported deliberately: merge_pr is the single source of truth for
# whether a peer's completion stamp names the current head, and a second
# implementation of that predicate in the picker is exactly the drift that let
# reviewed-but-since-pushed PRs reach no agent at all.
from merge_pr import _attested_head_peers, review_evidence


def skill_for_issue(issue: dict[str, Any]) -> str:
    """Route type:research / research: titles to the research skill."""
    labels = set(issue_label_names(issue))
    title = str(issue.get("title") or "").strip().lower()
    if "type:research" in labels or title.startswith("research:"):
        return "research"
    return "implement-next-issue"

# Retained as a backwards-compatible CLI default. Review count is audit data,
# never an eligibility or human-intervention gate.
DEFAULT_ROUND_CAP = 3

# How long a PR waits for a cross-family reviewer before any different agent
# may take it. Long enough that a mixed fleet routes correctly; short enough
# that a single-family fleet is never stuck.
DEFAULT_CROSS_FAMILY_WAIT_MIN = 30

PR_FIELDS = ("number,title,isDraft,labels,reviews,statusCheckRollup,updatedAt,"
             "createdAt,headRefName,headRefOid,body,reviewDecision,state,mergedAt,"
             "files,changedFiles")


def _label_value(labels: list[str], prefix: str) -> str | None:
    for name in labels:
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def list_open_prs() -> list[dict[str, Any]] | None:
    code, out, err = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", PR_FIELDS],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not list PRs: {err.strip()}", file=sys.stderr)
        return None
    try:
        prs = json.loads(out) if out else []
    except json.JSONDecodeError:
        print("[WARN] Could not parse the PR list.", file=sys.stderr)
        return None
    if not isinstance(prs, list):
        return None
    return attach_open_pr_file_snapshots(prs)


def _issue_closeout_snapshot(slug: str) -> dict[int, dict[str, Any]] | None:
    """Return all issue state needed for one merged-PR recovery scan.

    The old path called ``closeout_incomplete`` for every historical merge,
    which issued one ``gh issue view`` per linked issue.  A single paginated
    REST snapshot keeps the same authority while making picker cost depend on
    API pages rather than repository history.  Any malformed or ambiguous page
    fails closed so the picker cannot start work from a partial view.
    """
    code, out, err = run_cmd(
        [
            "gh", "api", "--paginate",
            f"repos/{slug}/issues?state=all&per_page=100",
            "--jq", ".[] | select(.pull_request == null) | {number,state,labels}",
        ],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not snapshot issue close-out state: {err.strip()}",
              file=sys.stderr)
        return None

    snapshot: dict[int, dict[str, Any]] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            issue = json.loads(line)
        except json.JSONDecodeError:
            print("[WARN] Could not parse issue close-out snapshot.", file=sys.stderr)
            return None
        if not isinstance(issue, dict):
            return None
        number = issue.get("number")
        state = issue.get("state")
        labels = issue.get("labels")
        if (not isinstance(number, int)
                or not isinstance(state, str)
                or state.upper() not in {"OPEN", "CLOSED"}
                or not isinstance(labels, list)
                or any(
                    not isinstance(label, dict)
                    or not isinstance(label.get("name"), str)
                    for label in labels
                )
                or number in snapshot):
            print("[WARN] Issue close-out snapshot has invalid or duplicate data.",
                  file=sys.stderr)
            return None
        snapshot[number] = {"state": state.upper(), "labels": labels}
    return snapshot


def _snapshot_closeout_incomplete(
    pr: dict[str, Any], issue_snapshot: dict[int, dict[str, Any]]
) -> bool | None:
    """Mirror ``merge_pr.closeout_incomplete`` against a cycle snapshot."""
    labels = label_names(pr)
    if any(name.startswith("merger:") for name in labels):
        return True
    for number in linked_issues(pr.get("body")):
        issue = issue_snapshot.get(number)
        if issue is None:
            return None
        if issue["state"] == "OPEN":
            return True
        issue_labels = {label["name"] for label in issue["labels"]}
        if "status:done" not in issue_labels:
            return True
    return False


def list_merged_needing_closeout() -> list[dict[str, Any]] | None:
    """Merged PRs whose close-out still needs ``merge_pr.py``.

    Paginate until exhausted so an incomplete close-out older than the newest
    fifty merges remains discoverable. Fail closed when any page cannot be
    read: otherwise a crashed close-out becomes invisible.
    """
    slug = get_repo_slug()
    if not slug:
        print("[WARN] Could not resolve repo slug for merged PR recovery.", file=sys.stderr)
        return None
    code, out, err = run_cmd(
        [
            "gh", "api", "--paginate",
            f"repos/{slug}/pulls?state=closed&per_page=100&sort=updated&direction=desc",
            "--jq", ".[]",
        ],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not list closed PRs: {err.strip()}", file=sys.stderr)
        return None
    closed = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            closed.append(json.loads(line))
        except json.JSONDecodeError:
            print("[WARN] Could not parse closed PR list.", file=sys.stderr)
            return None

    merged = [item for item in closed if item.get("merged_at")]
    if not merged:
        return []
    issue_snapshot = _issue_closeout_snapshot(slug)
    if issue_snapshot is None:
        return None

    recovery = []
    for item in merged:
        labels = [{"name": lab.get("name", "")} for lab in (item.get("labels") or [])]
        pr = {
            "number": item.get("number"),
            "title": item.get("title") or "",
            "isDraft": bool(item.get("draft")),
            "labels": labels,
            "reviews": [],
            "statusCheckRollup": [],
            "updatedAt": item.get("updated_at"),
            "createdAt": item.get("created_at"),
            "headRefName": ((item.get("head") or {}).get("ref")) or "",
            "headRefOid": ((item.get("head") or {}).get("sha")) or "",
            "body": item.get("body") or "",
            "reviewDecision": "",
            "state": "MERGED",
            "mergedAt": item.get("merged_at"),
            "_active_review_feedback": [],
        }
        incomplete = _snapshot_closeout_incomplete(pr, issue_snapshot)
        if incomplete is None:
            print(
                f"[WARN] Linked issue state for merged PR #{pr['number']} "
                "is absent from the authoritative snapshot.",
                file=sys.stderr,
            )
            return None
        if incomplete:
            recovery.append(pr)
    return recovery


def list_work_prs() -> list[dict[str, Any]] | None:
    """Open PRs plus merged PRs that still need close-out recovery."""
    open_prs = list_open_prs()
    if open_prs is None:
        return None
    recovery = list_merged_needing_closeout()
    if recovery is None:
        return None
    return open_prs + recovery


def label_names(pr: dict[str, Any]) -> list[str]:
    return [lab.get("name", "") for lab in pr.get("labels", [])]


def review_thread_count(pr: dict[str, Any]) -> int | None:
    """Returns and caches the live active-feedback count for one selection.

    A same-account blocking review is necessarily COMMENTED, so GitHub's
    reviewDecision cannot route it. Unresolved threads are the fail-closed
    author-feedback state, while zero threads plus reviewed-by attribution is
    the approval-equivalent completion state.
    """
    if "_active_review_feedback" not in pr:
        pr["_active_review_feedback"] = fetch_active_review_feedback(pr["number"])
    feedback = pr["_active_review_feedback"]
    return None if feedback is None else len(feedback)


def _authored_via_branch(pr: dict[str, Any], agent: str) -> bool:
    """Infers authorship from the linked issue's retained agent label.

    `create_pr.py` now requires --agent and fails loudly on a bad stamp, so new
    PRs opened through it always carry author:<id>. This covers what that cannot
    reach: PRs predating stamping, and PRs a human opened by hand with `gh`.
    Those leave no author on the PR. The branch still encodes
    the issue number, and an In Review issue retains the implementing agent's
    label as a legacy authorship backstop even though it no longer consumes an
    active implementation slot.
    """
    match = re.search(r"issue-(\d+)", pr.get("headRefName") or "", re.IGNORECASE)
    if not match:
        return False
    code, out, _ = run_cmd(
        ["gh", "issue", "view", match.group(1), "--json", "labels",
         "-q", "[.labels[].name] | join(\"\\n\")"],
        check=False,
    )
    if code != 0:
        return False
    return f"agent:{agent}" in [line.strip() for line in out.splitlines()]


def ci_state(pr: dict[str, Any]) -> str:
    """Returns 'green', 'red', 'pending', or 'none'.

    A PR with no checks at all is 'none', not 'green'. Merge still refuses
    an unverified head. Review may proceed on pending/none so pickup does
    not wait on CI; red still blocks.
    """
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "none"
    pending = False
    for check in rollup:
        status = (check.get("status") or "").upper()
        result = (check.get("conclusion") or check.get("state") or "").upper()
        if result in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
            return "red"
        if (status and status != "COMPLETED" and not result) or result in {
            "", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"
        }:
            pending = True
    return "pending" if pending else "green"


def waiting_minutes(pr: dict[str, Any]) -> float:
    stamp = pr.get("updatedAt") or pr.get("createdAt")
    try:
        ts = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def needs_my_attention(pr: dict[str, Any], agent: str) -> bool:
    """True when this is my PR and a reviewer has asked for something.

    Deliberately based on current thread state rather than reviewDecision.  A
    same-account COMMENTED review has no decision, while GitHub can retain an
    old CHANGES_REQUESTED decision after every actionable thread is resolved.
    A PR merely sitting unreviewed is someone else's work to review.
    """
    if _label_value(label_names(pr), "author:") != agent:
        return False
    threads = review_thread_count(pr)
    return threads is not None and threads > 0


# Definition-of-Done gates a PR's own author can clear alone. Work arising from
# these is offered only to author:<id>; the routed skill documents the concrete
# action for every name in this set.
# `review-evidence` is the unfixed-resolved-thread case: a peer already
# reviewed, threads are resolved, but no follow-up commit or `Withdrawn:`
# reply exists. Generic `review` (needs a peer) is never author-fixable.
AUTHOR_FIXABLE_GATES = frozenset({
    "accept", "ci", "rebased", "review-evidence", "size", "tests",
    "verification",
})
PEER_ROUTABLE_GATES = frozenset({"review"})

# These evaluate_dod names cannot create author work: open and issue-link
# failures are intercepted before gate evaluation, while review rounds is a
# visibility-only check that always passes. The exhaustiveness test requires a
# comment-backed entry here when evaluate_dod gains another deliberate non-route.
DOD_NON_ROUTABLE_GATES = frozenset({"open", "issue link", "review rounds"})
DETAIL_REQUIRED_GATES = frozenset({"tests", "verification"})


def _routable_gate_name(name: str) -> str:
    """Normalize parameterized DoD names to the action the author can take."""
    cleaned = name.strip()
    if re.fullmatch(r"accept\s+#\d+", cleaned, re.IGNORECASE):
        return "accept"
    return cleaned


def _unmet_gates(reason: str) -> set[str]:
    """Gate names from a ``dod_status`` reason, or empty when it is not one.

    ``dod_status`` reports gate failures as ``unmet: <name>, <name>`` but also
    returns prose for fetch failures, a missing ``Closes #<issue>``, and review
    evidence that does not match the head. Those are not gate lists, and
    treating them as one would invent work from an unknown state.
    """
    text = (reason or "").strip()
    prefix = "unmet:"
    if not text.lower().startswith(prefix):
        return set()
    return {
        _routable_gate_name(part)
        for part in text[len(prefix):].split(",")
        if part.strip()
    }


def _dod_gate_details(pr_number: int) -> dict[str, str] | None:
    """Failed DoD messages from the authoritative dry-run JSON payload."""
    script = Path(__file__).with_name("merge_pr.py")
    code, out, _ = run_cmd(
        [sys.executable, str(script), "--pr", str(pr_number), "--dry-run", "--json"],
        check=False,
    )
    try:
        payload = json.loads(out or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("pr") != pr_number
        or not isinstance(payload.get("gates"), list)
    ):
        return None
    details = {}
    for gate in payload["gates"]:
        if not isinstance(gate, dict) or gate.get("passed") is not False:
            continue
        name = gate.get("name")
        message = gate.get("message")
        if not isinstance(name, str) or not isinstance(message, str) or not message:
            return None
        details[_routable_gate_name(name)] = message
    if code == 0 and details:
        return None
    return details


def _author_can_repair_review(pr: dict[str, Any]) -> bool:
    """True when DoD `review` fails only because resolved threads lack evidence."""
    evidence = review_evidence(pr["number"])
    if not evidence:
        return False
    if not evidence.get("reviewed_head"):
        return False
    labels = label_names(pr)
    author = _label_value(labels, "author:")
    peers = [
        name[len("reviewed-by:"):]
        for name in labels
        if name.startswith("reviewed-by:")
        and name[len("reviewed-by:"):]
        and name[len("reviewed-by:"):] != author
    ]
    if not peers:
        return False
    if int(evidence.get("unresolved") or 0) > 0:
        return False
    return int(evidence.get("unfixed") or 0) > 0


def _author_fixable_from_unmet(
    pr: dict[str, Any], dod_reason: str | None,
) -> list[str] | None:
    """Author-clearable gate names, or None when a peer still has to act."""
    gates = _unmet_gates(dod_reason or "")
    if not gates:
        return None
    if gates - AUTHOR_FIXABLE_GATES - PEER_ROUTABLE_GATES:
        return None
    fixable = set(gates & AUTHOR_FIXABLE_GATES)
    if "review" in gates:
        if not _author_can_repair_review(pr):
            return None
        fixable.add("review-evidence")
    return sorted(fixable) if fixable else None


def author_gate_fix(pr: dict[str, Any], agent: str,
                    dod_reason: str | None) -> dict[str, Any] | None:
    """Work item when only author-clearable gates block `agent`'s own PR.

    Without this such a PR reaches no branch of ``select``: ``merge`` refuses it
    because a gate is unmet, and ``feedback`` refuses it because no review
    thread is open. It then sits open indefinitely while still holding its
    issue's ``touches:`` reservation, which blocks every issue declaring an
    overlapping path.

    ``dod_reason`` is the verdict ``merge_eligibility`` already produced for
    this PR. Subtype-sensitive gates perform one JSON dry run so the action is
    based on the real failure message rather than a lossy gate name. A ``None``
    verdict still returns ``None`` when merge eligibility failed a cheap filter
    before reaching any author-routable gate.
    """
    if pr.get("isDraft") or is_merged(pr):
        return None
    if _label_value(label_names(pr), "author:") != agent:
        return None
    threads = review_thread_count(pr)
    # None means the thread query failed - fail closed. A positive count is
    # ordinary review feedback, which keeps its higher priority in select().
    if threads is None or threads > 0:
        return None
    fixable = _author_fixable_from_unmet(pr, dod_reason)
    if not fixable:
        return None
    gate_details = {}
    if DETAIL_REQUIRED_GATES.intersection(fixable):
        all_details = _dod_gate_details(pr["number"])
        if all_details is None:
            return None
        for gate in DETAIL_REQUIRED_GATES.intersection(fixable):
            detail = all_details.get(gate)
            if not detail:
                return None
            gate_details[gate] = detail
    work = {"pr": pr["number"], "title": pr.get("title") or "",
            "unmet_gates": fixable, "reason": dod_reason}
    if gate_details:
        work["gate_details"] = gate_details
    return work


def review_eligibility(pr: dict[str, Any], agent: str, family: str | None,
                       round_cap: int, cross_family_wait: int,
                       merge_reason: str | None = None) -> dict[str, Any]:
    """Decides whether `agent` may review this PR, and why not if not.

    Returns {eligible, reason, cross_family, degraded}. `degraded` marks a
    same-family review taken only because the wait threshold passed.
    """
    def no(reason):
        return {"eligible": False, "reason": reason, "cross_family": False,
                "degraded": False, "stale_attribution": False}

    # Recovery queries may include merged PRs while close-out state is being
    # rebuilt. They belong only to merge/close-out recovery; never let stale
    # reviewer attribution route a closed PR back through review.
    if is_merged(pr):
        return no("merged PRs are not reviewable")

    labels = label_names(pr)
    author = _label_value(labels, "author:")
    pr_family = _label_value(labels, "family:")
    holder = reviewed_by(labels)

    # Set when a peer's completion stamp no longer names the head, so the report
    # can explain why an already-reviewed PR reappeared in the review queue.
    stale_attribution = False

    if pr.get("isDraft"):
        return no("draft")
    if holder and holder != agent:
        return no(f"already being reviewed by '{holder}'")
    if author and author == agent:
        return no("you wrote it")
    if not author and _authored_via_branch(pr, agent):
        # Unstamped PR - stamping is best-effort and legacy PRs predate it.
        # The branch still names the issue, and the issue still carries the
        # agent:<id> claim of whoever implemented it, so authorship is
        # recoverable without the label. Refusing outright would make every
        # legacy PR unreviewable; this refuses only the ones provably mine.
        return no("you wrote it (inferred from the linked issue's claim)")

    threads = review_thread_count(pr)
    if threads is None:
        return no("review thread state is unavailable")
    if threads:
        return no(f"{threads} active review feedback item(s); waiting on author")

    peer_reviewers = [
        name[len("reviewed-by:"):]
        for name in labels
        if name.startswith("reviewed-by:")
        and name[len("reviewed-by:"):]
        and name[len("reviewed-by:"):] != author
    ]
    if peer_reviewers:
        if not author:
            # Another review cannot repair missing authorship: merge_pr cannot
            # prove that any reviewed-by stamp is independent until the PR is
            # bound to its verified author. Reassigning the review would spin
            # forever while leaving the actual gate unchanged.
            return no(
                "independent review exists, but the PR has no author stamp"
            )
        gates = _unmet_gates(merge_reason or "")
        if merge_reason == "every Definition-of-Done gate passed" or (
            gates and "review" not in gates
        ):
            return no("independent review complete; waiting on gated merge")
        if "review" in gates:
            stale_attribution = True
        else:
            # Direct callers and cheap merge filters have no current-head DoD
            # verdict to reuse, so retain the authoritative fallback query.
            evidence = review_evidence(pr["number"])
            if evidence is None:
                return no("review attestation state is unavailable")
            attested = _attested_head_peers(evidence, peer_reviewers)
            # None means legacy evidence carrying no attestation records; keep the
            # existing verdict rather than reopening every historical PR.
            if attested is None or attested:
                return no("independent review complete; waiting on gated merge")
            stale_attribution = True

    decision = (pr.get("reviewDecision") or "").upper()
    if decision == "APPROVED" and not stale_attribution:
        # An approved PR is waiting on gated mechanical merge. A historical
        # CHANGES_REQUESTED decision with no current feedback instead needs a
        # fresh review so the latest verdict can unblock the merge gate.
        #
        # Stale attribution overrides this. GitHub does not dismiss a stale
        # approval unless branch protection is configured to, so a
        # distinct-account approval survives a push that it never covered.
        # merge_pr rejects such an approval for the same reason it rejects the
        # stale stamp, so suppressing here would restore the exact deadlock
        # above one branch later.
        return no("already approved")

    state = ci_state(pr)
    if state == "red":
        return no(f"CI is {state}")

    cross = bool(family and pr_family and pr_family != family)
    if cross or not family or not pr_family:
        # Unknown family on either side is treated as cross: there is no
        # evidence of overlap, and blocking on missing metadata would idle the
        # fleet for a labelling gap.
        return {"eligible": True, "reason": "", "cross_family": True,
                "degraded": False, "stale_attribution": stale_attribution}

    waited = waiting_minutes(pr)
    if waited >= cross_family_wait:
        return {"eligible": True,
                "reason": f"same family '{family}', waited {waited:.0f}m",
                "cross_family": False, "degraded": True,
                "stale_attribution": stale_attribution}
    return no(f"same family '{family}'; waiting {cross_family_wait - waited:.0f}m more "
              "for a cross-family reviewer")


def merge_eligibility(pr: dict[str, Any], agent: str) -> dict[str, Any]:
    """Decides whether `agent` may claim mechanical merge of this PR.

    Cheap label/CI/thread filters run first. Only survivors call the shared
    ``merge_pr.dod_status`` evaluator so picker cost stays proportional to
    near-ready PRs, not the whole open queue. Already-merged PRs are eligible
    only when close-out is still incomplete.
    """
    labels = label_names(pr)
    author = _label_value(labels, "author:")
    holder = merge_claimant(labels)

    def no(reason):
        return {"eligible": False, "reason": reason}

    if pr.get("isDraft"):
        return no("draft")
    if holder and holder != agent:
        return no(f"already being merged by '{holder}'")

    if is_merged(pr):
        if not closeout_incomplete(pr):
            return no("merged and close-out already complete")
        ok, reason = dod_status(pr["number"])
        if not ok:
            return no(reason)
        return {"eligible": True, "reason": reason}

    review_holder = reviewed_by(labels)
    if review_holder:
        return no(f"review still in progress by '{review_holder}'")

    threads = review_thread_count(pr)
    if threads is None:
        return no("review thread state is unavailable")
    if threads:
        return no(f"{threads} active review feedback item(s); waiting on author")

    state = ci_state(pr)
    if state != "green":
        if state == "red":
            return no("unmet: ci")
        if state != "none":
            return no(f"CI is {state}")

    peers = [
        name[len("reviewed-by:"):]
        for name in labels
        if name.startswith("reviewed-by:")
        and name[len("reviewed-by:"):]
        and name[len("reviewed-by:"):] != author
    ]
    if not peers and (pr.get("reviewDecision") or "").upper() != "APPROVED":
        return no("no independent review attribution yet")

    if author and author == agent and not peers:
        return no("author cannot merge without a distinct peer reviewer")

    ok, reason = dod_status(pr["number"])
    if not ok:
        return no(reason)
    return {"eligible": True, "reason": reason}


def mark(pr_number: int, label: str, colour: str, description: str) -> None:
    """Applies an advisory label. Never fatal - it is a signal, not a gate."""
    run_cmd(["gh", "label", "create", label, "--color", colour, "--description", description],
            check=False)
    code, _, err = run_cmd(["gh", "pr", "edit", str(pr_number), "--add-label", label],
                           check=False)
    if code != 0:
        print(f"[WARN] Could not label PR #{pr_number} '{label}': {err.strip()}", file=sys.stderr)


def record_review_claim(pr_number: int, agent: str, created_at: str | None) -> None:
    """Persist open-to-claim latency for the telemetry epic. Never a gate."""
    claimed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wait_line = "wait-minutes: unknown\n"
    if created_at:
        try:
            opened = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            waited = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
            wait_line = f"wait-minutes: {waited:.1f}\n"
        except ValueError:
            pass
    body = (
        "## Review claim\n"
        f"review-claimed-at: {claimed_at}\n"
        f"reviewer: {agent}\n"
        f"{wait_line}"
    )
    code, _, err = run_cmd(
        ["gh", "pr", "comment", str(pr_number), "--body", body],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not record review-claimed-at on #{pr_number}: "
              f"{err.strip()}", file=sys.stderr)


def select(agent: str, family: str | None, round_cap: int, cross_family_wait: int
           ) -> dict[str, Any]:
    """Builds the full picture, then picks by priority."""
    prs = list_work_prs()
    if prs is None:
        # Fail closed. Treating an unreadable queue as empty makes the selector
        # claim new implementation work as though no feedback or review were
        # waiting - growing the queue precisely while it cannot be observed.
        return {"agent": agent, "family": family,
                "work": {"type": "error", "skill": None,
                         "reason": "the pull request queue could not be read"},
                "mergeable_detail": [], "mergeable": [], "merge_skipped": [],
                "reviewable_detail": [], "reviewable": [], "skipped_prs": [],
                "escalated_prs": [], "claimable_issues": [],
                "blocked_by_dependencies": [], "blocked_by_file_conflict": [],
                "missing_touches": [], "operator_only_issues": []}

    # A PR whose review state cannot be read is not a candidate - it is skipped
    # (with its reason) and selection continues over the rest. A transient read
    # on one PR must not idle the whole agent for a loop cycle.
    unreadable = {pr["number"] for pr in prs if review_thread_count(pr) is None}

    # 1. Finish what I started.
    mine = [p for p in prs if needs_my_attention(p, agent)]
    feedback = min(mine, key=lambda p: p["number"]) if mine else None

    # 2. Merge independently reviewed, gate-green work.
    mergeable, merge_skipped = [], []
    # Keeps each PR's merge verdict so the gate-fix pass below can read the
    # already-evaluated gates instead of paying for a second evaluation.
    dod_reasons: dict[int, str] = {}
    for pr in sorted(prs, key=lambda p: p["number"]):
        verdict = merge_eligibility(pr, agent)
        dod_reasons[pr["number"]] = verdict["reason"]
        if verdict["eligible"]:
            mergeable.append(pr)
        else:
            # Only surface skips that looked like merge candidates, otherwise
            # every unreviewed PR pollutes the report with "no independent review".
            labels = label_names(pr)
            author = _label_value(labels, "author:")
            has_peer = any(
                name.startswith("reviewed-by:")
                and name[len("reviewed-by:"):]
                and name[len("reviewed-by:"):] != author
                for name in labels
            )
            if has_peer or merge_claimant(labels) == agent:
                merge_skipped.append({"number": pr["number"], "why": verdict["reason"]})

    # 2b. My own PR blocked only by a gate I can clear alone. It matches neither
    # merge nor feedback, so without this it reaches nobody and its issue's
    # touches: reservation blocks the board indefinitely.
    gate_fix = None
    for candidate in sorted(prs, key=lambda p: p["number"]):
        gate_fix = author_gate_fix(candidate, agent,
                                   dod_reasons.get(candidate["number"]))
        if gate_fix:
            break

    # 3. Review someone else's work.
    reviewable, skipped = [], []
    for pr in sorted(prs, key=lambda p: p["number"]):
        verdict = review_eligibility(
            pr,
            agent,
            family,
            round_cap,
            cross_family_wait,
            dod_reasons.get(pr["number"]),
        )
        if verdict["eligible"]:
            reviewable.append((pr, verdict))
        else:
            reason = verdict["reason"]
            if pr["number"] in unreadable:
                reason = "review thread state is unreadable"
            skipped.append({"number": pr["number"], "why": reason})

    # Cross-family first, then degraded same-family, oldest PR first within each.
    reviewable.sort(key=lambda pair: (not pair[1]["cross_family"], -waiting_minutes(pair[0])))

    # 4. Otherwise start something new - unchanged issue selection.
    issues = list_open_issues()
    parts = build_candidates(
        issues, agent, pr_files_by_issue=pr_files_by_issue_from_prs(prs),
    )

    if feedback is not None:
        work = {"type": "feedback", "pr": feedback["number"], "title": feedback["title"],
                "skill": "address-pr-feedback"}
    elif mergeable:
        pr = mergeable[0]
        work = {"type": "merge", "pr": pr["number"], "title": pr["title"],
                "skill": "merge-pr", "head_sha": pr.get("headRefOid")}
    elif gate_fix:
        # Reuses the routed `feedback` type rather than inventing one the loop
        # contract does not document; unmet_gates names the documented author
        # action instead of sending the agent hunting for nonexistent threads.
        work = {"type": "feedback", "pr": gate_fix["pr"], "title": gate_fix["title"],
                "skill": "address-pr-feedback",
                "unmet_gates": gate_fix["unmet_gates"], "reason": gate_fix["reason"]}
        if gate_fix.get("gate_details"):
            work["gate_details"] = gate_fix["gate_details"]
    elif reviewable:
        pr, verdict = reviewable[0]
        work = {"type": "review", "pr": pr["number"], "title": pr["title"],
                "skill": "code-review", "cross_family": verdict["cross_family"],
                "degraded": verdict["degraded"]}
    elif parts["my_in_flight"]:
        issue = parts["my_in_flight"]
        work = {"type": "issue", "issue": issue["number"], "title": issue["title"],
                "skill": skill_for_issue(issue), "resuming": True}
    elif parts["candidates"]:
        issue = parts["candidates"][0]
        work = {"type": "issue", "issue": issue["number"], "title": issue["title"],
                "skill": skill_for_issue(issue), "resuming": False}
    else:
        work = {"type": "idle", "skill": None}

    return {
        "agent": agent, "family": family, "work": work,
        "mergeable_detail": [
            {"pr": p["number"], "title": p["title"],
             "head_sha": p.get("headRefOid")}
            for p in mergeable
        ],
        "mergeable": [p["number"] for p in mergeable],
        "merge_skipped": merge_skipped,
        # Ordered candidates, so a lost claim race costs one retry rather than
        # sending the agent back through the whole picker.
        "reviewable_detail": [
            {"pr": p["number"], "title": p["title"],
             "cross_family": v["cross_family"], "degraded": v["degraded"],
             "stale_attribution": v.get("stale_attribution", False),
             "created_at": p.get("createdAt")}
            for p, v in reviewable
        ],
        "reviewable": [p["number"] for p, _ in reviewable],
        "skipped_prs": skipped,
        # Backwards-compatible JSON field. Review rounds never populate it.
        "escalated_prs": [],
        "claimable_issues": [i["number"] for i in parts["candidates"]],
        "blocked_by_dependencies": parts["blocked"],
        "blocked_by_file_conflict": parts["conflicted"],
        "missing_touches": parts["missing_touches"],
        "operator_only_issues": parts.get("operator_only", []),
    }


def main():
    parser = argparse.ArgumentParser(description="Pick the next work item for one agent.")
    parser.add_argument("--agent", required=True, help="Agent id; required for every claim")
    parser.add_argument("--family", default=None,
                        help="This agent's model family (anthropic, openai, ...). "
                             "Omitting it means every PR looks cross-family.")
    parser.add_argument("--claim", action="store_true", help="Claim the selected work item")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--round-cap", type=int, default=DEFAULT_ROUND_CAP,
        help="Deprecated compatibility option; review count never blocks routing",
    )
    parser.add_argument("--cross-family-wait", type=int, default=DEFAULT_CROSS_FAMILY_WAIT_MIN,
                        metavar="MINUTES")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release issue and review claims idle longer than HOURS")
    args = parser.parse_args()

    if args.reap_after:
        reap_stale_reviews(args.reap_after)
        reap_stale_merges(args.reap_after)
        reap_stale_claims(list_open_issues(), args.reap_after)

    res = select(args.agent, (args.family or "").lower() or None,
                 args.round_cap, args.cross_family_wait)
    work = res["work"]

    if args.claim and work["type"] == "merge":
        work["claimed"] = False
        for candidate in res.get("mergeable_detail") or []:
            rc = claim_merge(candidate["pr"], args.agent)
            if rc == EXIT_OK:
                work.update({"pr": candidate["pr"], "title": candidate["title"],
                             "head_sha": candidate.get("head_sha"),
                             "claimed": True})
                break
            if rc == EXIT_CONFLICT:
                print(f"[INFO] PR #{candidate['pr']} merge was taken; trying the next one.",
                      file=sys.stderr)
                continue
            work["claim_result"] = "error"
            break
        if not work["claimed"] and "claim_result" not in work:
            work["claim_result"] = "all_taken"
    elif args.claim and work["type"] == "review":
        # Walk the candidates: another agent claiming the top one first should
        # cost a retry, not a wasted cycle through the whole picker.
        work["claimed"] = False
        for candidate in res["reviewable_detail"]:
            rc = claim_review(candidate["pr"], args.agent)
            if rc == EXIT_OK:
                work.update({"pr": candidate["pr"], "title": candidate["title"],
                             "cross_family": candidate["cross_family"],
                             "degraded": candidate["degraded"], "claimed": True})
                record_review_claim(
                    candidate["pr"], args.agent, candidate.get("created_at"),
                )
                if candidate["degraded"]:
                    mark(candidate["pr"], "same-family-review", "fbca40",
                         "Reviewed by the author's own model family; no cross-family agent was free")
                break
            if rc == EXIT_CONFLICT:
                print(f"[INFO] PR #{candidate['pr']} was taken; trying the next one.",
                      file=sys.stderr)
                continue
            work["claim_result"] = "error"
            break
        if not work["claimed"] and "claim_result" not in work:
            # Every candidate was taken while we were deciding. Fall through to
            # implementation work rather than idling.
            work["claim_result"] = "all_taken"
            if res["claimable_issues"]:
                from claim_issue import claim_issue
                from common import get_issue
                for number in res["claimable_issues"]:
                    issue_meta = get_issue(number)
                    if not issue_meta:
                        print(
                            f"[WARN] Could not read issue #{number}; skipping claim.",
                            file=sys.stderr,
                        )
                        continue
                    if claim_issue(number, args.agent) == EXIT_OK:
                        work = {
                            "type": "issue",
                            "issue": number,
                            "skill": skill_for_issue(issue_meta),
                            "title": issue_meta.get("title") or "",
                            "resuming": False,
                            "claimed": True,
                        }
                        # Rebind the result too. Rebinding only the local name
                        # left --json reporting the unclaimed review while the
                        # issue was claimed and In Progress, so the agent would
                        # work the wrong item and strand the real claim.
                        res["work"] = work
                        break
    elif args.claim and work["type"] == "issue" and not work.get("resuming"):
        from claim_issue import claim_issue
        work["claimed"] = claim_issue(work["issue"], args.agent) == EXIT_OK

    if args.as_json:
        print(json.dumps(res, indent=2))
        return

    print("=== Aru_Agentic_SDLC: next work ===")
    print(f"👤 {args.agent}" + (f" ({args.family})" if args.family else " (family unset)"))
    if work["type"] == "feedback":
        print(f"🔁 Your PR #{work['pr']} has requested changes — address it before taking new work.")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "merge":
        print(f"🔀 Merge PR #{work['pr']} (Definition of Done passed)")
        print(f"   → merge_pr.py --pr {work['pr']}  (never gh pr merge)")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "review":
        tag = "cross-family" if work["cross_family"] else "SAME FAMILY (degraded)"
        print(f"🔍 Review PR #{work['pr']} [{tag}]")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "issue":
        verb = "Resume" if work.get("resuming") else "Implement"
        print(f"🛠️  {verb} issue #{work['issue']}")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "error":
        print(f"⛔ Picker error: {work.get('reason')}")
    else:
        print("✨ Nothing to do: no mergeable/reviewable PR and no claimable issue.")

    if res.get("merge_skipped"):
        print("\nMerge candidates not offered to you:")
        for item in res["merge_skipped"]:
            print(f"  #{item['number']}: {item['why']}")
    if res["skipped_prs"]:
        print("\nPRs not offered to you:")
        for item in res["skipped_prs"]:
            print(f"  #{item['number']}: {item['why']}")
    if work["type"] not in {"issue", "merge"} and res["claimable_issues"]:
        print(f"\nIssues waiting: {res['claimable_issues']}")


if __name__ == "__main__":
    main()
