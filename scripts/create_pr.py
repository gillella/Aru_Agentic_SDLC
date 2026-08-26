#!/usr/bin/env python3
# +12 for the #344 terminal merge lease guard.
# +85 for #472 complete-inventory review-pool selection and CodeAnt triggering.
# +200 for #472 serialized review reservation, draft-rollback ownership, and
# exactly-once CodeAnt triggering.
# line-ceiling: 863
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
from contextlib import contextmanager
import json
import shlex
import sys
import time
from typing import Dict, List, Optional

from common import (
    terminal_lease_refusal,
    terminal_merge_lease,

    VERIFICATION_EVIDENCE_END,
    VERIFICATION_EVIDENCE_SCHEMA,
    VERIFICATION_EVIDENCE_START,
    ensure_label,
    get_current_branch,
    get_current_commit,
    get_issue,
    get_repo_slug,
    run_cmd,
)
from github_pr_transport import open_pull_request_with_fallback
from github_inventory import open_pull_requests

REVIEW_LABEL_PREFIX = "review:"
REVIEW_SERVICES = ("coderabbit", "sourcery", "codeant")
EMERGENCY_AGENT_LABEL = "review:agent"
CODEANT_TRIGGER = "@codeant-ai: review"
# Bounded: a reservation that keeps losing to concurrent assignment fails
# closed rather than spinning against the pool.
REVIEW_RESERVATION_ATTEMPTS = 3
REVIEW_RESERVATION_SETTLE_ROUNDS = 2
REVIEW_RESERVATION_SETTLE_DELAY_S = 2.0
REVIEW_ASSIGNMENT_LOCK_REF = "refs/tags/aru-locks/review-assignment"

# Kept explicit rather than free-form: a typo like "anthropc" would silently
# make every PR look cross-family to the picker, which is the one failure mode
# this label exists to prevent.
MODEL_FAMILIES = (
    "anthropic", "openai", "codex", "google", "meta", "mistral", "xai", "human",
)


class ReviewAssignmentLookupError(RuntimeError):
    """The live review assignment could not be read, or is ambiguous.

    Deliberately distinct from `None`, which means "read successfully, no
    assignment yet". Collapsing the two is what let a failed `gh pr view`, a
    malformed response, or a PR carrying any non-canonical review label look
    like an unassigned PR: finalization would then add CodeRabbit beside an
    unknown or legacy authority. Every such state fails closed instead.
    """


def review_label_for_service(service: str) -> str:
    """Canonical authority label for one configured external service."""
    return f"{REVIEW_LABEL_PREFIX}{service}"


def _inventory_review_labels(pr) -> List[str]:
    """Review labels on one inventory record, failing closed on bad shapes."""
    labels = pr.get("labels") if isinstance(pr, dict) else None
    if not isinstance(labels, list):
        raise ReviewAssignmentLookupError("open-PR inventory has malformed labels")
    names = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise ReviewAssignmentLookupError("open-PR inventory has a malformed label")
        if label["name"].lower().startswith(REVIEW_LABEL_PREFIX):
            names.append(label["name"])
    return names


def review_capacity(exclude_pr: Optional[int] = None) -> Dict:
    """Complete open-PR authority load, plus who else holds each label.

    Returns ``{"counts": {service: int}, "holders": {service: [pr_number]}}``
    computed over the complete paginated inventory, with ``exclude_pr`` left
    out of both. Excluding one pull request is what lets a caller ask "what
    would the load look like without my own reservation" without a second
    round trip. Unreadable, malformed, unknown, or conflicting authority state
    fails closed instead of being mistaken for spare capacity.
    """
    slug = get_repo_slug()
    if not slug:
        raise ReviewAssignmentLookupError("could not resolve the repository for capacity")
    inventory = open_pull_requests(run_cmd, slug)
    if inventory is None:
        raise ReviewAssignmentLookupError("could not read the complete open-PR inventory")

    counts = {service: 0 for service in REVIEW_SERVICES}
    holders: Dict[str, List[int]] = {service: [] for service in REVIEW_SERVICES}
    canonical = {review_label_for_service(service): service for service in REVIEW_SERVICES}
    for pr in inventory:
        names = _inventory_review_labels(pr)
        number = pr.get("number") if isinstance(pr, dict) else None
        if not names:
            continue
        if names == [EMERGENCY_AGENT_LABEL]:
            # A terminal emergency agent is not part of the ordinary external
            # pool and therefore neither consumes nor creates provider capacity.
            continue
        if len(names) != 1 or names[0] not in canonical:
            raise ReviewAssignmentLookupError(
                f"open PR #{number if number is not None else 'unknown'} has ambiguous or "
                f"unsupported review authority: {', '.join(names)}")
        if exclude_pr is not None and number == exclude_pr:
            continue
        counts[canonical[names[0]]] += 1
        if isinstance(number, int):
            holders[canonical[names[0]]].append(number)
    return {"counts": counts, "holders": holders}


def _deterministic_choice(counts: Dict, issue_id: int) -> str:
    """The least-loaded service, ties rotated by issue number."""
    minimum = min(counts.values())
    eligible = [service for service in REVIEW_SERVICES if counts[service] == minimum]
    return eligible[(issue_id - 1) % len(eligible)]


def select_review_service(issue_id: int, exclude_pr: Optional[int] = None) -> str:
    """Choose the least-loaded external authority with a stable tie-break.

    Capacity is the number of open PRs carrying each sole canonical authority
    label. Ties rotate by issue number over the stable ``REVIEW_SERVICES``
    order, keeping simultaneous selections deterministic and approximately
    even without mutating any existing assignment.
    """
    if type(issue_id) is not int or issue_id <= 0:
        raise ReviewAssignmentLookupError("issue id must be a positive integer")
    return _deterministic_choice(review_capacity(exclude_pr)["counts"], issue_id)


def pr_number(pr_ref: str) -> Optional[int]:
    """The numeric pull request id behind a number, URL, or branch reference."""
    tail = str(pr_ref).rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        return int(tail)
    code, out, _ = run_cmd(["gh", "pr", "view", str(pr_ref), "--json", "number"], check=False)
    if code != 0:
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    number = payload.get("number") if isinstance(payload, dict) else None
    return number if isinstance(number, int) and number > 0 else None


@contextmanager
def review_assignment_lock(head: str):
    """Atomically serialize capacity selection through one server-side ref.

    Inventory settling detects contenders that have already become visible,
    but no bounded number of reads excludes one that lands immediately after
    the last read. GitHub reference creation is atomic: exactly one finalizer
    creates this transient tag ref, and every contender fails closed before it
    writes an authority label. A crash can leave a stale ref; that intentionally
    blocks new assignment until an operator verifies no finalizer is live and
    removes the named ref.
    """
    slug = get_repo_slug()
    if not slug or not isinstance(head, str) or not head:
        print("[ERROR] Could not resolve repository/head for the review-assignment lock.",
              file=sys.stderr)
        yield False
        return
    code, _, err = run_cmd(
        ["gh", "api", "--method", "POST", f"repos/{slug}/git/refs",
         "-f", f"ref={REVIEW_ASSIGNMENT_LOCK_REF}", "-f", f"sha={head}"],
        check=False)
    if code != 0:
        print("[CONFLICT] Could not acquire the atomic review-assignment lock; "
              f"another finalizer may be active: {err.strip()}.", file=sys.stderr)
        yield False
        return
    try:
        yield True
    finally:
        path = REVIEW_ASSIGNMENT_LOCK_REF.removeprefix("refs/")
        released, _, release_err = run_cmd(
            ["gh", "api", "--method", "DELETE", f"repos/{slug}/git/refs/{path}"],
            check=False)
        if released != 0:
            print(f"[ERROR] Review assignment completed but lock release failed: "
                  f"{release_err.strip()}. Verify no finalizer is live, then remove "
                  f"{REVIEW_ASSIGNMENT_LOCK_REF} before assigning another PR.",
                  file=sys.stderr)


def codeant_already_triggered(pr_ref: str) -> Optional[bool]:
    """Whether this pull request already carries the CodeAnt trigger comment.

    ``None`` when the comment history cannot be read. Finalization is
    resumable, so a retry that reposts the trigger enqueues a second CodeAnt
    run and can produce competing evidence records for one head; a retry that
    guesses "already sent" can leave the assignment with no review at all.
    The match is the exact trigger body, because CodeAnt's own usage-guide
    comments quote the same string.
    """
    code, out, _ = run_cmd(["gh", "pr", "view", str(pr_ref), "--json", "comments"],
                           check=False)
    if code != 0:
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        return None
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            return None
        if body.strip() == CODEANT_TRIGGER:
            return True
    return False


def _confirm_reservation(number: int, issue_id: int, selected: str) -> str:
    """Compare-and-set the applied label against a fresh complete inventory.

    Returns ``"held"``, ``"yield"``, or ``"unconfirmed"``. Selection reads a
    snapshot, so two finalizers can pick the same least-loaded service before
    either label lands and oversubscribe it while each pull request still looks
    individually valid. The label is therefore a tentative reservation, and
    this re-read is the set half: it recomputes the deterministic choice from
    the inventory with this pull request's own reservation removed.

    When two reservations collide, the contender with the highest pull request
    number yields. Both sides read the same holder list and so agree on who
    that is, which is what keeps the release deterministic instead of a
    livelock where both sides step aside.
    """
    snapshot = review_capacity()
    holders = snapshot["holders"].get(selected) or []
    if number not in holders:
        # The reservation is not visible in the inventory yet, so the counts
        # this verdict would use are not the counts that include it.
        return "unconfirmed"
    counts = dict(snapshot["counts"])
    counts[selected] -= 1
    if _deterministic_choice(counts, issue_id) == selected:
        return "held"
    rivals = [holder for holder in holders if holder != number]
    return "yield" if rivals and number > max(rivals) else "held"


def reserve_review_service(pr_ref: str, issue_id: int) -> Optional[str]:
    """Apply exactly one authority label under a confirmed reservation.

    Returns the reserved service, or ``None`` after printing why nothing could
    be reserved. Every failure leaves either no authority label or the one
    already confirmed, so `--finalize-review` can resume without recomputing
    capacity against an assignment that is already immutable. Selection and
    confirmation both run while holding the repository-wide atomic ref lock.
    """
    number = pr_number(pr_ref)
    if number is None:
        print(f"[ERROR] Could not resolve a pull request number for {pr_ref}; "
              "refusing to assign review authority without one to reserve against.",
              file=sys.stderr)
        return None
    with review_assignment_lock(get_current_commit()) as acquired:
        if not acquired:
            return None
        return _reserve_review_service_locked(pr_ref, issue_id, number)


def _reserve_review_service_locked(  # noqa: C901, PLR0911, PLR0912
    pr_ref: str, issue_id: int, number: int,
) -> Optional[str]:
    """Select and confirm authority while the atomic ref lock is held."""
    for _ in range(REVIEW_RESERVATION_ATTEMPTS):
        try:
            selected = select_review_service(issue_id, exclude_pr=number)
        except ReviewAssignmentLookupError as exc:
            print(f"[ERROR] Refusing to assign PR {pr_ref}: {exc}", file=sys.stderr)
            return None
        label = review_label_for_service(selected)
        ensure_label(label, "0e8a16", f"Authoritative review service: {selected}")
        try:
            live = existing_review_assignment(pr_ref)
        except ReviewAssignmentLookupError as exc:
            print(f"[ERROR] Refusing to assign PR {pr_ref}: {exc}", file=sys.stderr)
            return None
        if live is not None:
            if live != selected:
                print(
                    f"[ERROR] PR {pr_ref} was assigned concurrently to "
                    f"{review_label_for_service(live)}, not selected {label}; "
                    "refusing to arbitrate the race.",
                    file=sys.stderr,
                )
                return None
            print(f"🔒 {label} was assigned concurrently; resuming finalization.")
            return selected
        code, _, err = run_cmd(
            ["gh", "pr", "edit", pr_ref, "--add-label", label], check=False)
        if code != 0:
            print(f"[ERROR] Could not apply {label}: {err.strip()}", file=sys.stderr)
            return None

        verdict = "unconfirmed"
        held_rounds = 0
        for _confirm in range(REVIEW_RESERVATION_ATTEMPTS):
            # One sole-holder read is not settled: a contender selected from
            # the same snapshot can land immediately afterward and also claim
            # to have won.
            time.sleep(REVIEW_RESERVATION_SETTLE_DELAY_S)
            try:
                verdict = _confirm_reservation(number, issue_id, selected)
            except ReviewAssignmentLookupError as exc:
                print(f"[ERROR] Could not confirm the {label} reservation on PR "
                      f"{pr_ref}: {exc}", file=sys.stderr)
                return None
            if verdict == "held":
                held_rounds += 1
                if held_rounds == REVIEW_RESERVATION_SETTLE_ROUNDS:
                    break
                continue
            held_rounds = 0
            if verdict == "yield":
                break
        if held_rounds == REVIEW_RESERVATION_SETTLE_ROUNDS:
            return selected
        if verdict != "yield":
            print(f"[ERROR] {label} was applied to PR {pr_ref} but never settled across "
                  "consecutive open-PR inventory reads. Re-run "
                  f"create_pr.py --finalize-review {number} to resume; the existing "
                  "assignment is immutable and will be retained.", file=sys.stderr)
            return None
        code, _, err = run_cmd(
            ["gh", "pr", "edit", pr_ref, "--remove-label", label], check=False)
        if code != 0:
            print(f"[ERROR] {label} lost its reservation on PR {pr_ref} to a lower-numbered "
                  f"pull request but could not be released: {err.strip()}. Remove {label} "
                  f"manually, then re-run create_pr.py --finalize-review {number}.",
                  file=sys.stderr)
            return None
        print(f"↩️  Released {label} on PR {pr_ref}: a lower-numbered pull request "
              "reserved it first; re-selecting.")
    print(f"[ERROR] Could not reserve a review service for PR {pr_ref} in "
          f"{REVIEW_RESERVATION_ATTEMPTS} attempts; concurrent assignment kept "
          "invalidating the selection.", file=sys.stderr)
    return None


def existing_review_assignment(pr_ref: str) -> Optional[str]:
    """Return the sole canonical external assignment, if safely readable.

    ``None`` means the complete label array was read and contains no review
    label. Unknown, case-variant, duplicated, multiple, or malformed
    label state raises instead of being mistaken for an unassigned PR.
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
    names = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise ReviewAssignmentLookupError(
                f"could not parse labels for PR {pr_ref}: malformed label entry")
        names.append(label["name"])
    review_labels = [name for name in names if name.lower().startswith(REVIEW_LABEL_PREFIX)]
    if not review_labels:
        return None
    if len(review_labels) != 1:
        raise ReviewAssignmentLookupError(
            f"PR {pr_ref} carries {len(review_labels)} review labels "
            f"({', '.join(review_labels)}); exactly one canonical external "
            "review label is required.")
    canonical = {review_label_for_service(service): service for service in REVIEW_SERVICES}
    if review_labels[0] not in canonical:
        raise ReviewAssignmentLookupError(
            f"PR {pr_ref} carries unsupported review label {review_labels[0]!r}; "
            f"expected one of {', '.join(canonical)} and assignments are never "
            "migrated implicitly.")
    return canonical[review_labels[0]]


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


def trigger_codeant_review(pr_ref: str, made_ready: bool) -> bool:
    """Fire the CodeAnt manual trigger exactly once for this assignment.

    ``made_ready`` says whether *this* invocation moved the PR out of draft. A
    retry that found it already ready owns none of that state, so a transient
    comment failure must not push an already-admitted PR back out of merge
    admission for want of a rollback it never earned.
    """
    triggered = codeant_already_triggered(pr_ref)
    if triggered is None:
        print(f"[ERROR] Could not read PR {pr_ref} comments to tell whether CodeAnt was "
              "already triggered; not posting a second trigger.", file=sys.stderr)
        return False
    if triggered:
        print(f"🔍 CodeAnt review already triggered on PR {pr_ref}")
        return True
    code, _, err = run_cmd(["gh", "pr", "comment", pr_ref, "--body", CODEANT_TRIGGER],
                           check=False)
    if code == 0:
        return True
    if made_ready:
        rollback, _, rollback_err = run_cmd(["gh", "pr", "ready", pr_ref, "--undo"],
                                            check=False)
        detail = "draft state restored" if rollback == 0 else (
            f"draft rollback also failed: {rollback_err.strip()}")
    else:
        detail = "the pull request was already ready and is left as it was"
    print(f"[ERROR] Could not trigger CodeAnt review: {err.strip()}; {detail}.",
          file=sys.stderr)
    return False


def finalize_review_assignment(pr_ref: str, issue_id: int) -> bool:  # noqa: C901, PLR0912
    """Assign one balanced external authority and make the PR reviewable.

    Retries are idempotent: a sole canonical label is retained without
    recomputing capacity, an identical concurrent assignment is accepted after
    readback, and a PR already made ready is recognized from live state.
    Unknown, duplicate, malformed, or unreadable authority blocks without
    implicit migration.
    """
    try:
        service = existing_review_assignment(pr_ref)
    except ReviewAssignmentLookupError as exc:
        print(f"[ERROR] Refusing to finalize PR {pr_ref}: {exc}", file=sys.stderr)
        return False
    if service is None:
        selected = reserve_review_service(pr_ref, issue_id)
        if selected is None:
            return False
        label = review_label_for_service(selected)
    else:
        selected = service
        label = review_label_for_service(service)
        print(f"🔒 {label} already assigned; resuming finalization.")

    try:
        confirmed = existing_review_assignment(pr_ref)
    except ReviewAssignmentLookupError as exc:
        print(f"[ERROR] PR {pr_ref} has ambiguous authority before ready: {exc}", file=sys.stderr)
        return False
    if confirmed != selected:
        print(
            f"[ERROR] PR {pr_ref} did not retain {label}; not marking ready.",
            file=sys.stderr,
        )
        return False

    # Whether *this* invocation moved the PR out of draft; see
    # trigger_codeant_review for why only that half may be rolled back.
    made_ready = False
    code, _, err = run_cmd(["gh", "pr", "ready", pr_ref], check=False)
    if code == 0:
        made_ready = True
    else:
        state_code, state_out, state_err = run_cmd(
            ["gh", "pr", "view", pr_ref, "--json", "isDraft"],
            check=False,
        )
        try:
            state = json.loads(state_out) if state_code == 0 else None
        except json.JSONDecodeError:
            state = None
        if not isinstance(state, dict) or state.get("isDraft") is not False:
            detail = state_err.strip() if state_code != 0 else err.strip()
            print(
                f"[ERROR] Could not mark PR ready after assigning "
                f"{label}: {detail or 'ready state remained unconfirmed'}",
                file=sys.stderr,
            )
            return False
        try:
            if existing_review_assignment(pr_ref) != selected:
                print(f"[ERROR] PR {pr_ref} is ready but no longer carries {label}.", file=sys.stderr)
                return False
        except ReviewAssignmentLookupError as exc:
            print(f"[ERROR] PR {pr_ref} has ambiguous authority after ready recovery: {exc}",
                  file=sys.stderr)
            return False
        print(f"🔍 PR {pr_ref} was already ready with {label}")

    if selected == "codeant" and not trigger_codeant_review(pr_ref, made_ready):
        return False

    print(f"🔍 Assigned {label} and marked PR ready")
    return True


def create_pr(issue_id: int, title: str = "", body: str = "",
              agent: str = "", family: str = "",
              verification_commands: Optional[List[str]] = None) -> bool:
    current_branch = get_current_branch()
    # Opening a PR from a branch that already carried a governed merge means a
    # stale worker is re-proposing merged work under a spent name (#344).
    lease = terminal_merge_lease(current_branch)
    if lease:
        print(f"[BLOCKED] {terminal_lease_refusal(lease, 'open a PR from this branch')}",
              file=sys.stderr)
        return False
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
    code, out, err = open_pull_request_with_fallback(
        run_cmd, code, out, err, title, full_body, current_branch)
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
            "Retry balanced external review assignment on an existing PR or recover from a "
            "prior label/readback/ready failure. Requires --issue for the linked "
            "issue number; existing authority is immutable and unknown labels are never migrated."
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
        # Keep the linked issue contract strict even though issue number no
        # longer selects a provider.
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
