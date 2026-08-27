#!/usr/bin/env python3
# #414 removed the review work type and ratcheted this file down from 1,264 lines.
# line-ceiling: 1260
"""Return the highest-priority work one governed factory agent can perform.
Finishing beats starting: author feedback, merge-ready work, resumable issues, then
Ready issues. Review is not coding-agent work at all -- the assigned external
service is the oracle, so a PR waiting on it is never routed here. A truly idle
claiming picker may promote one qualified Backlog issue and reselect.
"""

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import agent_presence as agent_presence
from agent_presence import (
    DEFAULT_WORKER_PATH,
    PLAN_ADOPT,
    PLAN_ROUTE,
    PresenceError,
    PresenceStore,
    WorkerHandoffStore,
    WorkerRecord as WorkerRecord,
    plan_worker_dispatch,
    route_worker as route_worker,
)
from claim_issue import (
    EXIT_CONFLICT, EXIT_OK, claim_issue, claim_merge, merge_claimant, reap_stale_merges,
)
from common import (get_repo_projects, get_repo_slug, label_names as issue_label_names,
                    query_open_issues as list_open_issues, repository_owner_login,
                    repository_trusted_logins, run_cmd)
from fetch_next_issue import (active_increment_scope, build_candidates, priority_rank,
                              pr_files_by_issue_from_prs, reap_stale_claims,
                              select_path_disjoint_candidates)
from fetch_pr_feedback import fetch_active_review_feedback
from github_inventory import json_lines, open_pull_requests
import merge_pr
from merge_pr import closeout_incomplete, dod_status, is_merged, linked_issues, review_evidence
from update_issue_status import update_status
from picker_board_inventory import (governed_board_inventory as _governed_open_issue_statuses,
                                    stage_expected_ready_for_triage)


class AutoTriageError(RuntimeError):
    """Automatic promotion could not prove or preserve lifecycle state."""


def _reserve_derived_identity(agent_id: str) -> int | None:
    """Refuse a second live session before it can reuse a derived identity."""
    from agent_presence import DEFAULT_PRESENCE_PATH, PresenceError, PresenceStore

    session_id = f"{socket.gethostname().split('.')[0]}|{os.getpid()}"
    try:
        PresenceStore(DEFAULT_PRESENCE_PATH).resolve_free_identity([agent_id], session_id)
    except PresenceError as exc:
        print(f"[ERROR] Derived agent identity '{agent_id}' is already in use by a live "
              f"local session. Pass a unique --agent or set ARU_AGENT_ID. {exc}", file=sys.stderr)
        return 1
    return None


def _resolve_identity(args):
    """Resolve overrides first, then reserve a derived identity for one session."""
    from agent_identity import AGENT_ID_ENV_VAR, resolve_agent_id

    derived = args.agent is None and AGENT_ID_ENV_VAR not in os.environ
    try:
        args.agent = resolve_agent_id(args.agent, family=(args.family or "").lower())
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return _reserve_derived_identity(args.agent) if derived else None


def skill_for_issue(issue: dict[str, Any]) -> str:
    """The trimmed kernel routes every issue through the implementation skill."""
    return "implement-next-issue"

DEFAULT_REAP_AFTER_HOURS = 4
_UNSET = object()
OPEN_PR_QUERY_LIMIT = 200
DOD_CANDIDATE_LIMIT = 3

PR_FIELDS = "number,title,isDraft,labels,reviews,statusCheckRollup,updatedAt,createdAt,headRefName,headRefOid,body,reviewDecision,state,mergedAt,files,changedFiles"


class DegradedPrSnapshot(list):
    pass


def _label_value(labels: list[str], prefix: str) -> str | None:
    return next((name[len(prefix):] for name in labels if name.startswith(prefix)), None)


def list_open_prs() -> list[dict[str, Any]] | None:
    code, out, err = run_cmd(["gh", "pr", "list", "--state", "open", "--json", PR_FIELDS,
                              "--limit", str(OPEN_PR_QUERY_LIMIT)], check=False)
    prs: Any = None
    if code != 0:
        print(f"[WARN] Rich PR query failed; using a bounded REST inventory: {err.strip()}",
              file=sys.stderr)
    else:
        try:
            prs = json.loads(out) if out else []
        except json.JSONDecodeError:
            print("[WARN] Rich PR query was malformed; using a bounded REST inventory.",
                  file=sys.stderr)
    # PR_FIELDS carries files; a capped ``gh pr list`` is truncated, not short.
    if isinstance(prs, list) and len(prs) >= OPEN_PR_QUERY_LIMIT:
        print("[WARN] Open PR inventory hit its cap; the queue is truncated.", file=sys.stderr)
        return None
    return prs if isinstance(prs, list) else _rest_open_prs()


def _paginate_api(path: str, jq: str, what: str) -> list[Any] | None:
    """Decode one paginated ``gh api`` read, failing closed on any bad page."""
    code, out, err = run_cmd(["gh", "api", "--paginate", path, "--jq", jq], check=False)
    if code != 0:
        print(f"[WARN] Could not read {what}: {err.strip()}", file=sys.stderr)
        return None
    records = json_lines(out)
    if records is None:
        print(f"[WARN] Could not parse {what}.", file=sys.stderr)
    return records


def _rest_open_prs() -> list[dict[str, Any]] | None:
    """Return the complete but deliberately non-authoritative REST PR snapshot.

    REST preserves queue visibility during a GraphQL outage but cannot batch the
    review-thread, check-rollup, and changed-file evidence routing and merge need,
    so records are marked degraded and selection stops here rather than deciding
    from partial evidence or paying N+1 calls.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    prs = open_pull_requests(run_cmd, slug)
    if prs is None:
        return None
    for pr in prs:
        pr.update({"mergedAt": None, "_degraded_rest_snapshot": True})
    return DegradedPrSnapshot(prs)


def _issue_closeout_snapshot(slug: str) -> dict[int, dict[str, Any]] | None:
    """Return all issue state needed for one merged-PR recovery scan.

    One paginated snapshot replaces a ``gh issue view`` per linked issue, so
    picker cost tracks API pages rather than repository history. A malformed or
    ambiguous page fails closed rather than starting work from a partial view.
    """
    issues = _paginate_api(f"repos/{slug}/issues?state=all&per_page=100",
                           ".[] | select(.pull_request == null) | {number,state,labels}",
                           "the issue close-out snapshot")
    if issues is None:
        return None
    snapshot: dict[int, dict[str, Any]] = {}
    for issue in issues:
        issue = issue if isinstance(issue, dict) else {}
        number, state, labels = issue.get("number"), issue.get("state"), issue.get("labels")
        if (not isinstance(number, int) or not isinstance(state, str)
                or state.upper() not in {"OPEN", "CLOSED"} or not isinstance(labels, list)
                or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                       for label in labels)
                or number in snapshot):
            print("[WARN] Issue close-out snapshot has invalid or duplicate data.", file=sys.stderr)
            return None
        snapshot[number] = {"state": state.upper(), "labels": labels}
    return snapshot


def _snapshot_closeout_incomplete(pr: dict[str, Any],
                                  issue_snapshot: dict[int, dict[str, Any]]) -> bool | None:
    """Mirror ``merge_pr.closeout_incomplete`` against a cycle snapshot."""
    if any(name.startswith("merger:") for name in label_names(pr)):
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

    Paginate until exhausted so a close-out older than the newest fifty merges
    stays discoverable, and fail closed on an unreadable page: a crashed close-out
    that becomes invisible is never recovered.
    """
    slug = get_repo_slug()
    if not slug:
        print("[WARN] Could not resolve repo slug for merged PR recovery.", file=sys.stderr)
        return None
    closed = _paginate_api(
        f"repos/{slug}/pulls?state=closed&per_page=100&sort=updated&direction=desc",
        ".[]", "the closed-PR inventory")
    if closed is None:
        return None
    merged = [item for item in closed if item.get("merged_at")]
    if not merged:
        return []
    issue_snapshot = _issue_closeout_snapshot(slug)
    if issue_snapshot is None:
        return None

    recovery = []
    for item in merged:
        head = item.get("head") if isinstance(item.get("head"), dict) else {}
        pr = {"number": item.get("number"), "title": item.get("title") or "",
              "isDraft": bool(item.get("draft")), "reviews": [], "statusCheckRollup": [],
              "labels": [{"name": lab.get("name", "")} for lab in (item.get("labels") or [])],
              "updatedAt": item.get("updated_at"), "createdAt": item.get("created_at"),
              "headRefName": head.get("ref") or "", "headRefOid": head.get("sha") or "",
              "body": item.get("body") or "", "reviewDecision": "", "state": "MERGED",
              "mergedAt": item.get("merged_at"), "_active_review_feedback": []}
        incomplete = _snapshot_closeout_incomplete(pr, issue_snapshot)
        if incomplete is None:
            print(f"[WARN] Linked issue state for merged PR #{pr['number']} "
                  "is absent from the authoritative snapshot.", file=sys.stderr)
            return None
        if incomplete:
            recovery.append(pr)
    return recovery


def list_work_prs() -> list[dict[str, Any]] | None:
    """Open PRs plus merged PRs that still need close-out recovery."""
    open_prs = list_open_prs()
    if open_prs is None:
        return None
    if isinstance(open_prs, DegradedPrSnapshot):
        return open_prs
    recovery = list_merged_needing_closeout()
    if recovery is None:
        return None
    return open_prs + recovery


def label_names(pr: dict[str, Any]) -> list[str]:
    return [lab.get("name", "") for lab in pr.get("labels", [])]


def review_thread_count(pr: dict[str, Any]) -> int | None:
    """Returns and caches the live active-feedback count for one selection.

    A same-account blocking review is necessarily COMMENTED, so reviewDecision
    cannot route it: unresolved threads are the fail-closed author-feedback state,
    and zero threads plus authoritative assigned-service review evidence is the
    approval-equivalent completion state.
    """
    if "_active_review_feedback" not in pr:
        pr["_active_review_feedback"] = fetch_active_review_feedback(pr["number"])
    feedback = pr["_active_review_feedback"]
    return None if feedback is None else len(feedback)


def ci_state(pr: dict[str, Any]) -> str:
    """Returns 'green', 'red', 'pending', or 'none'.

    A PR with no checks at all is 'none', not 'green', so merge still refuses
    an unverified head; red always blocks.
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
        if ((status and status != "COMPLETED" and not result)
                or result in {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"}):
            pending = True
    return "pending" if pending else "green"


def needs_my_attention(pr: dict[str, Any], agent: str) -> bool:
    """True when this is my PR and a reviewer has asked for something.

    Current thread state, not reviewDecision: a same-account COMMENTED review has
    no decision, and a stale CHANGES_REQUESTED can outlive every resolved thread.
    A PR merely awaiting its review service is not coding-agent work.
    """
    if _label_value(label_names(pr), "author:") != agent:
        return False
    threads = review_thread_count(pr)
    return threads is not None and threads > 0


# Definition-of-Done gates a PR's own author can clear alone. Work arising from
# these is offered only to author:<id>; the routed skill documents the concrete
# action for every name in this set. `review-evidence` is the
# unfixed-resolved-thread case: the assigned service already reviewed, threads
# are resolved, but no follow-up commit or `Withdrawn:` reply exists. Generic
# `review` (the service still has to act) is never author-fixable.
AUTHOR_FIXABLE_GATES = frozenset({"accept", "ci", "rebased", "review-evidence", "size", "tests",
                                  "verification", "spec-sync"})
PEER_ROUTABLE_GATES = frozenset({"review"})

# These evaluate_dod names cannot create author work: open and issue-link failures
# are intercepted before gate evaluation. The exhaustiveness test requires a
# comment-backed entry here when evaluate_dod gains another deliberate non-route.
DOD_NON_ROUTABLE_GATES = frozenset({"open", "issue link"})
DETAIL_REQUIRED_GATES = frozenset({"rebased", "tests", "verification"})


def _routable_gate_name(name: str) -> str:
    """Normalize parameterized DoD names to the action the author can take."""
    cleaned = name.strip()
    return "accept" if re.fullmatch(r"accept\s+#\d+", cleaned, re.IGNORECASE) else cleaned


def _unmet_gates(reason: str) -> set[str]:
    """Gate names from a ``dod_status`` reason, or empty when it is not one.

    ``dod_status`` also returns prose for fetch failures, a missing ``Closes
    #<issue>``, and off-head review evidence; treating those as gate lists would
    invent work from an unknown state.
    """
    text = (reason or "").strip()
    prefix = "unmet:"
    if not text.lower().startswith(prefix):
        return set()
    return {_routable_gate_name(part) for part in text[len(prefix):].split(",") if part.strip()}


def _dod_gate_details(pr_number: int) -> dict[str, str] | None:
    """Failed DoD messages from the authoritative dry-run JSON payload."""
    script = Path(__file__).with_name("merge_pr.py")
    code, out, _ = run_cmd([sys.executable, str(script), "--pr", str(pr_number), "--dry-run",
                            "--json"], check=False)
    try:
        payload = json.loads(out or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if (not isinstance(payload, dict) or payload.get("pr") != pr_number
            or not isinstance(payload.get("gates"), list)):
        return None
    details = {}
    for gate in payload["gates"]:
        if not isinstance(gate, dict) or gate.get("passed") is not False:
            continue
        name, message = gate.get("name"), gate.get("message")
        if not isinstance(name, str) or not isinstance(message, str) or not message:
            return None
        details[_routable_gate_name(name)] = message
    if code == 0 and details:
        return None
    return details


def _author_can_repair_review(pr: dict[str, Any]) -> bool:
    """True when DoD `review` fails only because resolved threads lack evidence."""
    service = merge_pr.assigned_review_service(pr)
    if service not in {"coderabbit", "sourcery", "codeant"}:
        return False
    evidence = review_evidence(pr["number"])
    if evidence:
        evidence = merge_pr.with_service_evidence(pr, pr["number"], evidence)
    if not evidence or not merge_pr.has_authoritative_assigned_review(pr, evidence):
        return False
    counts = merge_pr._service_thread_counts(evidence, service)
    if not isinstance(counts, dict):
        return False
    return (int(counts.get("unfixed") or 0) > 0
            and int(counts.get("unresolved") or 0) <= 0
            and int(counts.get("outdated_unfixed") or 0) <= 0)


def _author_fixable_from_unmet(pr: dict[str, Any], dod_reason: str | None) -> list[str] | None:
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


def author_gate_fix(pr: dict[str, Any], agent: str, dod_reason: str | None) -> dict[str, Any] | None:
    """Work item when only author-clearable gates block `agent`'s own PR.

    Without this such a PR reaches no branch of ``select`` - ``merge`` refuses an
    unmet gate, ``feedback`` refuses an empty thread list - so it sits open holding
    its issue's ``touches:`` reservation against every overlapping issue.
    ``dod_reason`` is the verdict ``merge_eligibility`` already produced; subtype-
    sensitive gates spend one JSON dry run so the routed action carries the real
    failure message rather than a lossy gate name.
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


def merge_eligibility(pr: dict[str, Any], agent: str,  # noqa: C901, PLR0912
                      dod_budget: list[int] | None = None,
                      dod_cache: dict[int, tuple[bool, str]] | None = None) -> dict[str, Any]:
    """Decide merge eligibility, spending optional budget only on full DoD checks."""
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
    else:
        if pr.get("_degraded_rest_snapshot"):
            return no("GraphQL review, CI, and file evidence is unavailable")
        # CI is present in the initial batched PR snapshot. Reject obvious
        # non-candidates before paying for a per-PR GraphQL review-thread query.
        state = ci_state(pr)
        if state != "green":
            return no("unmet: ci" if state in {"red", "none"} else f"CI is {state}")
        # Reuse author feedback; DoD already checks threads for other green PRs.
        if "_active_review_feedback" in pr or _label_value(labels, "author:") == agent:
            threads = review_thread_count(pr)
            if threads is None:
                return no("review thread state is unavailable")
            if threads:
                return no(f"{threads} active review feedback item(s); waiting on author")

    cached = dod_cache.get(pr["number"]) if dod_cache is not None else None
    if cached is None:
        if dod_budget is not None:
            if dod_budget[0] <= 0:
                return no("Definition-of-Done evaluation deferred by per-cycle query budget")
            dod_budget[0] -= 1
        ok, reason = dod_status(pr["number"])
        if dod_cache is not None:
            dod_cache[pr["number"]] = (ok, reason)
    else:
        ok, reason = cached
    return {"eligible": True, "reason": reason} if ok else no(reason)


def _selection(agent: str, family: str | None, work: dict[str, Any],
               **fields: Any) -> dict[str, Any]:
    """One selection payload with every key the loop contract promises."""
    payload = {"agent": agent, "family": family, "work": work, "mergeable_detail": [],
               "mergeable": [], "merge_skipped": [], "claimable_issues": [],
               "blocked_by_dependencies": [], "blocked_by_file_conflict": [], "missing_touches": [],
               "operator_only_issues": []}
    payload.update(fields)
    return payload


def _error_selection(agent: str, family: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    """An empty selection carrying why nothing could be routed.

    Fail closed: reading an unreadable queue as an empty one would start new work as
    though no feedback or remediation were waiting - growing the queue precisely
    while it cannot be observed.
    """
    return _selection(agent, family, {"type": "error", "skill": None, "reason": reason}, **extra)


def select(agent: str, family: str | None, *, prs_snapshot: Any = _UNSET,  # noqa: C901, PLR0912
           issues_snapshot: Any = _UNSET) -> dict[str, Any]:
    """Builds the full picture, then picks by priority."""
    prs = list_work_prs() if prs_snapshot is _UNSET else prs_snapshot
    if prs is None:
        return _error_selection(agent, family, "the pull request queue could not be read")

    if sum(1 for pr in prs if not is_merged(pr)) >= OPEN_PR_QUERY_LIMIT:
        return _error_selection(agent, family,
                                "open pull request inventory may be truncated; refusing selection")

    degraded = [pr["number"] for pr in prs if pr.get("_degraded_rest_snapshot")]
    if isinstance(prs, DegradedPrSnapshot) or degraded:
        return _error_selection(agent, family,
                                "GraphQL is unavailable; REST preserved the open-PR inventory but "
                                "cannot prove review, CI, or file state", degraded_rest_prs=degraded)

    # 1. Finish what I started.
    mine = [p for p in prs if needs_my_attention(p, agent)]
    feedback = min(mine, key=lambda p: p["number"]) if mine else None

    # 2. Merge independently reviewed, gate-green work. Each verdict is kept so
    # the gate-fix pass below reads gates it already evaluated rather than
    # paying for a second evaluation.
    mergeable, merge_skipped = [], []
    dod_reasons: dict[int, str] = {}
    dod_budget = [DOD_CANDIDATE_LIMIT]
    for pr in sorted(prs, key=lambda p: p["number"]):
        verdict = merge_eligibility(pr, agent, dod_budget)
        dod_reasons[pr["number"]] = verdict["reason"]
        if verdict["eligible"]:
            mergeable.append(pr)
        elif merge_claimant(label_names(pr)) == agent:
            # Retired review labels are neither routing evidence nor queue state.
            merge_skipped.append({"number": pr["number"], "why": verdict["reason"]})

    # 2b. My own PR blocked only by a gate I can clear alone: it matches neither
    # merge nor feedback, so without this it reaches nobody while its issue's
    # touches: reservation blocks the board indefinitely.
    gate_fix = None
    for candidate in sorted(prs, key=lambda p: p["number"]):
        gate_fix = author_gate_fix(candidate, agent, dod_reasons.get(candidate["number"]))
        if gate_fix:
            break

    # 3. Otherwise start something new - unchanged issue selection.
    issues = list_open_issues() if issues_snapshot is _UNSET else issues_snapshot
    if issues is None:
        return _error_selection(agent, family, "the open issue queue could not be read")
    parts = build_candidates(issues, agent, pr_files_by_issue=pr_files_by_issue_from_prs(prs),
                             increment_scope=active_increment_scope())

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
    elif parts["my_in_flight"] or parts["candidates"]:
        resuming = bool(parts["my_in_flight"])
        issue = parts["my_in_flight"] or parts["candidates"][0]
        work = {"type": "issue", "issue": issue["number"], "title": issue["title"],
                "skill": skill_for_issue(issue), "resuming": resuming}
    else:
        work = {"type": "idle", "skill": None}

    return _selection(
        agent, family, work,
        mergeable_detail=[{"pr": p["number"], "title": p["title"],
                           "head_sha": p.get("headRefOid")} for p in mergeable],
        mergeable=[p["number"] for p in mergeable], merge_skipped=merge_skipped,
        claimable_issues=[i["number"] for i in parts["candidates"]],
        blocked_by_dependencies=parts["blocked"], blocked_by_file_conflict=parts["conflicted"],
        missing_touches=parts["missing_touches"],
        operator_only_issues=parts.get("operator_only", []))


def build_inventory_snapshot(  # noqa: PLR0913
    *, prs_snapshot: Any = _UNSET, issues_snapshot: Any = _UNSET,
    repo_owner: Any = _UNSET, trusted_logins: Any = _UNSET,
    increment_scope: Any = _UNSET,
) -> dict[str, Any]:
    """Read each authoritative inventory once for all lanes in this tick."""
    prs = list_work_prs() if prs_snapshot is _UNSET else prs_snapshot
    if prs is None:
        return {"error": "the pull request queue could not be read"}
    if sum(1 for pr in prs if not is_merged(pr)) >= OPEN_PR_QUERY_LIMIT:
        return {"error": "open pull request inventory may be truncated; refusing selection"}
    if isinstance(prs, DegradedPrSnapshot) or any(
            pr.get("_degraded_rest_snapshot") for pr in prs):
        return {"error": "GraphQL is unavailable; lane selection requires rich PR evidence"}
    issues = list_open_issues() if issues_snapshot is _UNSET else issues_snapshot
    if issues is None:
        return {"error": "the open issue queue could not be read"}
    owner = repository_owner_login() if repo_owner is _UNSET else repo_owner
    trusted = repository_trusted_logins() if trusted_logins is _UNSET else trusted_logins
    scope = active_increment_scope() if increment_scope is _UNSET else increment_scope
    if owner is None and trusted is None:
        return {"error": "trusted issue metadata authors could not be resolved"}
    # Thread hydration belongs to the shared snapshot, never to a per-lane pass.
    for pr in prs:
        if (not is_merged(pr) and "_active_review_feedback" not in pr
                and _label_value(label_names(pr), "author:")):
            pr["_active_review_feedback"] = fetch_active_review_feedback(pr["number"])
    return {
        "prs": prs,
        "issues": issues,
        "repo_owner": owner,
        "trusted_logins": trusted,
        "increment_scope": scope,
        "pr_files_by_issue": pr_files_by_issue_from_prs(prs),
        "dod_budget": [DOD_CANDIDATE_LIMIT],
        "dod_cache": {},
    }


def _lane_issue_parts(snapshot: dict[str, Any], agent: str | None) -> dict[str, Any]:
    return build_candidates(
        snapshot["issues"], agent,
        pr_files_by_issue=snapshot["pr_files_by_issue"],
        repo_owner=snapshot["repo_owner"],
        trusted_logins=snapshot["trusted_logins"],
        increment_scope=snapshot["increment_scope"],
    )


def select_lanes_from_snapshot(  # noqa: C901, PLR0912
    snapshot: dict[str, Any], lane_agents: list[str], family: str | None,
) -> dict[str, Any]:
    """Assign a bounded batch from one immutable inventory snapshot."""
    agents = sorted(lane_agents)
    if not agents or len(agents) != len(set(agents)):
        raise PresenceError("lane agents must be a non-empty distinct list")
    if snapshot.get("error"):
        return {
            "schema": "aru.fetch-next-work/v2", "lanes": {"agents": agents},
            "claim_status": "not-requested", "work_items": [],
            "work": {"type": "error", "skill": None, "reason": snapshot["error"]},
        }

    free = list(agents)
    taken_prs: set[int] = set()
    taken_issues: set[int] = set()
    items: list[dict[str, Any]] = []
    dod_reasons: dict[tuple[int, str], str] = {}

    def assign(agent: str, work: dict[str, Any]) -> None:
        items.append({"lane": len(items) + 1, "agent": agent,
                      "family": family, "work": work, "dispatchable": True})
        free.remove(agent)

    # Finish all author feedback before assigning any lower phase.
    feedback = []
    for agent in free:
        mine = [pr for pr in snapshot["prs"] if needs_my_attention(pr, agent)]
        if mine:
            feedback.append((min(mine, key=lambda pr: pr["number"]), agent))
    for candidate, agent in sorted(feedback, key=lambda pair: (pair[0]["number"], pair[1])):
        if candidate["number"] in taken_prs or agent not in free:
            continue
        taken_prs.add(candidate["number"])
        assign(agent, {"type": "feedback", "pr": candidate["number"],
                       "title": candidate["title"], "skill": "address-pr-feedback",
                       "resuming": True})

    # Independently mergeable work fills the lowest-sorting free lanes.
    for candidate in sorted(snapshot["prs"], key=lambda pr: pr["number"]):
        if not free or candidate["number"] in taken_prs:
            continue
        for agent in list(free):
            verdict = merge_eligibility(
                candidate, agent, snapshot["dod_budget"], snapshot["dod_cache"],
            )
            dod_reasons[(candidate["number"], agent)] = verdict["reason"]
            if verdict["eligible"]:
                taken_prs.add(candidate["number"])
                assign(agent, {"type": "merge", "pr": candidate["number"],
                               "title": candidate["title"], "skill": "merge-pr",
                               "head_sha": candidate.get("headRefOid")})
                break

    # Author-clearable gates stay bound to the author lane.
    for agent in list(free):
        for candidate in sorted(snapshot["prs"], key=lambda pr: pr["number"]):
            if candidate["number"] in taken_prs:
                continue
            gate = author_gate_fix(
                candidate, agent, dod_reasons.get((candidate["number"], agent)),
            )
            if gate:
                taken_prs.add(candidate["number"])
                work = {"type": "feedback", "pr": gate["pr"], "title": gate["title"],
                        "skill": "address-pr-feedback", "unmet_gates": gate["unmet_gates"],
                        "reason": gate["reason"], "resuming": True}
                if gate.get("gate_details"):
                    work["gate_details"] = gate["gate_details"]
                assign(agent, work)
                break

    parts_by_agent = {agent: _lane_issue_parts(snapshot, agent) for agent in list(free)}
    resumes = []
    for agent, parts in parts_by_agent.items():
        if parts["my_in_flight"]:
            resumes.append((parts["my_in_flight"], agent))
    for issue, agent in sorted(resumes, key=lambda pair: (pair[0]["number"], pair[1])):
        if agent in free and issue["number"] not in taken_issues:
            taken_issues.add(issue["number"])
            assign(agent, {"type": "issue", "issue": issue["number"],
                           "title": issue["title"], "skill": skill_for_issue(issue),
                           "resuming": True})

    common = _lane_issue_parts(snapshot, None)
    selected, local_conflicts = select_path_disjoint_candidates(
        [issue for issue in common["candidates"] if issue["number"] not in taken_issues],
        max(1, len(free)), [],
    ) if free else ([], [])
    for issue, agent in zip(selected, list(free)):
        taken_issues.add(issue["number"])
        assign(agent, {"type": "issue", "issue": issue["number"],
                       "title": issue["title"], "skill": skill_for_issue(issue),
                       "resuming": False})

    return {
        "schema": "aru.fetch-next-work/v2",
        "lanes": {"requested": len(agents), "verified": len(agents), "agents": agents},
        "claim_status": "not-requested",
        "work_items": items,
        "work": {"type": "error", "skill": None,
                 "reason": "multi-lane result: read work_items"},
        "blocked_by_dependencies": common["blocked"],
        "blocked_by_file_conflict": common["conflicted"] + local_conflicts,
        "missing_touches": common["missing_touches"],
    }


def claim_lane_items(items: list[dict[str, Any]]) -> str:
    """Claim in snapshot order and stop at the first stale-snapshot result."""
    successful = 0
    failed = False
    for item in items:
        work, agent = item["work"], item["agent"]
        if item.get("dispatchable") is False:
            continue
        if work.get("resuming") or work["type"] == "feedback":
            work["resuming"] = True
            continue
        if failed:
            work.update({"claimed": False,
                         "claim_result": "not-attempted-after-partial-failure"})
            item["dispatchable"] = False
            continue
        rc = (claim_issue(work["issue"], agent) if work["type"] == "issue"
              else claim_merge(work["pr"], agent))
        work["claimed"] = rc == EXIT_OK
        item["dispatchable"] = work["claimed"]
        if work["claimed"]:
            successful += 1
            continue
        work["claim_result"] = "conflict" if rc == EXIT_CONFLICT else "error"
        failed = True
    if not failed:
        return "complete"
    return "partial" if successful else "failed"


def apply_worker_handoffs(items: list[dict[str, Any]], *, store: WorkerHandoffStore,
                          project_id: str, now: Any = None) -> list[dict[str, Any]]:
    """Suppress duplicate dispatch and expose terminal records for governed routing."""
    for item in items:
        work = item["work"]
        number = work.get("issue") or work.get("pr")
        if not number:
            continue
        plan = plan_worker_dispatch(
            store, project_id=project_id,
            unit_kind="issue" if work["type"] == "issue" else "pr",
            unit_number=number, now=now,
        )
        if plan["action"] == PLAN_ADOPT:
            item.update({"dispatchable": False, "reason": "already-running",
                         "worker_id": plan["record"].worker_id})
        elif plan["action"] == PLAN_ROUTE:
            item.update({"dispatchable": False, "reason": "worker-needs-routing",
                         "handoff_action": PLAN_ROUTE,
                         "worker_id": plan["record"].worker_id})
    return items


def _idle_backlog_candidate(  # noqa: C901, PLR0912
    agent: str, expected_ready_issue: int | None = None, *, issues_snapshot: Any = _UNSET,
    prs_snapshot: Any = _UNSET, repo_slug_snapshot: str | None = None,
    projects_snapshot: Any = _UNSET, snapshot_out: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the one issue triage and the ordinary picker would admit."""
    from triage_backlog import partition, ready_gaps, split_reasons
    issues = list_open_issues() if issues_snapshot is _UNSET else issues_snapshot
    if issues is None:
        raise AutoTriageError("Open issue inventory unavailable; cannot prove the picker is idle")
    if not issues:
        return None, None
    if len(issues) >= 500:
        print("[WARN] Open issue inventory may be truncated; refusing auto-triage.", file=sys.stderr)
        return None, None

    repo_slug = repo_slug_snapshot or get_repo_slug()
    open_numbers = {issue["number"] for issue in issues}
    inventory = (_governed_open_issue_statuses(repo_slug or "", open_numbers,
                                               projects=projects_snapshot)
                 if projects_snapshot is not _UNSET
                 else _governed_open_issue_statuses(repo_slug or "", open_numbers))
    if inventory is None:
        if expected_ready_issue is None:
            raise AutoTriageError("Project inventory unavailable; cannot prove Ready is empty")
        return None, repo_slug
    _board_statuses, ready_count = inventory
    target_ready = _board_statuses.get(expected_ready_issue, "").lower() == "ready"
    if (ready_count != int(expected_ready_issue is not None)
            or (expected_ready_issue is not None and not target_ready)):
        return None, repo_slug
    triage_issues = stage_expected_ready_for_triage(issues, expected_ready_issue)
    if triage_issues is None:
        return None, repo_slug
    triage_board_statuses = dict(_board_statuses)
    if expected_ready_issue is not None:
        triage_board_statuses[expected_ready_issue] = "Backlog"

    backlog, ready, _held = partition(triage_issues)
    if ready or not backlog:
        return None, None

    def status_labels(issue):
        names = ((label.get("name") or "").lower() for label in issue.get("labels", []))
        return [name for name in names if name.startswith("status:")]

    qualified_numbers = {
        issue["number"] for issue in backlog
        if triage_board_statuses.get(issue["number"], "").lower() == "backlog"
        and status_labels(issue) == ["status:backlog"]
        and not ("trusted-rewrite" in {name.lower() for name in issue_label_names(issue)}
                 and not issue.get("editor"))
        and not ready_gaps(issue, open_numbers, repo_slug=repo_slug)
        and not split_reasons(issue)
        and priority_rank(issue.get("labels", []))[0] is not None
    }
    if not qualified_numbers:
        return None, repo_slug

    def staged(issue):
        if issue["number"] not in qualified_numbers:
            return issue
        kept = [label for label in issue.get("labels", [])
                if not (label.get("name") or "").lower().startswith("status:")]
        return dict(issue, labels=kept + [{"name": "status:ready"}])

    staged_issues = [staged(issue) for issue in triage_issues]

    prs = list_work_prs() if prs_snapshot is _UNSET else prs_snapshot
    if prs is None:
        print("[WARN] Cannot triage while the pull request queue is unreadable.", file=sys.stderr)
        return None, repo_slug
    if sum(1 for pr in prs if not is_merged(pr)) >= OPEN_PR_QUERY_LIMIT:
        print("[WARN] Open pull request inventory may be truncated; refusing auto-triage.",
              file=sys.stderr)
        return None, repo_slug
    if snapshot_out is not None:
        snapshot_out.update({"issues": issues, "prs": prs})
    increment_scope = _auto_triage_increment_scope()
    if increment_scope is False:
        return None, repo_slug
    parts = build_candidates(staged_issues, agent, pr_files_by_issue=pr_files_by_issue_from_prs(prs),
                             increment_scope=increment_scope)
    return next((issue for issue in parts["candidates"]
                 if issue["number"] in qualified_numbers), None), repo_slug


def _auto_triage_increment_scope() -> set | None | bool:
    try:
        return active_increment_scope(fail_on_error=True)
    except Exception as exc:
        print(f"[WARN] Active increment state is unreadable: {exc}", file=sys.stderr)
        return False


def _promote_one_idle_backlog_issue_locked(
    agent: str, family: str | None, *, current_selection: dict[str, Any] | None = None,
    issues_snapshot: Any = _UNSET, prs_snapshot: Any = _UNSET,
    post_snapshot_out: dict[str, Any] | None = None,
) -> int | None:
    """Promote one qualified Backlog issue when no Ready item exists.

    Deliberately narrower than ``triage_backlog.py --promote``: the candidate
    clears triage and ordinary picker contracts before the write, and a fresh
    post-write snapshot must prove the expected Ready state.
    """
    current = current_selection or select(agent, family)
    if current["work"]["type"] != "idle":
        return None
    repo_slug_snapshot = get_repo_slug()
    projects_snapshot: Any = (get_repo_projects(repo_slug_snapshot) if repo_slug_snapshot
                              and post_snapshot_out is not None else _UNSET)
    fresh, repo_slug = _idle_backlog_candidate(
        agent, issues_snapshot=issues_snapshot, prs_snapshot=prs_snapshot,
        repo_slug_snapshot=repo_slug_snapshot, projects_snapshot=projects_snapshot,
        snapshot_out=post_snapshot_out)
    if fresh is None or not repo_slug:
        return None

    if not fresh.get("updatedAt"):
        print("[WARN] Candidate update time is missing; refusing auto-triage.", file=sys.stderr)
        return None
    if select(agent, family)["work"]["type"] != "idle":
        print("[WARN] Picker is no longer idle; refusing auto-triage.", file=sys.stderr)
        return None

    number = fresh["number"]
    if not update_status(number, "Ready", require_board=True, expected_status="Backlog",
                         require_unclaimed=True, expected_updated_at=fresh.get("updatedAt")):
        raise AutoTriageError(f"qualified Backlog issue #{number} could not be promoted cleanly")
    post, _post_slug = _idle_backlog_candidate(
        agent, expected_ready_issue=number, repo_slug_snapshot=repo_slug_snapshot,
        projects_snapshot=projects_snapshot, snapshot_out=post_snapshot_out)
    post_labels = {name.lower() for name in issue_label_names(post or {})}
    post_statuses = {name for name in post_labels if name.startswith("status:")}
    post_agents = {name for name in post_labels if name.startswith("agent:")}
    expected_labels = {name.lower() for name in issue_label_names(fresh)
                       if not name.lower().startswith("status:")} | {"status:ready"}
    stable = post is not None and all(
        post.get(field) == fresh.get(field) for field in
        ("number", "body", "author", "editor", "authorAssociation", "editorAssociation",
         "trustIdentityResolved"))
    if post is None:
        post_selection = {"work": {"type": "idle"}}
    elif post_snapshot_out is None:
        post_selection = select(agent, family)
    else:
        post_selection = select(agent, family, prs_snapshot=post_snapshot_out.get("prs", _UNSET),
                                issues_snapshot=post_snapshot_out.get("issues", _UNSET))
    post_work = post_selection["work"]
    if post_snapshot_out is not None:
        post_snapshot_out["_selection"] = post_selection
    if (post_statuses != {"status:ready"} or post_agents or post_labels != expected_labels
            or not stable or post is None or post.get("number") != number
            or post_work.get("type") != "issue" or post_work.get("issue") != number
            or _post_slug != repo_slug):
        rolled_back = update_status(number, "Backlog", require_board=True,
                                    expected_status="Ready", require_unclaimed=True)
        raise AutoTriageError(f"issue #{number} failed authoritative readback; "
                              f"rollback {'succeeded' if rolled_back else 'FAILED'}")
    print(f"[INFO] Picker promoted qualified Backlog issue #{number} to Ready.", file=sys.stderr)
    return number


def promote_one_idle_backlog_issue(
    agent: str, family: str | None = None, *, current_selection: dict[str, Any] | None = None,
    issues_snapshot: Any = _UNSET, prs_snapshot: Any = _UNSET,
    post_snapshot_out: dict[str, Any] | None = None,
) -> int | None:
    """Serialize one auto-triage transition against other lifecycle writes."""
    with merge_pr.repository_merge_lock() as (locked, message):
        if not locked:
            print(f"[WARN] Auto-triage deferred: {message}.", file=sys.stderr)
            return None
        return _promote_one_idle_backlog_issue_locked(
            agent, family, current_selection=current_selection,
            issues_snapshot=issues_snapshot, prs_snapshot=prs_snapshot,
            post_snapshot_out=post_snapshot_out)


def _reap_stale_claims(hours: int, prs_snapshot: Any, issues_snapshot: Any) -> tuple[Any, Any]:
    """Release idle issue and merge claims, refreshing snapshots only if any moved."""
    skipped = ("PR snapshot unavailable" if prs_snapshot is None
               else "PR snapshot is REST-degraded" if isinstance(prs_snapshot, DegradedPrSnapshot)
               else "Open issue snapshot unavailable" if issues_snapshot is None else "")
    if skipped:
        print(f"[WARN] {skipped}; stale-claim reaping skipped.", file=sys.stderr)
        return prs_snapshot, issues_snapshot
    try:
        released_merges = reap_stale_merges(hours, prs_snapshot=prs_snapshot)
        open_prs = [pr for pr in prs_snapshot if not is_merged(pr)]
        released_issues = reap_stale_claims(issues_snapshot, hours, open_prs_snapshot=open_prs)
        if released_merges or released_issues:
            # Reaping mutates, so only that uncommon path pays for a fresh snapshot.
            return list_work_prs(), list_open_issues()
    except Exception as err:
        print(f"[WARN] Autonomous claim reap encountered error: {err}", file=sys.stderr)
    return prs_snapshot, issues_snapshot


def _claim_selected_work(args, res: dict[str, Any], work: dict[str, Any]) -> None:
    """Claim the selected merge or issue in place, recording why one failed."""
    if work["type"] == "merge":
        work["claimed"] = False
        for candidate in res.get("mergeable_detail") or []:
            rc = claim_merge(candidate["pr"], args.agent)
            if rc == EXIT_OK:
                work.update({"pr": candidate["pr"], "title": candidate["title"],
                             "head_sha": candidate.get("head_sha"), "claimed": True})
                break
            if rc == EXIT_CONFLICT:
                print(f"[INFO] PR #{candidate['pr']} merge was taken; trying the next one.",
                      file=sys.stderr)
                continue
            work["claim_result"] = "error"
            break
        if not work["claimed"] and "claim_result" not in work:
            work["claim_result"] = "all_taken"
    elif work["type"] == "issue" and not work.get("resuming"):
        from claim_issue import claim_issue
        issue_rc = claim_issue(work["issue"], args.agent)
        work["claimed"] = issue_rc == EXIT_OK
        if not work["claimed"]:
            work["claim_result"] = "conflict" if issue_rc == EXIT_CONFLICT else "error"


def _print_selection(args, res: dict[str, Any], work: dict[str, Any], claim_failed: bool) -> None:
    """Render one selection for a human terminal."""
    print("=== Aru_Agentic_SDLC: next work ===")
    print(f"👤 {args.agent}" + (f" ({args.family})" if args.family else " (family unset)"))
    if claim_failed:
        number = work.get("issue") or work.get("pr")
        print(f"⛔ Could not claim {work['type']} #{number}; "
              f"result={work.get('claim_result', 'error')}. No work was started.")
    elif work["type"] == "feedback":
        print(f"🔁 Your PR #{work['pr']} has requested changes — address it before taking new work.")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "merge":
        print(f"🔀 Merge PR #{work['pr']} (Definition of Done passed)")
        print(f"   → merge_pr.py --pr {work['pr']}  (never gh pr merge)")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "issue":
        print(f"🛠️  {'Resume' if work.get('resuming') else 'Implement'} issue #{work['issue']}")
        print(f"   → {work['skill']}: {work['title']}")
    else:
        print(f"⛔ Picker error: {work.get('reason')}" if work["type"] == "error"
              else "✨ Nothing to do: no mergeable PR, remediation, or claimable issue.")

    if res.get("merge_skipped"):
        print("\nMerge candidates not offered to you:")
        for item in res["merge_skipped"]:
            print(f"  #{item['number']}: {item['why']}")
    if work["type"] not in {"issue", "merge"} and res["claimable_issues"]:
        print(f"\nIssues waiting: {res['claimable_issues']}")


def verified_lane_agents(requested: list[str], checkout: Path) -> tuple[list[str], str]:
    """Fail closed unless every explicit lane is fresh and available here."""
    if not requested or len(requested) != len(set(requested)):
        raise PresenceError("--lane-agent values must be distinct")
    records = PresenceStore().query_project(checkout_path=str(checkout.resolve()), expire=True)
    by_id = {record.agent_id: record for record in records}
    missing = [agent for agent in requested if agent not in by_id]
    unavailable = [agent for agent in requested if agent in by_id
                   and by_id[agent].availability not in {"available", "returned"}]
    if missing or unavailable:
        detail = []
        if missing:
            detail.append("unregistered: " + ", ".join(sorted(missing)))
        if unavailable:
            detail.append("not available: " + ", ".join(sorted(unavailable)))
        raise PresenceError("lane verification failed (" + "; ".join(detail) + ")")
    projects = {by_id[agent].project_id for agent in requested}
    if len(projects) != 1:
        raise PresenceError("lane agents do not share one project")
    return sorted(requested), projects.pop()


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Pick the next work item for one agent.")
    parser.add_argument("--agent", required=False, default=None, help="Agent id. Omit to use "
                        "ARU_AGENT_ID or a stable machine, checkout, and family fingerprint.")
    parser.add_argument("--family", default=None, help="This agent's model family (anthropic, "
                        "openai, ...). Omitting it means every PR looks cross-family.")
    parser.add_argument("--claim", action="store_true", help="Claim the selected work item")
    parser.add_argument("--promote-idle", action="store_true",
                        help="Promote one qualified Backlog item when idle without claiming it")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--lanes", type=int, default=1, metavar="N",
                        help="Upper bound for an explicit multi-lane tick")
    parser.add_argument("--lane-agent", action="append", default=[], metavar="AGENT_ID",
                        help="Verified agent identity for one lane; repeat exactly N times")
    parser.add_argument("--worker-path", type=Path, default=DEFAULT_WORKER_PATH,
                        help="Worker handoff path used by the matching worker lifecycle commands")
    parser.add_argument("--reap-after", type=int, default=DEFAULT_REAP_AFTER_HOURS, metavar="HOURS",
                        help="Release issue and merge claims idle longer than HOURS "
                        "(default: 4h; 0 disables)")
    args = parser.parse_args()

    lane_mode = bool(args.lane_agent or args.lanes != 1)
    if args.lanes < 1 or (lane_mode and len(args.lane_agent) != args.lanes):
        print("[ERROR] --lanes must be positive and match distinct --lane-agent values.",
              file=sys.stderr)
        return 1
    if not lane_mode:
        rc = _resolve_identity(args)
        if rc is not None:
            return rc
    family = (args.family or "").lower() or None

    lane_agents: list[str] = []
    project_id = ""
    if lane_mode:
        try:
            lane_agents, project_id = verified_lane_agents(args.lane_agent, Path.cwd())
        except PresenceError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

    prs_snapshot: list[dict[str, Any]] | None | object = _UNSET
    issues_snapshot: list[dict[str, Any]] | None | object = _UNSET
    if args.reap_after > 0:
        prs_snapshot = list_work_prs()
        if prs_snapshot is not None and not isinstance(prs_snapshot, DegradedPrSnapshot):
            issues_snapshot = list_open_issues()
        prs_snapshot, issues_snapshot = _reap_stale_claims(args.reap_after, prs_snapshot,
                                                           issues_snapshot)

    if lane_mode:
        snapshot = build_inventory_snapshot(
            prs_snapshot=prs_snapshot, issues_snapshot=issues_snapshot,
        )
        res = select_lanes_from_snapshot(snapshot, lane_agents, family)
        try:
            apply_worker_handoffs(
                res["work_items"], store=WorkerHandoffStore(args.worker_path),
                project_id=project_id,
            )
        except PresenceError as exc:
            res["handoff_warning"] = str(exc)
        if args.claim:
            res["claim_status"] = claim_lane_items(res["work_items"])
        command_failed = (
            res["work"]["type"] == "error" and not res["work_items"]
        ) or res["claim_status"] in {"partial", "failed"}
        if args.as_json:
            print(json.dumps(res, indent=2))
        else:
            print(json.dumps(res, indent=2))
        return 1 if command_failed else None

    res = select(args.agent, family, prs_snapshot=prs_snapshot, issues_snapshot=issues_snapshot)
    work = res["work"]

    # A read-only picker call remains read-only. A loop asking to claim work may
    # promote one mechanically qualified Backlog item, then immediately run the
    # ordinary selector again so normal claim arbitration still applies.
    if (args.claim or args.promote_idle) and work["type"] == "idle":
        post: dict[str, Any] = {}
        try:
            promoted = promote_one_idle_backlog_issue(
                args.agent, family, current_selection=res, issues_snapshot=issues_snapshot,
                prs_snapshot=prs_snapshot, post_snapshot_out=post)
        except AutoTriageError as exc:
            promoted = None
            res["work"] = work = {"type": "error", "skill": None, "reason": str(exc)}
        if promoted is not None:
            res = post.get("_selection") or select(args.agent, family,
                                                   prs_snapshot=post.get("prs", _UNSET),
                                                   issues_snapshot=post.get("issues", _UNSET))
            res["auto_promoted_issue"] = promoted
            work = res["work"]

    if args.claim:
        _claim_selected_work(args, res, work)

    claim_failed = bool(args.claim and work["type"] in {"issue", "merge"}
                        and not work.get("resuming") and not work.get("claimed", False))
    command_failed = claim_failed or work["type"] == "error"

    if args.as_json:
        print(json.dumps(res, indent=2))
        return 1 if command_failed else None
    _print_selection(args, res, work, claim_failed)
    return 1 if command_failed else None


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
