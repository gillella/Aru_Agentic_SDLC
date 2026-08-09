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
  python3 merge_pr.py --pr 42 --force-human-review   # oversized diff, reviewed anyway

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

# Reported agentic PRs run materially larger than human ones, and large diffs
# are where review quality collapses. Not a hard stop - a forced human ack.
SIZE_SOFT_LIMIT = 400

PR_FIELDS = (
    "number,title,body,state,isDraft,mergeable,mergeStateStatus,baseRefName,"
    "headRefName,additions,deletions,reviews,statusCheckRollup"
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
    failing, pending = [], []
    for check in rollup:
        # Check runs use 'conclusion'; legacy statuses use 'state'.
        status = (check.get("status") or "").upper()
        result = (check.get("conclusion") or check.get("state") or "").upper()
        name = check.get("name") or check.get("context") or "check"
        if status and status != "COMPLETED" and not result:
            pending.append(name)
        elif result in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
            failing.append(f"{name}={result.lower()}")
        elif result in {"", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"}:
            pending.append(name)
    if failing:
        return False, f"CI is red: {', '.join(failing)}."
    if pending:
        return False, f"CI has not finished: {', '.join(pending)}."
    return True, f"CI green ({len(rollup)} checks)."


def check_reviews(pr, threads):
    reviews = pr.get("reviews") or []
    substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
    if not substantive:
        return False, "No review on this PR. At least one review is required."
    if any((r.get("state") or "").upper() == "CHANGES_REQUESTED" for r in substantive):
        return False, "A reviewer requested changes and has not re-approved."
    if threads is None:
        return False, "Could not determine review-thread state; refusing rather than guessing."
    if threads > 0:
        return False, f"{threads} unresolved review thread(s)."
    return True, f"{len(substantive)} review(s), no unresolved threads."


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


def check_size(pr, forced):
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    if total > SIZE_SOFT_LIMIT and not forced:
        return False, (
            f"Diff is {total} lines, over the {SIZE_SOFT_LIMIT}-line soft limit. "
            "Split it, or re-run with --force-human-review to confirm a human read it all."
        )
    note = " (waived)" if total > SIZE_SOFT_LIMIT else ""
    return True, f"Diff is {total} lines{note}."


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
    parser.add_argument("--force-human-review", action="store_true",
                        help="Acknowledge an oversized diff was read by a human")
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
        ("size", check_size(pr, args.force_human_review)),
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
    proc = subprocess.run(
        ["gh", "pr", "merge", str(args.pr), f"--{args.merge_method}", "--delete-branch"],
        capture_output=True, text=True, env=env, check=False,
    )
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
