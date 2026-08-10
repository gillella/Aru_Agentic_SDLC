#!/usr/bin/env python3
"""merge_pr.py - the Definition-of-Done gate.

Branch protection is not available on every plan, and "CI green before merge"
written in a playbook is not a gate. This script is the gate: it refuses to
merge until the DoD is provably met, then performs the whole close-out -
merge, delete branch, prune worktree, move the board to Done - so the tail of
the lifecycle stops depending on someone remembering it.

Every refusal names the one unmet condition and exits non-zero. Nothing here
is advisory.

  python3 merge_pr.py --pr 42
  python3 merge_pr.py --pr 42 --dry-run

Exit codes:
  0 - merged (or dry-run passed every check)
  1 - error talking to GitHub
  3 - DoD not met; nothing was merged
"""

import argparse
import json
import os
import re
import subprocess
import sys

from common import get_repo_slug, run_cmd
from update_issue_status import update_status

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 3

# Completed-review attribution, written by claim_issue.py --complete-review.
# This is the only label that satisfies the gate.
REVIEWED_BY_LABEL = "reviewed-by:"
# The transient claim, written by claim_review. Deliberately NOT accepted here:
# it records that an agent took the PR off the queue, not that it read anything.
# Treating it as attestation would let an author's own same-account review plus
# any peer's claim satisfy the gate before that peer had looked at the diff.
REVIEW_CLAIM_LABEL = "reviewer:"

# Large diffs remain visible in the audit output. The separate independent-
# review gate, not a blanket human-review assertion, owns review quality.
SIZE_SOFT_LIMIT = 400

PR_FIELDS = (
    "number,title,body,state,isDraft,mergeable,mergeStateStatus,baseRefName,author,"
    "headRefName,headRefOid,additions,deletions,reviews,statusCheckRollup,labels"
)


def _gh_json(args):
    """Runs a gh command expected to emit JSON. Returns None on any failure."""
    code, out, err = run_cmd(args, check=False)
    if code != 0:
        print(f"[ERROR] {' '.join(args[:3])}...: {err.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        print(f"[ERROR] Unparseable JSON from {' '.join(args[:3])}...", file=sys.stderr)
        return None


def fetch_pr(pr_id):
    return _gh_json(["gh", "pr", "view", str(pr_id), "--json", PR_FIELDS])


def linked_issues(body):
    """Every issue this PR closes, in order of appearance.

    A PR may legitimately close several issues, and GitHub closes all of them.
    Checking only the first would let the acceptance criteria of the others
    through unverified - which is the exact hole this script exists to close.
    """
    seen, out = set(), []
    for match in re.finditer(r"\bcloses\s+#(\d+)\b", body or "", re.IGNORECASE):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


def linked_issue(body):
    """The first closed issue, or None. Kept for callers that want just one."""
    issues = linked_issues(body)
    return issues[0] if issues else None


def unresolved_threads(pr_id):
    """Counts unresolved review threads.

    gh pr view cannot report thread resolution, so this drops to GraphQL.
    Returns None when the query fails, which the caller treats as a refusal
    rather than a pass - an unknown review state must never merge.
    """
    slug = get_repo_slug()
    if not slug:
        return None
    owner, name = slug.split("/", 1)
    query = """
    query($owner:String!, $name:String!, $pr:Int!) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          reviewThreads(first:100) { nodes { isResolved isOutdated } }
        }
      }
    }"""
    data = _gh_json([
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
    ])
    if not data:
        return None
    try:
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return None
    return sum(1 for n in nodes if not n.get("isResolved") and not n.get("isOutdated"))


def unticked_criteria(issue_body):
    """Returns the acceptance-criteria lines still unchecked.

    Only counts checkboxes under an 'Acceptance Criteria' heading; a checklist
    elsewhere in the body (a reviewer's notes, say) must not gate the merge.
    """
    if not issue_body:
        return []
    section = re.split(
        r"^\s*#{1,4}\s*acceptance criteria\s*$", issue_body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(section) < 2:
        return []
    # Stop at the next heading.
    tail = re.split(r"^\s*#{1,4}\s+", section[1], flags=re.MULTILINE)[0]
    return [
        line.strip()
        for line in tail.splitlines()
        if re.match(r"^\s*[-*]\s*\[\s\]", line)
    ]


# --- Individual gates -------------------------------------------------------
# Each returns (passed, message). Kept pure and separate so the test suite can
# drive every refusal path without a network.

def check_open(pr):
    if pr.get("state") != "OPEN":
        return False, f"PR is {pr.get('state','?').lower()}, not open."
    if pr.get("isDraft"):
        return False, "PR is still a draft."
    return True, "PR is open."


def check_ci(pr):
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return False, "No CI checks reported on the head commit. A PR with no checks is not verified."
    # Allowlist, not denylist. Enumerating the failure conclusions let unknown
    # ones - STARTUP_FAILURE, STALE, anything GitHub adds later - fall through
    # to "green" and merge an unverified head. Only these three mean "passed";
    # every other completed conclusion fails closed.
    passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    in_progress = {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}

    failing, pending = [], []
    for check in rollup:
        # Check runs use 'conclusion'; legacy statuses use 'state'.
        status = (check.get("status") or "").upper()
        result = (check.get("conclusion") or check.get("state") or "").upper()
        name = check.get("name") or check.get("context") or "check"
        if status and status != "COMPLETED" and not result or result in in_progress:
            pending.append(name)
        elif result not in passing:
            failing.append(f"{name}={result.lower() or 'unknown'}")
    if failing:
        return False, f"CI is red: {', '.join(failing)}."
    if pending:
        return False, f"CI has not finished: {', '.join(pending)}."
    return True, f"CI green ({len(rollup)} checks)."


def label_values(pr, prefix):
    """All values of labels sharing a prefix, e.g. every reviewed-by:<id>."""
    return [
        (lab.get("name") or "")[len(prefix):]
        for lab in (pr.get("labels") or [])
        if (lab.get("name") or "").startswith(prefix)
    ]


def latest_state_per_reviewer(reviews):
    """Collapses review history to each reviewer's most recent verdict.

    `reviews` is the full submission history, so a reviewer who requested
    changes and later approved still has the CHANGES_REQUESTED entry in it.
    Reading the raw list blocks such a PR forever, contradicting the message
    that says re-approval is supported. Only the last word from each reviewer
    counts.
    """
    latest = {}
    for review in reviews:
        state = (review.get("state") or "").upper()
        if state in {"PENDING", "COMMENTED"}:
            # A comment-only review does not change a prior verdict, and a
            # pending one was never submitted.
            continue
        who = ((review.get("author") or {}).get("login")
               or review.get("id") or "unknown")
        latest[who] = (review.get("submittedAt") or "", state)
    return {who: state for who, (_, state) in latest.items()}


def check_reviews(pr, threads):
    reviews = pr.get("reviews") or []
    substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
    if not substantive:
        return False, "No review on this PR. At least one review is required."
    verdicts = latest_state_per_reviewer(reviews)
    blocking = [who for who, state in verdicts.items() if state == "CHANGES_REQUESTED"]
    if blocking:
        return False, (f"{', '.join(blocking)} requested changes and has not re-approved.")
    if threads is None:
        return False, "Could not determine review-thread state; refusing rather than guessing."
    if threads > 0:
        return False, f"{threads} unresolved review thread(s)."

    # GitHub cannot tell a self-review from a peer review here: every agent
    # authenticates as the same user, so every review looks like it came from
    # the same person who opened the PR. The agent identity labels are the only
    # thing that distinguishes them.
    # A review from a *different GitHub account* is provably not a self-review,
    # whatever the labels say. This is how external reviewers count: Codex and
    # Bugbot post as their own apps and will never stamp reviewed-by:, so
    # requiring the label would block every bot-reviewed PR forever.
    pr_login = ((pr.get("author") or {}).get("login") or "").lower()
    other_accounts = sorted({
        ((r.get("author") or {}).get("login") or "").lower()
        for r in substantive
    } - {"", pr_login})
    if other_accounts:
        note = f"{len(substantive)} review(s) from {', '.join(other_accounts)}, no unresolved threads."
        if any((lab.get("name") or "") == "same-family-review"
               for lab in (pr.get("labels") or [])):
            note += " ⚠️  Same-family review: no cross-family agent was available."
        return True, note

    # Everything below is the same-account case: agents all authenticate as one
    # GitHub user, so only the identity labels can tell them apart.
    authors = label_values(pr, "author:")
    if not authors:
        # Unstamped PR - predates create_pr.py --agent, or a human opened it.
        # Falling back to "any review counts" keeps those mergeable; refusing
        # would strand every PR opened before stamping existed.
        return True, f"{len(substantive)} review(s), no unresolved threads (author unstamped)."

    author = authors[0]
    # Only completed attribution counts. A `reviewer:` claim is deliberately
    # not consulted: it means an agent took the PR off the queue, which is not
    # evidence anyone read the diff. Accepting it would let the author's own
    # same-account review plus any peer's claim clear the gate.
    reviewers = label_values(pr, REVIEWED_BY_LABEL)
    peers = [r for r in reviewers if r != author]
    if reviewers and not peers:
        return False, (f"The only review is from '{author}', who wrote this PR. "
                       "A self-review does not satisfy the gate.")
    if not reviewers:
        claimants = [c for c in label_values(pr, REVIEW_CLAIM_LABEL) if c != author]
        if claimants:
            # The common case, and worth its own message: the reviewer claimed
            # the PR and skipped the completion step, so the work happened but
            # was never attributed.
            return False, (
                f"'{claimants[0]}' holds the review claim but never completed it, so no "
                f"{REVIEWED_BY_LABEL}<agent> label attributes the review. Finish with "
                f"`claim_issue.py --pr <n> --agent {claimants[0]} --complete-review`.")
        return False, (f"A review exists but no {REVIEWED_BY_LABEL}<agent> label identifies "
                       f"who left it, so it cannot be distinguished from a self-review by "
                       f"'{author}'. The reviewing agent must finish with "
                       f"`claim_issue.py --pr <n> --agent <id> --complete-review`.")

    note = f"{len(substantive)} review(s) from {', '.join(peers)}, no unresolved threads."
    if any((lab.get("name") or "") == "same-family-review" for lab in (pr.get("labels") or [])):
        note += " ⚠️  Same-family review: no cross-family agent was available."
    return True, note


def check_rebased(pr):
    state = (pr.get("mergeStateStatus") or "").upper()
    if state == "BEHIND":
        return False, "Branch is behind the base. Rebase on main and re-run."
    if state == "DIRTY":
        return False, "Branch has merge conflicts with the base."
    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return False, "Branch conflicts with the base."
    return True, "Branch is current with the base."


def check_issue_link(pr):
    issues = linked_issues(pr.get("body"))
    if not issues:
        return False, "PR body has no 'Closes #<issue>'. Every PR must close a tracked issue."
    return True, "Linked to " + ", ".join(f"#{i}" for i in issues) + "."


def check_acceptance(issue_num, issue_body):
    pending = unticked_criteria(issue_body)
    if pending:
        preview = "\n      ".join(pending[:5])
        more = f"\n      ... and {len(pending) - 5} more" if len(pending) > 5 else ""
        return False, (
            f"Issue #{issue_num} has {len(pending)} unticked acceptance criteria:\n"
            f"      {preview}{more}"
        )
    return True, f"All acceptance criteria on #{issue_num} are ticked."


def check_size(pr):
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if total > SIZE_SOFT_LIMIT:
        return True, (
            f"Diff is {total} lines, over the {SIZE_SOFT_LIMIT}-line soft limit. "
            "Independent review remains mandatory through the separate review gate."
        )
    return True, f"Diff is {total} lines."


def prune_worktree(repo_root, branch):
    """Removes the worktree for a merged branch, if one exists.

    Best-effort: a worktree with uncommitted changes is left alone and
    reported, because discarding an agent's unpushed work to tidy up would be
    worse than a stale directory.
    """
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root)
    if code != 0:
        return
    path = None
    for block in out.split("\n\n"):
        if f"branch refs/heads/{branch}" in block:
            first = block.splitlines()[0]
            path = first.split(" ", 1)[1] if first.startswith("worktree ") else None
            break
    if not path:
        return
    code, _, err = run_cmd(["git", "worktree", "remove", path], check=False, cwd=repo_root)
    if code == 0:
        print(f"🧹 Pruned worktree {path}")
    else:
        print(f"[WARN] Left worktree {path} in place: {err.strip()}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Merge a PR only if the Definition of Done is met.")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--dry-run", action="store_true", help="Run every check, merge nothing")
    parser.add_argument("--merge-method", default="squash", choices=["squash", "merge", "rebase"])
    args = parser.parse_args()

    pr = fetch_pr(args.pr)
    if not pr:
        return EXIT_ERROR

    issue_nums = linked_issues(pr.get("body"))
    issue_bodies = {}
    for num in issue_nums:
        issue = _gh_json(["gh", "issue", "view", str(num), "--json", "body"])
        if issue is None:
            return EXIT_ERROR
        issue_bodies[num] = issue.get("body") or ""

    threads = unresolved_threads(args.pr)

    gates = [
        ("open", check_open(pr)),
        ("issue link", check_issue_link(pr)),
        ("ci", check_ci(pr)),
        ("review", check_reviews(pr, threads)),
        ("rebased", check_rebased(pr)),
        ("size", check_size(pr)),
    ]
    # One acceptance gate per closed issue: GitHub will close them all, so all
    # of them must be satisfied.
    for num in issue_nums:
        gates.append((f"accept #{num}", check_acceptance(num, issue_bodies[num])))

    print(f"=== Definition of Done — PR #{args.pr}: {pr.get('title','')} ===")
    blocked = []
    for name, (ok, message) in gates:
        print(f"  {'✅' if ok else '❌'} {name:<11} {message}")
        if not ok:
            blocked.append(name)

    if blocked:
        print(f"\n🚫 Not merged. Unmet: {', '.join(blocked)}.")
        return EXIT_BLOCKED

    if args.dry_run:
        print("\n✅ Every gate passed. --dry-run, so nothing was merged.")
        return EXIT_OK

    # ARU_ALLOW_MAIN_PUSH lets the pre-push hook distinguish this sanctioned
    # path from an agent pushing to main directly.
    env = dict(os.environ, ARU_ALLOW_MAIN_PUSH="1")
    # Pin the head we actually gated. Between fetch_pr() and this call an
    # agent can push again, and without this every gate above would describe
    # the old head while gh merges a new, unreviewed and untested one.
    merge_cmd = ["gh", "pr", "merge", str(args.pr), f"--{args.merge_method}", "--delete-branch"]
    head_sha = pr.get("headRefOid")
    if head_sha:
        merge_cmd += ["--match-head-commit", head_sha]
    else:
        print("[WARN] No head SHA available; merging without pinning it.", file=sys.stderr)
    proc = subprocess.run(merge_cmd, capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0:
        print(f"[ERROR] Merge failed: {proc.stderr.strip()}", file=sys.stderr)
        return EXIT_ERROR
    print(f"\n✅ PR #{args.pr} merged and branch deleted.")

    code, root, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], check=False)
    if code == 0 and root:
        prune_worktree(root.strip(), pr.get("headRefName", ""))

    for num in issue_nums:
        if update_status(num, "Done"):
            print(f"✅ Issue #{num} moved to Done.")
        else:
            print(f"[WARN] Could not move #{num} to Done; do it by hand.", file=sys.stderr)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
