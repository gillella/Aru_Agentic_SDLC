#!/usr/bin/env python3
# line-ceiling: 800
"""
create_pr.py - Opens a Pull Request pre-populated with issue linking ('Closes #X').

Also stamps who wrote it. Every agent authenticates as the same GitHub user, so
`github.actor` cannot distinguish them - the same reason claiming needs its own
`agent:<id>` label. Review eligibility depends on knowing the author, so the
identity has to be on the PR itself:

  author:<agent-id>   never review your own work
  family:<family>     prefer a reviewer whose model blind spots differ

Family, not tool: Cursor running Sonnet has the same blind spots as Claude Code
running Sonnet, so "a different tool" is not necessarily a different reviewer.
"""

import argparse
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from common import (
    VERIFICATION_EVIDENCE_END,
    VERIFICATION_EVIDENCE_SCHEMA,
    VERIFICATION_EVIDENCE_START,
    ensure_label,
    get_current_branch,
    get_current_commit,
    get_issue,
    run_cmd,
)

NEEDS_REVIEW_LABEL = "needs-review"
REVIEW_SERVICES = ("coderabbit", "sourcery", "codeant")
REVIEW_LABEL_PREFIX = "review:"

# Kept explicit rather than free-form: a typo like "anthropc" would silently
# make every PR look cross-family to the picker, which is the one failure mode
# this label exists to prevent.
MODEL_FAMILIES = ("anthropic", "openai", "google", "meta", "mistral", "xai", "human")


CAPACITY_MAX_AGE_SECONDS = 3600
CAPACITY_EVIDENCE_START = "<!-- ARU:REVIEW-CAPACITY-EVIDENCE:START -->"
CAPACITY_EVIDENCE_END = "<!-- ARU:REVIEW-CAPACITY-EVIDENCE:END -->"

# v2 carries what a merge-time auditor needs and v1 did not: the issue set the
# selection is bound to, and a provenance block naming the bounded capacity
# snapshot the eligible pool was computed from. Without those, merge_pr.py can
# only recompute the legacy full-pool rotation, which rejects every legitimate
# capacity-rerouted assignment -- the failure #403 exists to close. The version
# is bumped rather than reused because a v1 comment is a materially weaker
# artifact, and a gate that read it as v2 would be trusting fields that are
# simply absent.
CAPACITY_SELECTION_SCHEMA = "aru.review-capacity-selection.v2"

# The digest binds the parts of one selection together: candidates, the
# bounded exclusion records, the eligible pool they produce, the issue set,
# and the service chosen. It is a consistency seal, not a signature -- there
# is no key here, so it does not stop an actor who rewrites the whole comment.
# What it does stop is the realistic tamper: editing one field of a real
# assignment (swap the selected service, drop an exclusion, add an issue) and
# leaving the rest intact.
CAPACITY_DIGEST_FIELDS = ("as_of", "candidates", "eligible", "excluded", "issues", "selected")


def review_service_for_issue(issue_id: int) -> str:
    """Stable approximately-even authority assignment for one issue number.

    Capacity-unaware by design: it is the full-pool rotation, and stays a
    pure function of issue_id alone. merge_pr.py recomputes it from every
    linked issue for PRs that carry no capacity-selection evidence -- the
    legacy path -- and audit_review_assignment.py reports against it.
    select_review_service() below is where capacity evidence is applied, and
    a PR whose assignment came from there is proved against that evidence
    instead, since a real exclusion can legitimately move the answer.
    """
    return REVIEW_SERVICES[(issue_id - 1) % len(REVIEW_SERVICES)]


def review_label_for_service(service: str) -> str:
    return f"{REVIEW_LABEL_PREFIX}{service}"


def capacity_selection_digest(evidence: Dict) -> str:
    """Seal the fields one selection was actually computed from.

    Imported by merge_pr.py so the value is produced and re-derived by the
    same code. A second implementation of "canonicalize and hash" on the gate
    side would eventually disagree with this one over key order or separators,
    and a digest check that can disagree with itself is worse than none: it
    fails honest assignments while a tampered one that happens to hit the
    other convention passes.
    """
    payload = json.dumps(
        {field: evidence.get(field) for field in CAPACITY_DIGEST_FIELDS},
        sort_keys=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_review_service(issue_ids, *, as_of=None, snapshot: Optional[Dict] = None) -> Dict:
    """Deterministic, approximately-even selection over the eligible pool.

    Loads fresh known-unavailable evidence from the shared cross-repository
    ledger that scripts/audit_review_service_capacity.py owns, and excludes
    only services with a fresh, well-formed, unexpired cooldown,
    quota-exhaustion, outage, or unavailable record. When nothing excludes
    anyone -- an empty ledger, or only stale/malformed entries -- the
    eligible pool is the full REVIEW_SERVICES tuple in order, and
    `eligible[(issue_id - 1) % len(eligible)]` is exactly
    review_service_for_issue(issue_id): the original rotation is unchanged
    until real evidence exists.

    ``issue_ids`` is every issue the PR closes, not just the one named on the
    command line, because the merge gate revalidates the selection against
    all of them. A set whose members rotate to different services has no
    single auditable answer, so nothing is selected and the PR stays draft --
    the same fail-closed outcome the gate would reach, reached earlier and
    with a rationale attached.
    """
    from audit_review_service_capacity import (
        UNAVAILABILITY_SCHEMA,
        audit_unavailability,
        load_unavailability_snapshot,
    )

    issues = [int(num) for num in issue_ids]
    if not issues:
        raise ValueError("select_review_service requires at least one issue id")
    if snapshot is None:
        snapshot = load_unavailability_snapshot()
    report = audit_unavailability(snapshot, as_of=as_of, max_age_seconds=CAPACITY_MAX_AGE_SECONDS)
    excluded_map = report["unavailable"]
    eligible = [service for service in REVIEW_SERVICES if service not in excluded_map]
    picks = {eligible[(num - 1) % len(eligible)] for num in issues} if eligible else set()
    if len(picks) == 1:
        selected = picks.pop()
        rationale = (
            f"selected '{selected}' from {len(eligible)} eligible service(s) via "
            f"(issue_id - 1) mod {len(eligible)} rotation over the eligible pool, "
            f"agreeing across issue(s) {', '.join('#' + str(num) for num in issues)}"
        )
    elif picks:
        selected = None
        rationale = (
            "linked issues rotate to different services over the eligible pool "
            f"({', '.join(sorted(picks))}); no single service can be authoritative "
            "for all of them, so the PR stays draft"
        )
    else:
        selected = None
        rationale = "no configured review service is currently eligible; PR stays draft"
    evidence = {
        "schema": CAPACITY_SELECTION_SCHEMA,
        "as_of": report["as_of"],
        "candidates": list(REVIEW_SERVICES),
        "eligible": eligible,
        "excluded": [
            {"service": service, **excluded_map[service]}
            for service in REVIEW_SERVICES if service in excluded_map
        ],
        "issues": issues,
        "selected": selected,
        "rationale": rationale,
    }
    # The provenance block names the bounded snapshot the pool came from and
    # how it was bounded, so the gate can replay the exclusions under the same
    # window instead of trusting the eligible list it was handed.
    evidence["snapshot"] = {
        "source": UNAVAILABILITY_SCHEMA,
        "observed_at": report["as_of"],
        "max_age_seconds": CAPACITY_MAX_AGE_SECONDS,
        "excluded_count": len(evidence["excluded"]),
        "digest": capacity_selection_digest(evidence),
    }
    return evidence


def render_capacity_evidence(evidence: Dict) -> str:
    """Renders auditable candidate/exclusion/selection rationale as a PR comment."""
    payload = json.dumps(evidence, indent=2, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "## Review-capacity assignment evidence\n\n"
        f"{CAPACITY_EVIDENCE_START}\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
        f"{CAPACITY_EVIDENCE_END}\n"
    )


# Deliberately identical to merge_pr.linked_issues(): the assignment is bound
# to the issue set the merge gate will recompute from, and two regexes that
# disagree about what "Closes #12" means would bind evidence to one set and
# validate it against another.
CLOSES_ISSUE_RE = re.compile(r"\bcloses\s+#(\d+)\b", re.IGNORECASE)


def linked_issues_for_pr(pr_ref: str) -> Optional[List[int]]:
    """Every issue the live PR body closes, in order, or None if unreadable.

    Read from the PR rather than assumed from --issue because a body may
    legitimately close several issues, and the merge gate validates the
    selection against all of them. Binding the evidence to a narrower set
    than the gate will read produces a PR that is correctly routed and
    permanently unmergeable, which is exactly the failure #403 removes.
    """
    code, out, err = run_cmd(["gh", "pr", "view", pr_ref, "--json", "body"], check=False)
    if code != 0:
        print(f"[ERROR] Could not read the body of PR {pr_ref}: "
              f"{err.strip() or f'gh exited {code}'}", file=sys.stderr)
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        print(f"[ERROR] Could not parse the body of PR {pr_ref}: response was not JSON",
              file=sys.stderr)
        return None
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, str):
        print(f"[ERROR] Could not parse the body of PR {pr_ref}: no body in response",
              file=sys.stderr)
        return None
    seen, issues = set(), []
    for match in CLOSES_ISSUE_RE.finditer(body):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            issues.append(num)
    return issues


class ReviewAssignmentLookupError(RuntimeError):
    """The live review assignment could not be read, or is ambiguous.

    Deliberately distinct from `None`, which means "read successfully, no
    assignment yet". Collapsing the two is what let a failed `gh pr view`, a
    malformed response, or a PR carrying two review:* labels look like an
    unassigned PR: finalization would then select a service and add another
    authority label, switching or duplicating a reviewer the routing contract
    promises is immutable. Every such state fails closed instead.
    """


def existing_review_assignment(pr_ref: str) -> Optional[str]:
    """Reads live PR labels for an already-assigned review service, if any.

    Authority is immutable once assigned: every (re)assignment path calls
    this first, so a later capacity change, or a retry of a PR left waiting
    for capacity, can never silently switch reviewers on a PR that already
    has one. Returns None only when the labels were read successfully and
    none of them is a review:* label; anything else raises
    ReviewAssignmentLookupError so the caller stops rather than guesses.
    """
    code, out, err = run_cmd(["gh", "pr", "view", pr_ref, "--json", "labels"], check=False)
    if code != 0:
        raise ReviewAssignmentLookupError(
            f"could not read labels for PR {pr_ref}: {err.strip() or f'gh exited {code}'}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        raise ReviewAssignmentLookupError(
            f"could not parse labels for PR {pr_ref}: response was not JSON")
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, list):
        raise ReviewAssignmentLookupError(
            f"could not parse labels for PR {pr_ref}: no labels array in response")
    names = {label.get("name") for label in labels if isinstance(label, dict)}
    assigned = [
        service for service in REVIEW_SERVICES
        if review_label_for_service(service) in names
    ]
    if len(assigned) > 1:
        raise ReviewAssignmentLookupError(
            f"PR {pr_ref} carries conflicting review labels "
            f"({', '.join(review_label_for_service(s) for s in assigned)}); "
            "exactly one service may be authoritative. Remove the extras.")
    return assigned[0] if assigned else None


def collect_verification_evidence(
    commands: Optional[List[str]] = None,
    head_sha: str = "",
) -> Dict:
    """Runs configured verification commands and returns a versioned record."""
    records = []
    for command in commands or []:
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        if not argv:
            records.append({
                "command": ["<invalid-command>"],
                "duration_seconds": 0.0,
                "exit_code": 2,
                "status": "failed",
            })
            continue
        code, _, _ = run_cmd(argv, check=False, evidence=records)
        print(f"{'✅' if code == 0 else '❌'} Verification ({code}): {records[-1]['command']}")

    if not records:
        status = "not_run"
    elif all(record["exit_code"] == 0 for record in records):
        status = "passed"
    else:
        status = "failed"
    return {
        "commands": records,
        "head_sha": head_sha or get_current_commit(),
        "schema": VERIFICATION_EVIDENCE_SCHEMA,
        "status": status,
    }


def render_verification_evidence(evidence: Dict) -> str:
    """Renders stable marker-delimited JSON for machine parsing."""
    payload = serialize_verification_evidence(evidence)
    return (
        "\n\n<details>\n"
        "<summary>Local verification evidence</summary>\n\n"
        f"{VERIFICATION_EVIDENCE_START}\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
        f"{VERIFICATION_EVIDENCE_END}\n"
        "</details>"
    )


def serialize_verification_evidence(evidence: Dict) -> str:
    """Serializes JSON without allowing payload text to become raw delimiters."""
    return json.dumps(evidence, indent=2, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")


def replace_verification_evidence(body: str, evidence: Dict) -> Optional[str]:
    """Replaces exactly one evidence payload while preserving the PR prose."""
    if (body.count(VERIFICATION_EVIDENCE_START) != 1
            or body.count(VERIFICATION_EVIDENCE_END) != 1):
        return None
    if body.index(VERIFICATION_EVIDENCE_START) > body.index(VERIFICATION_EVIDENCE_END):
        return None
    before, _marker, remainder = body.partition(VERIFICATION_EVIDENCE_START)
    _old_payload, _end_marker, after = remainder.partition(VERIFICATION_EVIDENCE_END)
    payload = serialize_verification_evidence(evidence)
    return (
        f"{before}{VERIFICATION_EVIDENCE_START}\n"
        f"```json\n{payload}\n```\n"
        f"{VERIFICATION_EVIDENCE_END}{after}"
    )


def refresh_pr_evidence(pr_ref: str, verification_commands: List[str]) -> bool:
    """Reruns verification and refreshes evidence for the exact live PR head."""
    local_head = get_current_commit()
    code, out, err = run_cmd(
        ["gh", "pr", "view", str(pr_ref), "--json", "body,headRefOid"],
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not read PR #{pr_ref}: {err}", file=sys.stderr)
        return False
    try:
        pr = json.loads(out)
    except json.JSONDecodeError:
        print(f"[ERROR] Could not parse PR #{pr_ref} metadata.", file=sys.stderr)
        return False
    if not local_head or pr.get("headRefOid") != local_head:
        print(
            "[ERROR] Checked-out commit does not match the live PR head; "
            "check out the PR worktree before refreshing evidence.",
            file=sys.stderr,
        )
        return False

    evidence = collect_verification_evidence(verification_commands, local_head)
    if get_current_commit() != local_head:
        print("[ERROR] HEAD changed while verification was running.", file=sys.stderr)
        return False
    code, fresh_out, err = run_cmd(
        ["gh", "pr", "view", str(pr_ref), "--json", "body,headRefOid"],
        check=False,
    )
    try:
        fresh_pr = json.loads(fresh_out) if code == 0 else {}
    except json.JSONDecodeError:
        fresh_pr = {}
    if fresh_pr.get("headRefOid") != local_head:
        print(
            "[ERROR] The remote PR head changed while verification was running; rerun refresh.",
            file=sys.stderr,
        )
        return False

    updated_body = replace_verification_evidence(fresh_pr.get("body") or "", evidence)
    if updated_body is None:
        print(
            "[ERROR] PR body must contain exactly one complete verification evidence block.",
            file=sys.stderr,
        )
        return False
    code, _, err = run_cmd(
        ["gh", "pr", "edit", str(pr_ref), "--body", updated_body],
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not refresh PR evidence: {err}", file=sys.stderr)
        return False
    print(f"✅ Refreshed verification evidence for PR #{pr_ref} at {local_head}.")
    return True


def apply_identity(pr_ref: str, agent: str = "", family: str = "") -> bool:
    """Labels the PR with its author agent and model family.

    Returns False if the author label could not be attached.

    This used to be best-effort, on the reasoning that a PR which opened
    successfully should not be reported as failed over a label. That held
    while an unstamped PR merely degraded review routing. It no longer does:
    merge_pr.py reads `author:` to tell a peer review from a self-review, and
    an unstamped PR takes the fallback branch where any review counts. Silently
    producing one opens the hole the gate exists to close, so the caller is
    told and the operator is given the command to fix it.
    """
    labels = []
    if agent:
        name = f"author:{agent}"
        ensure_label(name, "1d76db", f"PR authored by agent '{agent}'")
        labels.append(name)
    if family:
        name = f"family:{family}"
        ensure_label(name, "d4a27f", f"PR authored by a {family}-family model")
        labels.append(name)
    if not labels:
        return True

    cmd = ["gh", "pr", "edit", pr_ref]
    for label in labels:
        cmd += ["--add-label", label]
    code, _, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Could not stamp {', '.join(labels)}: {err.strip()}", file=sys.stderr)
        print("[ERROR] The PR exists but is unstamped, so the merge gate cannot tell a "
              "peer review from a self-review on it.", file=sys.stderr)
        print(f"[ERROR] Fix with: gh pr edit {pr_ref} "
              f"{' '.join('--add-label ' + name for name in labels)}", file=sys.stderr)
        return False
    print(f"🏷️  Stamped {', '.join(labels)}")
    return True


def enqueue_review(pr_ref: str) -> bool:
    """Mark a newly opened PR as claimable review work immediately.

    This is invocation, not a second review path: no bot posts a review.
    The picker still requires a distinct agent. Failures here are warnings
    because the PR already exists and identity is already stamped.
    """
    queued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ensure_label(
        NEEDS_REVIEW_LABEL,
        "5319e7",
        "Opened; claimable as review work (not a bot review)",
    )
    code, _, err = run_cmd(
        ["gh", "pr", "edit", pr_ref, "--add-label", NEEDS_REVIEW_LABEL],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not apply {NEEDS_REVIEW_LABEL}: {err.strip()}",
              file=sys.stderr)
    body = (
        "## Review queue\n"
        f"review-queued-at: {queued_at}\n"
        "\n"
        "This PR is claimable review work for a distinct agent. "
        "No automated account should post a review.\n"
    )
    code, _, err = run_cmd(
        ["gh", "pr", "comment", pr_ref, "--body", body],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not record review-queued-at: {err.strip()}",
              file=sys.stderr)
        return False
    print(f"🔍 Enqueued as review work (review-queued-at: {queued_at})")
    return True


def _acquire_review_assignment(pr_ref: str, issue_id: int):
    """Select, record, and claim exactly one review service for a draft PR.

    Returns (ok, service): (True, service) claimed it, (True, None) correctly
    left the PR waiting for capacity, and (False, None) stop -- nothing safe
    was done and nothing should follow. The three outcomes are kept distinct
    because "no service" and "could not tell" must not share a return value;
    conflating them is what let a failed lookup look like an unassigned PR.
    """
    issues = linked_issues_for_pr(pr_ref)
    if issues is None:
        return False, None
    if issue_id not in issues:
        print(f"[ERROR] PR {pr_ref} does not close issue #{issue_id} "
              f"(body links {issues or 'no issues'}); refusing to bind a review "
              "assignment to an issue set the merge gate will not recompute.",
              file=sys.stderr)
        return False, None
    evidence = select_review_service(issues)
    # The evidence comment is the only durable record of why a PR was assigned
    # or left waiting, so a PR must never reach either state without it.
    # Failing to post it is a failed finalization, not a warning.
    comment_code, _, comment_err = run_cmd(
        ["gh", "pr", "comment", pr_ref, "--body", render_capacity_evidence(evidence)],
        check=False,
    )
    if comment_code != 0:
        print(f"[ERROR] Could not record capacity evidence for PR {pr_ref}: "
              f"{comment_err.strip()}; not assigning without it.", file=sys.stderr)
        return False, None

    service = evidence["selected"]
    if service is None:
        print(
            f"⏳ No review service is assignable for PR {pr_ref}; left in draft "
            f"with bounded waiting-for-capacity evidence ({evidence['rationale']}). "
            "Retry with --finalize-review once capacity evidence changes.",
            file=sys.stderr,
        )
        return True, None

    label = review_label_for_service(service)
    ensure_label(label, "0e8a16", f"Authoritative review service: {service}")
    # Re-read immediately before the write: the gap since the caller's lookup
    # is exactly where a second concurrent finalizer can land its own label,
    # and two review:* labels means no single authority.
    try:
        if existing_review_assignment(pr_ref) is not None:
            print(f"[ERROR] PR {pr_ref} was assigned concurrently; leaving that "
                  "assignment authoritative.", file=sys.stderr)
            return False, None
    except ReviewAssignmentLookupError as exc:
        print(f"[ERROR] Refusing to assign PR {pr_ref}: {exc}", file=sys.stderr)
        return False, None

    code, _, err = run_cmd(["gh", "pr", "edit", pr_ref, "--add-label", label], check=False)
    if code != 0:
        print(f"[ERROR] Could not apply {label}: {err.strip()}", file=sys.stderr)
        return False, None

    # And re-read after: a racing finalizer that wrote between the check above
    # and this add-label leaves both labels present, which the lookup now
    # reports as a conflict instead of silently picking one.
    try:
        confirmed = existing_review_assignment(pr_ref)
    except ReviewAssignmentLookupError as exc:
        print(f"[ERROR] PR {pr_ref} has ambiguous authority after assignment: {exc}",
              file=sys.stderr)
        return False, None
    if confirmed != service:
        print(f"[ERROR] PR {pr_ref} resolved to '{confirmed}' after assigning "
              f"'{service}'; not marking ready.", file=sys.stderr)
        return False, None
    return True, service


def finalize_review_assignment(pr_ref: str, issue_id: int) -> bool:
    """Assign exactly one eligible review-pool service while the PR is draft.

    Idempotent and safe to retry: if a review:* label is already on the PR,
    authority is stable and this only resumes marking it ready (recovering
    from a prior ready/trigger failure) rather than recomputing or switching
    the assignment. Otherwise it loads fresh capacity evidence, records
    candidate/exclusion/selection rationale as an auditable PR comment, and
    either assigns the selected service or -- if none is assignable -- leaves
    the PR in draft with that evidence as the bounded waiting-for-capacity
    record. Call this again later to retry a waiting PR.
    """
    try:
        service = existing_review_assignment(pr_ref)
    except ReviewAssignmentLookupError as exc:
        print(f"[ERROR] Refusing to finalize PR {pr_ref}: {exc}", file=sys.stderr)
        return False
    if service is not None:
        label = review_label_for_service(service)
        print(f"🔒 {label} already assigned; authority stays stable, resuming finalization.")
    else:
        ok, service = _acquire_review_assignment(pr_ref, issue_id)
        if not ok:
            return False
        if service is None:
            return True
        label = review_label_for_service(service)

    code, _, err = run_cmd(["gh", "pr", "ready", pr_ref], check=False)
    if code != 0:
        print(f"[ERROR] Could not mark PR ready after assigning {label}: {err.strip()}", file=sys.stderr)
        return False
    if service == "codeant":
        code, _, err = run_cmd(
            ["gh", "pr", "comment", pr_ref, "--body", "@codeant-ai: review"],
            check=False,
        )
        if code != 0:
            print(f"[ERROR] Could not trigger CodeAnt review: {err.strip()}", file=sys.stderr)
            rollback_code, _, rollback_err = run_cmd(
                ["gh", "pr", "ready", pr_ref, "--undo"],
                check=False,
            )
            if rollback_code != 0:
                print(
                    f"[ERROR] Could not restore draft state: {rollback_err.strip()}",
                    file=sys.stderr,
                )
            else:
                print("[INFO] Restored draft state; CodeAnt finalization can be retried.")
            return False
    print(f"🔍 Assigned {label} and marked PR ready")
    return True


def create_pr(issue_id: int, title: str = "", body: str = "",
              agent: str = "", family: str = "",
              verification_commands: Optional[List[str]] = None) -> bool:
    current_branch = get_current_branch()
    issue = get_issue(issue_id)

    if not title:
        title = issue["title"] if issue else f"Fix issue #{issue_id}"

    closure_footer = f"\n\nCloses #{issue_id}"
    base_body = body.strip() if body else f"Implementation for issue #{issue_id}."
    if VERIFICATION_EVIDENCE_START in base_body or VERIFICATION_EVIDENCE_END in base_body:
        print(
            "[ERROR] PR body contains reserved verification evidence markers.",
            file=sys.stderr,
        )
        return False
    head_sha = get_current_commit()
    if not head_sha:
        print("[ERROR] Could not determine the commit being verified.", file=sys.stderr)
        return False
    evidence = collect_verification_evidence(verification_commands, head_sha)
    if get_current_commit() != head_sha:
        print("[ERROR] HEAD changed while verification was running.", file=sys.stderr)
        return False
    full_body = base_body + render_verification_evidence(evidence) + closure_footer

    print(f"Opening Pull Request for branch '{current_branch}' linking 'Closes #{issue_id}'...")
    cmd = [
        "gh", "pr", "create", "--draft",
        "--title", title, "--body", full_body, "--head", current_branch,
    ]

    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to open PR: {err}", file=sys.stderr)
        return False

    print(f"✅ Pull Request created successfully:\n{out}")

    # `gh pr create` prints the URL, which gh accepts anywhere a PR number
    # would do. Falling back to the branch keeps this working if the output
    # format ever changes.
    pr_ref = out.strip().splitlines()[-1].strip() if out.strip() else current_branch
    # Reported as failure even though the PR opened: an unstamped PR is a
    # hole in the review gate, and a zero exit here would let a caller
    # move on believing the identity landed. Every created PR is finalized,
    # even with empty agent/family, so it always gets its authoritative
    # review-service label and leaves draft state.
    if not apply_identity(pr_ref, agent, family):
        return False
    return finalize_review_assignment(pr_ref, issue_id)


def main():
    parser = argparse.ArgumentParser(description="Create Pull Request linking an issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--title", type=str, default="", help="Pull Request Title")
    parser.add_argument("--body", type=str, default="", help="Pull Request Description Body")
    parser.add_argument(
        "--refresh-pr",
        type=int,
        default=0,
        help="Refresh the evidence block on an existing PR for the checked-out head.",
    )
    parser.add_argument(
        "--finalize-review",
        type=int,
        default=0,
        metavar="PR",
        help=(
            "Retry capacity-aware review assignment on an existing draft PR left "
            "waiting for capacity (or recover from a prior ready/trigger failure). "
            "Requires --issue for the linked issue number; authority is stable and "
            "unchanged if the PR is already assigned."
        ),
    )
    parser.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help=(
            "Verification command to execute and record; repeat for multiple commands. "
            "Commands are tokenized without a shell."
        ),
    )
    # Required, matching claim_issue.py. It was optional and defaulted to "",
    # so a caller who simply forgot produced a PR with no author:<id>, and
    # merge_pr.py then accepted any review on it - including a self-review.
    # An identity the gate depends on cannot be opt-in.
    parser.add_argument("--agent", type=str, required=True,
                        help="Authoring agent id; stamped as author:<id> for review eligibility")
    parser.add_argument("--model-family", type=str, default="", dest="family",
                        help=f"Authoring model family, one of: {', '.join(MODEL_FAMILIES)}")
    args = parser.parse_args()

    if args.finalize_review:
        # Retry only, never a second create path: --agent/--model-family are
        # unused here (the PR already carries author:/family: from creation),
        # but argparse still requires the token be present on the command line.
        # The rotation is `(issue_id - 1) % len(eligible)`, so a non-positive
        # issue number indexes backwards into the pool and yields a plausible
        # assignment for an issue that does not exist.
        if args.issue <= 0:
            print("[ERROR] --issue must be a positive issue number.", file=sys.stderr)
            sys.exit(1)
        ok = finalize_review_assignment(str(args.finalize_review), args.issue)
        sys.exit(0 if ok else 1)

    # `required=True` only proves the option token was typed; `--agent ""` gets
    # past it and reopens exactly the hole this script is meant to close - an
    # empty id means create_pr() skips apply_identity() and the PR lands
    # unstamped. This is a realistic accident, not a contrived one: `--agent
    # "$AGENT_ID"` with the variable unset produces precisely this.
    args.agent = args.agent.strip()
    if not args.agent:
        print("[ERROR] --agent is empty. It stamps author:<id>, which is what "
              "lets the merge gate tell a peer review from a self-review; an "
              "empty id would open an unstamped PR. If you passed a shell "
              "variable, it is unset.", file=sys.stderr)
        sys.exit(1)

    if args.family and args.family.lower() not in MODEL_FAMILIES:
        print(f"[ERROR] Unknown model family '{args.family}'. Valid values: "
              f"{', '.join(MODEL_FAMILIES)}", file=sys.stderr)
        sys.exit(1)

    # Left optional rather than required: family only steers cross-family
    # review preference, so its absence degrades routing without opening the
    # self-review hole that --agent guards. Loud, because a fleet that stops
    # passing it silently loses the reviewer-diversity property.
    if not args.family:
        print("[WARN] No --model-family given. Review routing cannot prefer a "
              "reviewer whose blind spots differ from this author's.", file=sys.stderr)

    if not args.verify_command:
        print(
            "[ERROR] At least one --verify-command is required. Repeat the option "
            "for every local test, lint, build, or validation command that the PR claims.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.refresh_pr:
        ok = refresh_pr_evidence(args.refresh_pr, args.verify_command)
        sys.exit(0 if ok else 1)

    ok = create_pr(
        args.issue,
        args.title,
        args.body,
        args.agent,
        args.family.lower(),
        verification_commands=args.verify_command,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
