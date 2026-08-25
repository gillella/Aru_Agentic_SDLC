#!/usr/bin/env python3
# line-ceiling: 1226
"""Return the highest-priority work one governed factory agent can perform.
Finishing beats starting: author feedback, merge-ready work, resumable issues,
then Ready issues. External review services remain outside the coding-agent queue.
A claiming picker that is truly idle may promote one fully qualified
Backlog issue, reselect it, and use the ordinary optimistic claim protocol.
"""

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_issue import (
    EXIT_CONFLICT,
    EXIT_OK,
    claim_merge,
    merge_claimant,
    reap_stale_merges,
)
from common import (
    board_agent_identities,
    get_repo_projects,
    get_repo_slug,
    list_open_issues,
    query_issue_project_items,
    run_cmd,
    select_governed_project_items,
    select_governed_projects,
    label_names as issue_label_names,
)
from fetch_next_issue import (
    active_increment_scope,
    attach_open_pr_file_snapshots,
    build_candidates,
    priority_rank,
    pr_files_by_issue_from_prs,
    reap_stale_claims,
)
from fetch_pr_feedback import fetch_active_review_feedback
import merge_pr
from merge_pr import closeout_incomplete, dod_status, is_merged, linked_issues
# _attested_head_peers is private, and importing it across modules is normally a
# smell. It is imported deliberately: merge_pr is the single source of truth for
# whether a peer's completion stamp names the current head, and a second
# implementation of that predicate in the picker is exactly the drift that let
# reviewed-but-since-pushed PRs reach no agent at all.
from merge_pr import review_evidence
from update_issue_status import update_status


# Seats disambiguate concurrent sessions sharing one checkout -- the only case a
# fingerprint alone cannot separate. Small on purpose: more than a handful of
# agents in one working copy is a misconfiguration, not a fleet.
MAX_WORKER_SEATS = 8


class AutoTriageError(RuntimeError):
    """An attempted automatic promotion left unverifiable lifecycle state."""


def _fingerprint_assign_identity(args, session_id):
    """Derive this worker's identity from where it runs (#310).

    The pool model assigned a name per *process*, so a restart -- a new PID with
    no link to the worker that had been running -- took a different name, and two
    machines each arbitrating from their own local registry both took ring[0].
    A fingerprint over machine, checkout, and family is stable across restarts
    and distinct across machines, so an id can never be reissued to a different
    worker.

    No board query is needed here: a board claim carrying this id is, by
    construction, this worker's own earlier work. That is what makes a restart
    reclaim its issue instead of treating it as somebody else's.
    """
    from agent_presence import (
        DEFAULT_PRESENCE_PATH,
        PresenceError,
        PresenceStore,
        fingerprint_agent_id,
    )
    family = (args.family or "").lower()
    # The seat ladder is the pool. resolve_free_identity skips seats held by
    # another *live* session and records the claim atomically, so a restart
    # (whose old claim has aged out) lands back on seat 1, while a genuinely
    # concurrent second session on the same checkout gets seat 2.
    ladder = [fingerprint_agent_id(family, seat=seat)
              for seat in range(1, MAX_WORKER_SEATS + 1)]
    store = PresenceStore(DEFAULT_PRESENCE_PATH)
    try:
        args.agent = store.resolve_free_identity(ladder, session_id)
    except PresenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[presence] worker identity '{args.agent}' for session '{session_id}'",
          file=sys.stderr)
    return None


def _auto_assign_identity(args, session_id):
    """Settle an identity when none was named on the command line.

    An operator-pinned ARU_AGENT_ID wins, then the fingerprint, then the legacy
    named pool for fleets that want fixed readable names.
    """
    from agent_presence import configured_agent_id

    pinned = configured_agent_id()
    if pinned:
        args.agent = pinned
        return _explicit_identity(args, session_id)
    if not getattr(args, "agent_pool", False):
        return _fingerprint_assign_identity(args, session_id)
    return _pool_assign_identity(args, session_id)


def _pool_assign_identity(args, session_id):
    """Pick a free identity from the named pool, consulting GitHub first.

    The registry alone was never enough: its 300-second heartbeat TTL freed an
    id that the board still showed holding an issue claim and authoring an open
    PR, so a live agent's id was reissued to a second session (#304). The
    registry stays as fast-path advisory state; GitHub decides liveness. Only
    reachable via --agent-pool now that #310 makes derived ids the default.
    """
    from agent_presence import (
        DEFAULT_AGENT_RING,
        DEFAULT_PRESENCE_PATH,
        PresenceError,
        PresenceStore,
    )
    holders, error = board_agent_identities()
    if holders is None:
        # Fail closed. Assigning under an unreadable board risks handing out an
        # id another agent is demonstrably using, which is the defect itself.
        print(f"[ERROR] Cannot verify agent id ownership on GitHub: {error}. "
              "Refusing to auto-assign an identity; pass --agent explicitly if "
              "you know the id is free.", file=sys.stderr)
        return 1

    store = PresenceStore(DEFAULT_PRESENCE_PATH)
    try:
        registered = sorted(store._read().get("agents") or {})
        pool = registered or list(DEFAULT_AGENT_RING)
        free_pool = [a for a in pool if a not in holders]
        if not free_pool:
            taken = ", ".join(
                f"{a} held by {holders[a][0]}" for a in pool if a in holders)
            print(f"[ERROR] Every agent id in the pool is in use on the board: "
                  f"{taken}. Refusing to reuse a live id -- add ids to the pool "
                  "or wait for that work to close.", file=sys.stderr)
            return 1
        args.agent = store.resolve_free_identity(free_pool, session_id)
        print(
            f"[presence] auto-assigned agent id '{args.agent}' for session '{session_id}'",
            file=sys.stderr,
        )
    except PresenceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return None


def _explicit_identity(args, session_id):
    """Refuse an explicitly named identity that another live session holds.

    The board is consulted for evidence, not as an extra refusal trigger. An
    agent legitimately keeps its id while it holds an issue claim and while its
    PR sits in review, so board presence alone cannot mean "taken" here without
    stopping an agent from ever picking up its next item. What the board adds is
    a message that names the work the conflicting session is on, instead of only
    an opaque session token.
    """
    from agent_presence import DEFAULT_PRESENCE_PATH, PresenceStore

    holder = PresenceStore(DEFAULT_PRESENCE_PATH).identity_holder(
        args.agent, session_id
    )
    if holder is None:
        return None
    holders, _error = board_agent_identities()
    where = (holders or {}).get(args.agent) or []
    evidence = f" It currently holds {', '.join(where)}." if where else ""
    print(
        f"[ERROR] agent '{args.agent}' already has a live heartbeat from "
        f"session '{holder}'.{evidence} Omit --agent to auto-assign a free "
        "identity, or wait for that session to expire.",
        file=sys.stderr,
    )
    return 1


def _resolve_identity(args, session_id):
    """Settle args.agent. Returns an exit code to abort on, or None to proceed."""
    if args.agent is None:
        return _auto_assign_identity(args, session_id)
    return _explicit_identity(args, session_id)


def _default_session_id() -> str:
    """A per-invocation session token so live claims are attributable."""
    return f"{socket.gethostname().split('.')[0]}|{os.getpid()}"


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
DEFAULT_REAP_AFTER_HOURS = 4

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
    A PR merely awaiting CodeRabbit is not coding-agent work.
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
    "verification", "spec-sync",
})
PEER_ROUTABLE_GATES = frozenset({"review"})

# These evaluate_dod names cannot create author work: open and issue-link
# failures are intercepted before gate evaluation, while review rounds is a
# visibility-only check that always passes. The exhaustiveness test requires a
# comment-backed entry here when evaluate_dod gains another deliberate non-route.
DOD_NON_ROUTABLE_GATES = frozenset({"open", "issue link", "review rounds"})
DETAIL_REQUIRED_GATES = frozenset({"rebased", "tests", "verification"})


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
    if merge_pr.assigned_review_service(pr) != "coderabbit":
        return False
    evidence = merge_pr._with_coderabbit_status(pr["number"], evidence)
    if not evidence:
        return False
    if not merge_pr.has_authoritative_coderabbit_review(pr, evidence):
        return False
    if int(evidence.get("unresolved") or 0) > 0:
        return False
    if int(evidence.get("outdated_unfixed") or 0) > 0:
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
    """Legacy API that always refuses coding-agent review work."""
    return {
        "eligible": False,
        "reason": ("CodeRabbit is the sole positive code-review authority "
                   "for this PR; coding agents implement and remediate findings only"),
        "cross_family": False,
        "degraded": False,
        "stale_attribution": False,
    }


def merge_eligibility(pr: dict[str, Any], agent: str) -> dict[str, Any]:  # noqa: C901, PLR0912
    """Decides whether `agent` may claim mechanical merge of this PR.

    Cheap label/CI/thread filters run first. Only survivors call the shared
    ``merge_pr.dod_status`` evaluator so picker cost stays proportional to
    near-ready PRs, not the whole open queue. Already-merged PRs are eligible
    only when close-out is still incomplete.
    """
    labels = label_names(pr)
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


def select(agent: str, family: str | None, round_cap: int, cross_family_wait: int  # noqa: C901, PLR0912, PLR0915
           ) -> dict[str, Any]:
    """Builds the full picture, then picks by priority."""
    prs = list_work_prs()
    if prs is None:
        # Fail closed. Treating an unreadable queue as empty makes the selector
        # claim new implementation work as though no feedback or remediation were
        # waiting - growing the queue precisely while it cannot be observed.
        return {"agent": agent, "family": family,
                "work": {"type": "error", "skill": None,
                         "reason": "the pull request queue could not be read"},
                "mergeable_detail": [], "mergeable": [], "merge_skipped": [],
                "reviewable_detail": [], "reviewable": [], "skipped_prs": [],
                "escalated_prs": [], "claimable_issues": [],
                "blocked_by_dependencies": [], "blocked_by_file_conflict": [],
                "missing_touches": [], "operator_only_issues": []}

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
            # Retired review labels are neither routing evidence nor queue state.
            labels = label_names(pr)
            if merge_claimant(labels) == agent:
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

    # CodeRabbit owns PR review. Coding-agent queue state deliberately contains
    # no review candidates; findings are surfaced through `feedback` above.
    reviewable, skipped = [], []

    # 4. Otherwise start something new - unchanged issue selection.
    issues = list_open_issues()
    parts = build_candidates(
        issues, agent, pr_files_by_issue=pr_files_by_issue_from_prs(prs),
        increment_scope=active_increment_scope(),
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


def _governed_open_issue_statuses(
    repo_slug: str, open_numbers: set[int],
) -> dict[int, str] | None:
    """Read the governed board once and prove its open-issue inventory complete."""
    projects = get_repo_projects(repo_slug)
    governed = select_governed_projects(projects or [], repo_slug)
    if len(governed) != 1:
        return None
    project = governed[0]
    owner = (project.get("owner") or {}).get("login")
    number = project.get("number")
    if not owner or not isinstance(number, int):
        return None
    code, stdout, _stderr = run_cmd([
        "gh", "project", "item-list", str(number), "--owner", owner,
        "--limit", "1000", "--format", "json",
    ], check=False)
    if code != 0:
        return None
    try:
        payload = json.loads(stdout)
        items = payload["items"]
        total = payload["totalCount"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(items, list) or total != len(items) or len(items) >= 1000:
        return None
    statuses: dict[int, str] = {}
    for item in items:
        content = item.get("content") or {}
        issue_number = content.get("number")
        repository = content.get("repository") or item.get("repository")
        if issue_number not in open_numbers or repository != repo_slug:
            continue
        status = item.get("status")
        if issue_number in statuses or not isinstance(status, str) or not status:
            return None
        statuses[issue_number] = status
    return statuses if set(statuses) == open_numbers else None


def _idle_backlog_candidate(agent: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return the one issue triage and the ordinary picker would admit."""
    from triage_backlog import partition, ready_gaps, split_reasons

    issues = list_open_issues()
    if not issues:
        return None, None
    if len(issues) >= 500:
        print("[WARN] Open issue inventory may be truncated; refusing auto-triage.",
              file=sys.stderr)
        return None, None

    repo_slug = get_repo_slug()
    open_numbers = {issue["number"] for issue in issues}
    board_statuses = _governed_open_issue_statuses(repo_slug or "", open_numbers)
    if board_statuses is None:
        print("[WARN] Governed board inventory is incomplete; refusing auto-triage.",
              file=sys.stderr)
        return None, repo_slug
    if any(status.lower() == "ready" for status in board_statuses.values()):
        return None, repo_slug
    if any("status:ready" in {name.lower() for name in issue_label_names(issue)}
           for issue in issues):
        return None, repo_slug

    backlog, ready, _held = partition(issues)
    if ready or not backlog:
        return None, None

    qualified_numbers = {
        issue["number"]
        for issue in backlog
        if [
            (label.get("name") or "").lower()
            for label in issue.get("labels", [])
            if (label.get("name") or "").lower().startswith("status:")
        ] == ["status:backlog"]
        and not (
            "trusted-rewrite" in {
                name.lower() for name in issue_label_names(issue)
            }
            and not issue.get("editor")
        )
        and not ready_gaps(issue, open_numbers, repo_slug=repo_slug)
        and not split_reasons(issue)
        and priority_rank(issue.get("labels", []))[0] is not None
    }
    if not qualified_numbers:
        return None, repo_slug

    staged_issues = []
    for issue in issues:
        if issue["number"] not in qualified_numbers:
            staged_issues.append(issue)
            continue
        staged = dict(issue)
        staged["labels"] = [
            label
            for label in issue.get("labels", [])
            if not (label.get("name") or "").lower().startswith("status:")
        ] + [{"name": "status:ready"}]
        staged_issues.append(staged)

    prs = list_work_prs()
    if prs is None:
        print("[WARN] Cannot triage while the pull request queue is unreadable.",
              file=sys.stderr)
        return None, repo_slug
    if len(prs) >= 200:
        print("[WARN] Open pull request inventory may be truncated; refusing auto-triage.",
              file=sys.stderr)
        return None, repo_slug
    parts = build_candidates(
        staged_issues,
        agent,
        pr_files_by_issue=pr_files_by_issue_from_prs(prs),
        increment_scope=active_increment_scope(),
    )
    return next(
        (issue for issue in parts["candidates"]
         if issue["number"] in qualified_numbers),
        None,
    ), repo_slug


def _promote_one_idle_backlog_issue_locked(
    agent: str, family: str | None, round_cap: int, cross_family_wait: int,
) -> int | None:
    """Promote one qualified Backlog issue when no Ready item exists.

    This is deliberately narrower than ``triage_backlog.py --promote``. The
    candidate must clear the triage and picker contracts twice without changing
    and still be Backlog on the governed board immediately before the write.
    """
    current = select(agent, family, round_cap, cross_family_wait)
    if current["work"]["type"] != "idle":
        return None
    candidate, repo_slug = _idle_backlog_candidate(agent)
    fresh, fresh_slug = _idle_backlog_candidate(agent)
    if candidate is None or fresh is None or not repo_slug or fresh_slug != repo_slug:
        return None

    compared_fields = (
        "number", "body", "labels", "author", "editor", "authorAssociation",
        "editorAssociation", "trustIdentityResolved",
    )
    if any(candidate.get(field) != fresh.get(field) for field in compared_fields):
        print("[WARN] Backlog candidate changed during triage; leaving it untouched.",
              file=sys.stderr)
        return None

    number = fresh["number"]

    def board_status() -> str:
        items = query_issue_project_items(number)
        governed = select_governed_project_items(items or [], repo_slug)
        return (
            ((governed[0].get("status") or {}).get("name") or "").lower()
            if len(governed) == 1 else ""
        )

    if board_status() != "backlog":
        print(f"[WARN] Issue #{number} is not authoritatively Backlog on the board.",
              file=sys.stderr)
        return None
    if not update_status(
        number, "Ready", require_board=True,
        expected_status="Backlog", require_unclaimed=True,
    ):
        raise AutoTriageError(
            f"qualified Backlog issue #{number} could not be promoted cleanly"
        )
    post = next(
        (issue for issue in list_open_issues() if issue["number"] == number), None,
    )
    post_statuses = {
        name.lower() for name in issue_label_names(post or {})
        if name.lower().startswith("status:")
    }
    post_agents = {
        name for name in issue_label_names(post or {}) if name.lower().startswith("agent:")
    }
    if post_statuses != {"status:ready"} or post_agents or board_status() != "ready":
        raise AutoTriageError(
            f"issue #{number} did not read back as unclaimed Ready on board and labels"
        )
    print(f"[INFO] Picker promoted qualified Backlog issue #{number} to Ready.",
          file=sys.stderr)
    return number


def promote_one_idle_backlog_issue(
    agent: str,
    family: str | None = None,
    round_cap: int = DEFAULT_ROUND_CAP,
    cross_family_wait: int = DEFAULT_CROSS_FAMILY_WAIT_MIN,
) -> int | None:
    """Serialize one auto-triage transition against other lifecycle writes."""
    with merge_pr.repository_merge_lock() as (locked, message):
        if not locked:
            print(f"[WARN] Auto-triage deferred: {message}.", file=sys.stderr)
            return None
        return _promote_one_idle_backlog_issue_locked(
            agent, family, round_cap, cross_family_wait,
        )


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Pick the next work item for one agent.")
    parser.add_argument("--agent", required=False, default=None,
                        help="Agent id. Omit to auto-assign a free identity from the "
                             "presence registry (see --session-id).")
    parser.add_argument("--session-id", default=None,
                        help="Identity of this loop session. Defaults to "
                             "hostname|pid. Used to attribute live presence claims "
                             "so two sessions never share an identity.")
    parser.add_argument("--family", default=None,
                        help="This agent's model family (anthropic, openai, ...). "
                             "Omitting it means every PR looks cross-family.")
    parser.add_argument("--agent-pool", action="store_true",
                        help="Assign an id from the named pool (claude-1, ...) "
                             "instead of deriving one from this worker. Legacy "
                             "path for fleets that want fixed readable names.")
    parser.add_argument("--claim", action="store_true", help="Claim the selected work item")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--round-cap", type=int, default=DEFAULT_ROUND_CAP,
        help="Deprecated compatibility option; review count never blocks routing",
    )
    parser.add_argument("--cross-family-wait", type=int, default=DEFAULT_CROSS_FAMILY_WAIT_MIN,
                        metavar="MINUTES")
    parser.add_argument("--reap-after", type=int, default=DEFAULT_REAP_AFTER_HOURS, metavar="HOURS",
                        help="Release issue and review claims idle longer than HOURS (default: 4h; 0 disables)")
    args = parser.parse_args()

    session_id = args.session_id or _default_session_id()
    rc = _resolve_identity(args, session_id)
    if rc is not None:
        return rc

    if args.reap_after > 0:
        try:
            reap_stale_merges(args.reap_after)
            reap_stale_claims(list_open_issues(), args.reap_after)
        except Exception as err:
            print(f"[WARN] Autonomous claim reap encountered error: {err}", file=sys.stderr)

    res = select(args.agent, (args.family or "").lower() or None,
                 args.round_cap, args.cross_family_wait)
    work = res["work"]

    # A read-only picker call remains read-only. A loop asking to claim work may
    # promote one mechanically qualified Backlog item, then immediately run the
    # ordinary selector again so normal claim arbitration still applies.
    if args.claim and work["type"] == "idle":
        try:
            promoted = promote_one_idle_backlog_issue(
                args.agent, (args.family or "").lower() or None,
                args.round_cap, args.cross_family_wait,
            )
        except AutoTriageError as exc:
            promoted = None
            res["work"] = {"type": "error", "skill": None, "reason": str(exc)}
            work = res["work"]
        if promoted is not None:
            res = select(args.agent, (args.family or "").lower() or None,
                         args.round_cap, args.cross_family_wait)
            res["auto_promoted_issue"] = promoted
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
    elif args.claim and work["type"] == "issue" and not work.get("resuming"):
        from claim_issue import claim_issue
        issue_rc = claim_issue(work["issue"], args.agent)
        work["claimed"] = issue_rc == EXIT_OK
        if not work["claimed"]:
            work["claim_result"] = (
                "conflict" if issue_rc == EXIT_CONFLICT else "error"
            )

    claim_failed = bool(
        args.claim
        and work["type"] in {"issue", "review", "merge"}
        and not work.get("resuming")
        and not work.get("claimed", False)
    )
    command_failed = claim_failed or work["type"] == "error"

    if args.as_json:
        print(json.dumps(res, indent=2))
        return 1 if command_failed else None

    print("=== Aru_Agentic_SDLC: next work ===")
    print(f"👤 {args.agent}" + (f" ({args.family})" if args.family else " (family unset)"))
    if claim_failed:
        number = work.get("issue") or work.get("pr")
        print(
            f"⛔ Could not claim {work['type']} #{number}; "
            f"result={work.get('claim_result', 'error')}. No work was started."
        )
    elif work["type"] == "feedback":
        print(f"🔁 Your PR #{work['pr']} has requested changes — address it before taking new work.")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "merge":
        print(f"🔀 Merge PR #{work['pr']} (Definition of Done passed)")
        print(f"   → merge_pr.py --pr {work['pr']}  (never gh pr merge)")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "issue":
        verb = "Resume" if work.get("resuming") else "Implement"
        print(f"🛠️  {verb} issue #{work['issue']}")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "error":
        print(f"⛔ Picker error: {work.get('reason')}")
    else:
        print("✨ Nothing to do: no mergeable PR, remediation, or claimable issue.")

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

    return 1 if command_failed else None


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
