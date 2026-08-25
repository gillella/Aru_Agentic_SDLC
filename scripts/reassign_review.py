#!/usr/bin/env python3
# line-ceiling: 200
"""reassign_review.py - move one stalled pull request to a fallback reviewer.

CodeRabbit is the default and the only authority `create_pr.py` ever assigns.
When it is demonstrably unavailable for a specific pull request -- a pause, a
rate limit, an outage -- this command moves that one pull request to Sourcery
and records why.

Deliberately not a scheduler. There is no rotation, no capacity ledger, and no
automatic failover: authority moves only when an operator names a pull request
and a reason. Automatic failover would relocate review authority at exactly the
moment evidence is least reliable, which is when it matters most (#435).

Exactly one authority label exists on a pull request at any time. The swap is
ordered add-then-remove: a crash between the two leaves two labels, which the
merge gate refuses loudly, whereas remove-then-add could leave a pull request
with no reviewer at all and nothing to notice it.
"""

from __future__ import annotations

import argparse
import sys

from common import ensure_label, run_cmd, run_gh_json

CODERABBIT_LABEL = "review:coderabbit"
FALLBACK_LABELS = {"sourcery": "review:sourcery"}
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


def reassign(pr_id: int, service: str, reason: str) -> int:
    target = FALLBACK_LABELS.get(service)
    if not target:
        print(f"[ERROR] Unknown fallback service {service!r}; supported: "
              f"{', '.join(sorted(FALLBACK_LABELS))}.", file=sys.stderr)
        return EXIT_ERROR
    if not reason or not reason.strip():
        print("[ERROR] A reason is required; reassignment must stay auditable.",
              file=sys.stderr)
        return EXIT_ERROR

    pr = run_gh_json(["gh", "pr", "view", str(pr_id), "--json", "labels,state"])
    if not isinstance(pr, dict):
        print(f"[ERROR] Could not read PR #{pr_id}.", file=sys.stderr)
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
              "labels and the merge gate will refuse it; remove one manually.",
              file=sys.stderr)
        return EXIT_ERROR

    body = (f"Review authority reassigned from `{existing}` to `{target}`.\n\n"
            f"Reason: {reason.strip()}\n\n"
            "CodeRabbit remains the default for new pull requests; this is a "
            "per-pull-request fallback, not a rotation. The merge gate now "
            f"requires {service}'s producer-validated evidence bound to this "
            "pull request's exact current head.")
    code, _, err = run_cmd(["gh", "pr", "comment", str(pr_id), "--body", body], check=False)
    if code != 0:
        print(f"[WARN] Reassigned, but could not record the reason on PR #{pr_id}: "
              f"{err.strip()}", file=sys.stderr)
    print(f"✅ PR #{pr_id} reassigned to {service}.")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move one stalled PR from CodeRabbit to a fallback reviewer.")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--to", default="sourcery", choices=sorted(FALLBACK_LABELS))
    parser.add_argument("--reason", required=True,
                        help="Concrete unavailability, e.g. 'CodeRabbit rate limited at <sha>'")
    args = parser.parse_args()
    return reassign(args.pr, args.to, args.reason)


if __name__ == "__main__":
    sys.exit(main())
