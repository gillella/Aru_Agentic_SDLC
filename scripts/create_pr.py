#!/usr/bin/env python3
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
import sys
from datetime import datetime, timezone

from common import ensure_label, get_current_branch, get_issue, run_cmd

NEEDS_REVIEW_LABEL = "needs-review"

# Kept explicit rather than free-form: a typo like "anthropc" would silently
# make every PR look cross-family to the picker, which is the one failure mode
# this label exists to prevent.
MODEL_FAMILIES = ("anthropic", "openai", "google", "meta", "mistral", "xai", "human")


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


def create_pr(issue_id: int, title: str = "", body: str = "",
              agent: str = "", family: str = "") -> bool:
    current_branch = get_current_branch()
    issue = get_issue(issue_id)

    if not title:
        title = issue["title"] if issue else f"Fix issue #{issue_id}"

    closure_footer = f"\n\nCloses #{issue_id}"
    full_body = (body.strip() + closure_footer) if body else f"Implementation for issue #{issue_id}.{closure_footer}"

    print(f"Opening Pull Request for branch '{current_branch}' linking 'Closes #{issue_id}'...")
    cmd = ["gh", "pr", "create", "--title", title, "--body", full_body, "--head", current_branch]

    code, out, err = run_cmd(cmd, check=False)
    if code != 0:
        print(f"[ERROR] Failed to open PR: {err}", file=sys.stderr)
        return False

    print(f"✅ Pull Request created successfully:\n{out}")

    if agent or family:
        # `gh pr create` prints the URL, which gh accepts anywhere a PR number
        # would do. Falling back to the branch keeps this working if the output
        # format ever changes.
        pr_ref = out.strip().splitlines()[-1].strip() if out.strip() else current_branch
        # Reported as failure even though the PR opened: an unstamped PR is a
        # hole in the review gate, and a zero exit here would let a caller
        # move on believing the identity landed.
        if not apply_identity(pr_ref, agent, family):
            return False
        enqueue_review(pr_ref)
        return True

    return True


def main():
    parser = argparse.ArgumentParser(description="Create Pull Request linking an issue.")
    parser.add_argument("--issue", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--title", type=str, default="", help="Pull Request Title")
    parser.add_argument("--body", type=str, default="", help="Pull Request Description Body")
    # Required, matching claim_issue.py. It was optional and defaulted to "",
    # so a caller who simply forgot produced a PR with no author:<id>, and
    # merge_pr.py then accepted any review on it - including a self-review.
    # An identity the gate depends on cannot be opt-in.
    parser.add_argument("--agent", type=str, required=True,
                        help="Authoring agent id; stamped as author:<id> for review eligibility")
    parser.add_argument("--model-family", type=str, default="", dest="family",
                        help=f"Authoring model family, one of: {', '.join(MODEL_FAMILIES)}")
    args = parser.parse_args()

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

    ok = create_pr(args.issue, args.title, args.body, args.agent, args.family.lower())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
