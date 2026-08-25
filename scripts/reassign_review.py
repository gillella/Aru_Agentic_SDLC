#!/usr/bin/env python3
# line-ceiling: 260
"""reassign_review.py - move one stalled pull request to a fallback reviewer.

CodeRabbit is the default and the only authority `create_pr.py` ever assigns.
When it is demonstrably unavailable for a specific pull request -- a pause, a
rate limit, an outage -- this command moves that one pull request to Sourcery
or CodeAnt and records why.

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
import re
import sys

from common import ensure_label, run_cmd, run_gh_json

CODERABBIT_LABEL = "review:coderabbit"
FALLBACK_LABELS = {"sourcery": "review:sourcery", "codeant": "review:codeant"}
# Relabelling alone does not summon a reviewer. `.coderabbit.yaml` filters
# CodeRabbit's queue by label, so a moved pull request silently leaves that
# queue; the incoming service has to be asked. These are the providers' own
# documented request commands.
SERVICE_TRIGGERS = {
    "sourcery": "@sourcery-ai review",
    "codeant": "@codeant-ai: review",
}
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}")
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


def audit_body(existing: str, target: str, service: str, reason: str, head: str) -> str:
    """The auditable record of why authority moved, naming the head it moved at.

    The head matters because the merge gate is exact-head bound: a reader
    comparing this record against later evidence needs to know which commit was
    live when the reassignment happened.
    """
    return (f"Review authority reassigned from `{existing}` to `{target}`.\n\n"
            f"Reason: {reason.strip()}\n\n"
            f"Head at reassignment: `{head}`\n\n"
            "CodeRabbit remains the default for new pull requests; this is a "
            "per-pull-request fallback, not a rotation. The merge gate now "
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


def reassign(pr_id: int, service: str, reason: str) -> int:  # noqa: C901, PLR0911
    target = FALLBACK_LABELS.get(service)
    if not target:
        print(f"[ERROR] Unknown fallback service {service!r}; supported: "
              f"{', '.join(sorted(FALLBACK_LABELS))}.", file=sys.stderr)
        return EXIT_ERROR
    if not reason or not reason.strip():
        print("[ERROR] A reason is required; reassignment must stay auditable.",
              file=sys.stderr)
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
    if existing != CODERABBIT_LABEL:
        print(f"[CONFLICT] PR #{pr_id} carries {existing!r}, not the default "
              f"{CODERABBIT_LABEL!r}; only the default assignment may be moved to a "
              "fallback, so an already-switched pull request is never switched again.",
              file=sys.stderr)
        return EXIT_CONFLICT

    ensure_label(target, "5319e7", f"Fallback review authority: {service}")
    # Add before remove: two labels is a state the merge gate refuses loudly,
    # while none is a pull request with no reviewer and nothing watching it.
    code, _, err = run_cmd(["gh", "pr", "edit", str(pr_id), "--add-label", target],
                           check=False)
    if code != 0:
        print(f"[ERROR] Could not add {target} to PR #{pr_id}: {err.strip()}. "
              "The original assignment is untouched.", file=sys.stderr)
        return EXIT_ERROR
    code, _, err = run_cmd(["gh", "pr", "edit", str(pr_id), "--remove-label", existing],
                           check=False)
    if code != 0:
        print(f"[ERROR] Added {target} but could not remove {existing} from PR "
              f"#{pr_id}: {err.strip()}. The pull request now carries two authority "
              f"labels and the merge gate will refuse it; remove {existing} manually "
              "or remove the new label to undo the reassignment.", file=sys.stderr)
        return EXIT_ERROR

    ok, err = _comment(pr_id, audit_body(existing, target, service,
                                         reason, pr["headRefOid"]))
    if not ok:
        print(f"[ERROR] PR #{pr_id} now carries {target}, but the reason could not "
              f"be recorded: {err}. An unaudited reassignment is not acceptable; "
              "post the reason manually or restore "
              f"{existing}.", file=sys.stderr)
        return EXIT_ERROR

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
        description="Move one stalled PR from CodeRabbit to a fallback reviewer.")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--to", required=True, choices=sorted(FALLBACK_LABELS))
    parser.add_argument("--reason", required=True,
                        help="Concrete unavailability, e.g. 'CodeRabbit rate limited at <sha>'")
    args = parser.parse_args()
    return reassign(args.pr, args.to, args.reason)


if __name__ == "__main__":
    sys.exit(main())
