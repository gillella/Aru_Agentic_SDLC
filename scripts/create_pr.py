#!/usr/bin/env python3
# line-ceiling: 441
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
import json
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


def review_service_for_issue(issue_id: int) -> str:
    """Stable approximately-even authority assignment for one issue number."""
    return REVIEW_SERVICES[(issue_id - 1) % len(REVIEW_SERVICES)]


def review_label_for_service(service: str) -> str:
    return f"{REVIEW_LABEL_PREFIX}{service}"


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


def finalize_review_assignment(pr_ref: str, issue_id: int) -> bool:
    """Assign exactly one review-pool service while the PR is still draft."""
    service = review_service_for_issue(issue_id)
    label = review_label_for_service(service)
    ensure_label(label, "0e8a16", f"Authoritative review service: {service}")
    code, _, err = run_cmd(
        ["gh", "pr", "edit", pr_ref, "--add-label", label],
        check=False,
    )
    if code != 0:
        print(f"[ERROR] Could not apply {label}: {err.strip()}", file=sys.stderr)
        return False
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
        return finalize_review_assignment(pr_ref, issue_id)

    return True


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
