#!/usr/bin/env python3
# +70 for #472 balanced-pool one-way reassignment history validation.
# +215 for #472 write-authorized marker provenance and atomic reassignment commit.
# line-ceiling: 545
"""reassign_review.py - move one stalled pull request to a fallback reviewer.

`create_pr.py` assigns one balanced external authority. When that service is
demonstrably unavailable for a specific pull request, this command makes one
audited external move. It never rotates through the pool. Only after external
paths are unavailable, busy, or waiting too long may an operator select one
independent coding agent. Every reassignment records why.

Deliberately not a scheduler. There is no rotation, no capacity ledger, and no
automatic failover: authority moves only when an operator names a pull request
and a reason. Automatic failover would relocate review authority at exactly the
moment evidence is least reliable, which is when it matters most (#435).

Exactly one authority label exists on a pull request at any time. The swap is
ordered add-then-remove: a crash between the two leaves two labels, which the
merge gate refuses loudly, whereas remove-then-add could leave a pull request
with no reviewer at all and nothing to notice it.

Every failure after the read leaves the pull request in a state this command
names, with the manual step needed to finish or undo it.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import re
import sys
import time

from common import (
    ensure_label,
    fetch_paginated_gh_api,
    get_repo_slug,
    run_cmd,
    run_gh_json,
)
from create_pr import MODEL_FAMILIES
from review_reassignment_lock import remote_reassignment_lock

EXTERNAL_FALLBACK_LABELS = {
    "coderabbit": "review:coderabbit",
    "sourcery": "review:sourcery",
    "codeant": "review:codeant",
}
AGENT_SERVICE = "agent"
AGENT_LABEL = "review:agent"
FALLBACK_LABELS = {**EXTERNAL_FALLBACK_LABELS, AGENT_SERVICE: AGENT_LABEL}
# Relabelling alone does not summon a reviewer. `.coderabbit.yaml` filters
# CodeRabbit's queue by label, so a moved pull request silently leaves that
# queue; the incoming service has to be asked. These are the providers' own
# documented request commands.
SERVICE_TRIGGERS = {
    "coderabbit": "@coderabbitai full review",
    "sourcery": "@sourcery-ai review",
    "codeant": "@codeant-ai: review",
}
REASSIGNMENT_MARKER_PREFIX = "<!-- aru-review-reassignment:v1 "
REASSIGNMENT_MARKER_RE = re.compile(
    re.escape(REASSIGNMENT_MARKER_PREFIX) + r"(\{[^\n]*\}) -->")
HISTORY_SETTLE_DELAYS_S = (0.0, 0.5, 1.5)
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}")
_AGENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
# Emergency coding-agent reviews require GitHub's User actor type, not Apps.
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


def current_authority(labels):
    """The single `review:` label on this PR, or a reason it is unusable.

    Returns (label, None) or (None, message). Zero, several, or a malformed
    label array all fail closed: a pull request whose authority cannot be read
    must not have that authority silently replaced.
    """
    if not isinstance(labels, list):
        return None, "could not read the pull request's labels"
    names = []
    for item in labels:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None, "the pull request has a malformed label entry"
        if item["name"].startswith("review:"):
            names.append(item["name"])
    if not names:
        return None, "the pull request carries no review authority label to replace"
    if len(names) > 1:
        return None, (f"the pull request carries {len(names)} review labels "
                      f"({', '.join(sorted(names))}); resolve that before reassigning")
    return names[0], None


def _comment(pr_id: int, body: str):
    """Post one PR comment; returns (ok, stderr)."""
    code, _, err = run_cmd(["gh", "pr", "comment", str(pr_id), "--body", body],
                           check=False)
    return code == 0, (err or "").strip()


def repository_write_logins(slug: str):
    """Repository writers, or ``None`` when the authoritative roster is unreadable."""
    code, out, _ = run_cmd(
        ["gh", "api", "--paginate",
         f"repos/{slug}/collaborators?permission=push", "--jq", ".[].login"],
        check=False)
    if code != 0:
        return None
    return {line.strip().casefold() for line in out.splitlines() if line.strip()}


def marker_provenance(comment, write_logins):
    """Classify one REST comment as trusted, untrusted, or unreadable.

    Returns ``"trusted"``, ``"untrusted"``, or ``None``. GitHub's per-comment
    MEMBER/COLLABORATOR association includes read-only actors, so it is not an
    authorization boundary. Only the repository collaborator roster filtered
    to push access can authorize an audit marker.
    """
    if not isinstance(comment, dict):
        return None
    user = comment.get("user")
    if (not isinstance(user, dict) or not isinstance(user.get("login"), str)
            or not user["login"] or not isinstance(write_logins, set)):
        return None
    if user.get("type") != "User":
        return "untrusted"
    return ("trusted" if user["login"].casefold() in write_logins else "untrusted")


def marker_records(comment, write_logins):
    """Parse one authorized marker comment; untrusted comments contribute no records."""
    provenance = marker_provenance(comment, write_logins)
    if provenance is None:
        return None
    if provenance == "untrusted":
        return []
    body = comment["body"]
    raw_markers = REASSIGNMENT_MARKER_RE.findall(body)
    if body.count(REASSIGNMENT_MARKER_PREFIX) != len(raw_markers):
        return None
    records = []
    allowed = {*EXTERNAL_FALLBACK_LABELS.values(), AGENT_LABEL}
    expected = {"from", "head", "reason", "to"}
    for raw in raw_markers:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if (not isinstance(record, dict) or set(record) != expected
                or record.get("from") not in allowed or record.get("to") not in allowed
                or record["from"] == record["to"]
                or not isinstance(record.get("reason"), str) or not record["reason"].strip()
                or not isinstance(record.get("head"), str)
                or _HEAD_RE.fullmatch(record["head"]) is None):
            return None
        records.append(record)
    return records


def reassignment_history(pr_id: int):
    """Return complete, well-formed audited moves, or ``None`` if unknown.

    A partial comment read must never look like no previous reassignment; that
    would permit a second external hop. The common paginated REST reader keeps
    transport failure distinct from an empty history.

    Only markers posted by an actor with repository write access count. Anyone
    who can see the pull request can also comment on it, so accepting every
    matching marker let an outsider fabricate a history that refuses the
    permitted fallback and blocks all later reassignment - an authorization
    bypass whose effect is denial of service. Untrusted markers are ignored
    rather than fatal, so posting one cannot block the command either; only a
    malformed *trusted* marker, or provenance that cannot be read at all,
    fails closed.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    comments = fetch_paginated_gh_api(f"repos/{slug}/issues/{pr_id}/comments")
    if comments is None:
        return None
    marker_comments = []
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            return None
        if REASSIGNMENT_MARKER_PREFIX in body:
            marker_comments.append(comment)
    if not marker_comments:
        return []
    writers = repository_write_logins(slug)
    if writers is None:
        return None
    history = []
    for comment in marker_comments:
        records = marker_records(comment, writers)
        if records is None:
            return None
        history.extend(records)
    return history


def authenticated_login():
    """The GitHub login `gh` is authenticated as, or ``None``.

    This is the account whose review and completion marker the merge gate will
    later demand, so the assignment has to name it explicitly rather than let
    the reviewer assert its own identity after the fact.
    """
    code, out, _ = run_cmd(["gh", "api", "user", "--jq", ".login"], check=False)
    if code != 0:
        return None
    login = (out or "").strip()
    return login if _LOGIN_RE.fullmatch(login) else None


def audit_body(existing: str, target: str, service: str, reason: str, head: str,
               reviewer: str = "", family: str = "", reviewer_login: str = "") -> str:
    """The auditable record of why authority moved, naming the head it moved at.

    The head matters because the merge gate is exact-head bound: a reader
    comparing this record against later evidence needs to know which commit was
    live when the reassignment happened.

    For the emergency agent path the record also names ``reviewer_login``: the
    one GitHub account authorized to submit that review and post the completion
    marker. Without it the gate could only check that the completion marker and
    the review came from the same account, which any collaborator can satisfy
    for themselves; naming the account here is what makes the later check an
    authorization test rather than a self-consistency test.
    """
    move = json.dumps({"from": existing, "head": head, "reason": reason.strip(),
                       "to": target}, sort_keys=True, separators=(",", ":"))
    move_record = f"{REASSIGNMENT_MARKER_PREFIX}{move} -->\n"
    agent_record = ""
    if service == AGENT_SERVICE:
        payload = json.dumps({"family": family, "from": existing, "head": head,
                              "reason": reason.strip(), "reviewer": reviewer,
                              "reviewer_login": reviewer_login},
                             sort_keys=True, separators=(",", ":"))
        agent_record = f"<!-- aru-agent-review-assignment:v1 {payload} -->\n"
    return (move_record + agent_record
            + f"Review authority reassigned from `{existing}` to `{target}`.\n\n"
            f"Reason: {reason.strip()}\n\n"
            f"Head at reassignment: `{head}`\n\n"
            + (f"Emergency reviewer: `{reviewer}` (`{family}`) reviewing as "
               f"`@{reviewer_login}`.\n\n"
               if service == AGENT_SERVICE else "")
            + "This is a one-way per-pull-request fallback, not a rotation. "
            "The merge gate now "
            f"requires {service}'s producer-validated evidence bound to this "
            "pull request's exact current head.")


def _validated_snapshot(pr_id: int):
    """Read the PR's state, labels, and live head, or (None, message)."""
    pr = run_gh_json(
        ["gh", "pr", "view", str(pr_id), "--json", "labels,state,headRefOid"])
    if not isinstance(pr, dict):
        return None, f"could not read PR #{pr_id}"
    head = pr.get("headRefOid")
    if not isinstance(head, str) or _HEAD_RE.fullmatch(head) is None:
        return None, (f"could not read a well-formed head commit for PR #{pr_id}; "
                      "refusing rather than reassigning against unknown state")
    return pr, None


def _recheck_before_commit(pr_id: int, existing: str, head: str, history):
    """Re-read authority, head, and history immediately before mutating.

    The caller holds the atomic per-PR ref lock, which excludes another helper
    transaction. This defense-in-depth read still catches state changed by a
    direct or legacy writer after lock acquisition. Returns ``None`` when it
    is safe to proceed, or an operator-facing message when it is not.
    """
    fresh, problem = _validated_snapshot(pr_id)
    if problem:
        return f"could not re-read PR #{pr_id} before committing: {problem}"
    if fresh.get("state") != "OPEN":
        return f"PR #{pr_id} became {fresh.get('state')} while this reassignment was being prepared"
    live, problem = current_authority(fresh.get("labels"))
    if problem:
        return f"PR #{pr_id} authority changed while this reassignment was being prepared: {problem}"
    if live != existing:
        return (f"PR #{pr_id} moved from {existing} to {live} while this reassignment "
                "was being prepared")
    if fresh.get("headRefOid") != head:
        return (f"PR #{pr_id} advanced to head {str(fresh.get('headRefOid'))[:12]} while this "
                "reassignment was being prepared; evidence is exact-head bound")
    fresh_history = reassignment_history(pr_id)
    if fresh_history is None:
        return f"could not re-establish the reassignment history for PR #{pr_id}"
    if fresh_history != list(history):
        return (f"PR #{pr_id} gained a concurrent audited reassignment while this one "
                "was being prepared")
    return None


@contextmanager
def reassignment_lock(pr_id: int):
    """Validate the live PR head, then enter its owner-bound remote lease."""
    snapshot, problem = _validated_snapshot(pr_id)
    if problem:
        print(f"[CONFLICT] Cannot establish the atomic reassignment lock for PR "
              f"#{pr_id}: {problem}.", file=sys.stderr)
        yield False
        return
    with remote_reassignment_lock(pr_id, snapshot["headRefOid"]) as acquired:
        yield acquired


def _settled_history(pr_id: int, prior, expected):
    """Retry only missing/stale audit visibility inside a bounded window."""
    committed = None
    for delay in HISTORY_SETTLE_DELAYS_S:
        if delay:
            time.sleep(delay)
        committed = reassignment_history(pr_id)
        if committed == expected or (committed is not None and committed != prior):
            break
    return committed


def reassign(pr_id: int, service: str, reason: str, reviewer: str = "",
             family: str = "", reviewer_login: str = "") -> int:
    """Run the complete reassignment transaction under its atomic ref lock."""
    target = FALLBACK_LABELS.get(service)
    if not target:
        print(f"[ERROR] Unknown fallback service {service!r}; supported: "
              f"{', '.join(sorted(FALLBACK_LABELS))}.", file=sys.stderr)
        return EXIT_ERROR
    if not reason or not reason.strip():
        print("[ERROR] A reason is required; reassignment must stay auditable.",
              file=sys.stderr)
        return EXIT_ERROR
    if service == AGENT_SERVICE and (
        _AGENT_RE.fullmatch(reviewer or "") is None or family not in MODEL_FAMILIES
    ):
        print("[ERROR] Agent fallback requires --reviewer with a safe agent id and "
              f"--model-family from: {', '.join(MODEL_FAMILIES)}.", file=sys.stderr)
        return EXIT_ERROR
    if service == AGENT_SERVICE:
        reviewer_login = (reviewer_login or "").strip() or (authenticated_login() or "")
        if _LOGIN_RE.fullmatch(reviewer_login) is None:
            print("[ERROR] Agent fallback must name the GitHub account authorized to "
                  "submit the emergency review. Pass --reviewer-login, or authenticate "
                  "`gh` as that account so it can be read from `gh api user`.",
                  file=sys.stderr)
            return EXIT_ERROR
    with reassignment_lock(pr_id) as acquired:
        if not acquired:
            return EXIT_CONFLICT
        return _reassign_locked(pr_id, service, reason, reviewer, family,
                                reviewer_login)


def _reassign_locked(  # noqa: C901, PLR0911, PLR0912, PLR0915
    pr_id: int, service: str, reason: str, reviewer: str = "",
    family: str = "", reviewer_login: str = "",
) -> int:
    target = FALLBACK_LABELS[service]

    pr, problem = _validated_snapshot(pr_id)
    if problem:
        print(f"[ERROR] {problem[0].upper()}{problem[1:]}.", file=sys.stderr)
        return EXIT_ERROR
    if pr.get("state") != "OPEN":
        print(f"[CONFLICT] PR #{pr_id} is {pr.get('state')}; only an open pull "
              "request can be reassigned.", file=sys.stderr)
        return EXIT_CONFLICT

    existing, problem = current_authority(pr.get("labels"))
    if problem:
        print(f"[CONFLICT] Refusing to reassign PR #{pr_id}: {problem}.", file=sys.stderr)
        return EXIT_CONFLICT
    if existing == target:
        print(f"[CONFLICT] PR #{pr_id} is already assigned to {service}; "
              "reassignment is not a retry mechanism.", file=sys.stderr)
        return EXIT_CONFLICT
    history = reassignment_history(pr_id)
    if history is None:
        print(f"[ERROR] Could not establish complete reassignment history for PR #{pr_id}; "
              "refusing rather than permitting a repeated move.", file=sys.stderr)
        return EXIT_ERROR
    if service != AGENT_SERVICE:
        if existing not in EXTERNAL_FALLBACK_LABELS.values():
            print(f"[CONFLICT] {existing!r} is not a configured external authority.",
                  file=sys.stderr)
            return EXIT_CONFLICT
        if history:
            print(f"[CONFLICT] PR #{pr_id} already has an audited reassignment; "
                  "a second external hop would rotate immutable authority.", file=sys.stderr)
            return EXIT_CONFLICT
    if service == AGENT_SERVICE:
        if existing not in EXTERNAL_FALLBACK_LABELS.values():
            print(f"[CONFLICT] {existing!r} is not a configured external "
                  "review authority; agent fallback cannot replace it.", file=sys.stderr)
            return EXIT_CONFLICT
        if len(history) > 1:
            print(f"[CONFLICT] PR #{pr_id} has repeated audited reassignments; "
                  "terminal fallback cannot legitimize a rotation.", file=sys.stderr)
            return EXIT_CONFLICT
        if history and history[0].get("to") != existing:
            print(f"[CONFLICT] PR #{pr_id} live authority {existing} does not match "
                  "its audited reassignment history; resolve the label drift first.",
                  file=sys.stderr)
            return EXIT_CONFLICT
        if any(record.get("to") == AGENT_LABEL for record in history):
            print(f"[CONFLICT] PR #{pr_id} already used its terminal agent fallback.",
                  file=sys.stderr)
            return EXIT_CONFLICT
        authors = [name[len("author:"):] for name in
                   (item["name"] for item in pr["labels"])
                   if name.startswith("author:") and name[len("author:"):]]
        if len(authors) != 1 or authors[0] == reviewer:
            print("[CONFLICT] Agent fallback requires exactly one different "
                  "author:<id>; self-review or ambiguous authorship is forbidden.",
                  file=sys.stderr)
            return EXIT_CONFLICT

    problem = _recheck_before_commit(pr_id, existing, pr["headRefOid"], history)
    if problem:
        print(f"[CONFLICT] Refusing to reassign PR #{pr_id}: {problem}. "
              "Nothing was changed; re-run once the concurrent move has settled.",
              file=sys.stderr)
        return EXIT_CONFLICT

    if not ensure_label(target, "5319e7", f"Fallback review authority: {service}"):
        print(f"[ERROR] Could not provision {target}.", file=sys.stderr)
        return EXIT_ERROR
    reviewer_label = f"reviewer:{reviewer}" if service == AGENT_SERVICE else ""
    if reviewer_label and not ensure_label(
        reviewer_label, "0e8a16", f"Emergency review by agent '{reviewer}'",
    ):
        print(f"[ERROR] Could not provision {reviewer_label}.", file=sys.stderr)
        return EXIT_ERROR
    # Add before remove: two labels is a state the merge gate refuses loudly,
    # while none is a pull request with no reviewer and nothing watching it.
    add_cmd = ["gh", "pr", "edit", str(pr_id), "--add-label", target]
    if reviewer_label:
        add_cmd.extend(["--add-label", reviewer_label])
    code, _, err = run_cmd(add_cmd, check=False)
    if code != 0:
        print(f"[ERROR] Could not add {target} to PR #{pr_id}: {err.strip()}. "
              "The original assignment is untouched.", file=sys.stderr)
        return EXIT_ERROR

    # Persist the one-way audit while both labels are present. If this write
    # fails, the merge gate sees the intentionally ambiguous state and no
    # caller can mistake the missing history for permission to rotate again.
    own_record = {"from": existing, "head": pr["headRefOid"],
                  "reason": reason.strip(), "to": target}
    ok, err = _comment(pr_id, audit_body(existing, target, service, reason,
                                         pr["headRefOid"], reviewer, family,
                                         reviewer_login))
    if not ok:
        print(f"[ERROR] Added {target} to PR #{pr_id}, but the reason could not "
              f"be recorded: {err}. Both authority labels remain so the merge "
              f"gate blocks; restore {existing} by removing {target}, or record "
              "the audit before completing the swap.", file=sys.stderr)
        return EXIT_ERROR

    # The audit is the committed record, so require the complete history to be
    # exactly the prior prefix plus this command's record. Cardinality alone is
    # insufficient: an eventually-consistent read can still show no new record,
    # or a rival record can appear with the expected length.
    expected_history = [*history, own_record]
    committed = _settled_history(pr_id, list(history), expected_history)
    if committed != expected_history:
        detail = ("the reassignment history could not be re-read" if committed is None
                  else "the visible audit history is not the exact record this command wrote")
        print(f"[CONFLICT] PR #{pr_id} recorded this reassignment but {detail}. "
              "Authority is left fail-closed; an operator must re-read the labels "
              "and reconcile the audit trail before this pull request can merge.",
              file=sys.stderr)
        return EXIT_CONFLICT

    code, _, err = run_cmd(["gh", "pr", "edit", str(pr_id), "--remove-label", existing],
                           check=False)
    if code != 0:
        print(f"[ERROR] Added {target} but could not remove {existing} from PR "
              f"#{pr_id}: {err.strip()}. The pull request now carries two authority "
              f"labels and the merge gate will refuse it; remove {existing} manually "
              "or remove the new label to undo the reassignment.", file=sys.stderr)
        return EXIT_ERROR

    if service == AGENT_SERVICE:
        print(f"✅ PR #{pr_id} assigned to independent agent {reviewer} ({family}) "
              f"at head {pr['headRefOid'][:12]}; external exhaustion recorded.")
        return EXIT_OK

    trigger = SERVICE_TRIGGERS[service]
    ok, err = _comment(pr_id, trigger)
    if not ok:
        print(f"[ERROR] PR #{pr_id} is reassigned and audited, but {service} could "
              f"not be triggered: {err}. Post `{trigger}` on the pull request; "
              "until the service reviews the current head the merge gate will "
              "refuse it.", file=sys.stderr)
        return EXIT_ERROR

    print(f"✅ PR #{pr_id} reassigned to {service} at head {pr['headRefOid'][:12]} "
          f"and triggered with `{trigger}`.")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move one stalled PR through the audited one-way fallback path.")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--to", required=True, choices=sorted(FALLBACK_LABELS))
    parser.add_argument("--reason", required=True,
                        help="Concrete unavailability, e.g. 'assigned service rate limited at <sha>'")
    parser.add_argument("--reviewer", default="",
                        help="Independent agent id; required only with --to agent")
    parser.add_argument("--model-family", "--family", dest="family", default="",
                        choices=MODEL_FAMILIES,
                        help="Independent agent model family; required with --to agent")
    parser.add_argument("--reviewer-login", dest="reviewer_login", default="",
                        help=("GitHub account authorized to submit the emergency review; "
                              "defaults to the `gh` authenticated login. Used only "
                              "with --to agent."))
    args = parser.parse_args()
    return reassign(args.pr, args.to, args.reason, args.reviewer, args.family,
                    args.reviewer_login)


if __name__ == "__main__":
    sys.exit(main())
