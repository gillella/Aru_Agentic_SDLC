#!/usr/bin/env python3
# +70 for #472 balanced-pool one-way reassignment history validation.
# line-ceiling: 330
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
import json
import re
import sys

from common import ensure_label, fetch_paginated_gh_api, get_repo_slug, run_cmd, run_gh_json
from create_pr import MODEL_FAMILIES

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
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}")
_AGENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
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


def reassignment_history(pr_id: int):
    """Return complete, well-formed audited moves, or ``None`` if unknown.

    A partial comment read must never look like no previous reassignment; that
    would permit a second external hop. The common paginated REST reader keeps
    transport failure distinct from an empty history.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    comments = fetch_paginated_gh_api(f"repos/{slug}/issues/{pr_id}/comments")
    if comments is None:
        return None
    history = []
    allowed = {*EXTERNAL_FALLBACK_LABELS.values(), AGENT_LABEL}
    expected = {"from", "head", "reason", "to"}
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str):
            return None
        raw_markers = REASSIGNMENT_MARKER_RE.findall(body)
        if body.count(REASSIGNMENT_MARKER_PREFIX) != len(raw_markers):
            return None
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
            history.append(record)
    return history


def audit_body(existing: str, target: str, service: str, reason: str, head: str,
               reviewer: str = "", family: str = "") -> str:
    """The auditable record of why authority moved, naming the head it moved at.

    The head matters because the merge gate is exact-head bound: a reader
    comparing this record against later evidence needs to know which commit was
    live when the reassignment happened.
    """
    move = json.dumps({"from": existing, "head": head, "reason": reason.strip(),
                       "to": target}, sort_keys=True, separators=(",", ":"))
    move_record = f"{REASSIGNMENT_MARKER_PREFIX}{move} -->\n"
    agent_record = ""
    if service == AGENT_SERVICE:
        payload = json.dumps({"family": family, "from": existing, "head": head,
                              "reason": reason.strip(), "reviewer": reviewer},
                             sort_keys=True, separators=(",", ":"))
        agent_record = f"<!-- aru-agent-review-assignment:v1 {payload} -->\n"
    return (move_record + agent_record
            + f"Review authority reassigned from `{existing}` to `{target}`.\n\n"
            f"Reason: {reason.strip()}\n\n"
            f"Head at reassignment: `{head}`\n\n"
            + (f"Emergency reviewer: `{reviewer}` (`{family}`).\n\n"
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


def reassign(pr_id: int, service: str, reason: str, reviewer: str = "",  # noqa: C901, PLR0911, PLR0912
             family: str = "") -> int:
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
    ok, err = _comment(pr_id, audit_body(existing, target, service,
                                         reason, pr["headRefOid"], reviewer, family))
    if not ok:
        print(f"[ERROR] Added {target} to PR #{pr_id}, but the reason could not "
              f"be recorded: {err}. Both authority labels remain so the merge "
              f"gate blocks; restore {existing} by removing {target}, or record "
              "the audit before completing the swap.", file=sys.stderr)
        return EXIT_ERROR

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
    args = parser.parse_args()
    return reassign(args.pr, args.to, args.reason, args.reviewer, args.family)


if __name__ == "__main__":
    sys.exit(main())
