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
  1 - error, or merge completed with resumable close-out failures
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
# Transient merge-execution claim from claim_merge. Cleared on close-out; never
# treated as review attestation.
MERGER_CLAIM_LABEL = "merger:"

# Review apps can add useful findings, but their comments are not independent
# approval. GitHub exposes some bot logins with a ``[bot]`` suffix and the
# Codex connector without one, so both forms must be recognized explicitly.
ADVISORY_REVIEW_ACCOUNTS = {"chatgpt-codex-connector"}

# Large diffs remain visible in the audit output. The separate independent-
# review gate, not a blanket human-review assertion, owns review quality.
SIZE_SOFT_LIMIT = 400

PR_FIELDS = (
    "number,title,body,state,isDraft,mergeable,mergeStateStatus,baseRefName,author,"
    "headRefName,headRefOid,additions,deletions,reviews,statusCheckRollup,labels,"
    "mergedAt,mergeCommit,headRepository,headRepositoryOwner,isCrossRepository"
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
    query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          reviewThreads(first:100, after:$cursor) {
            nodes { isResolved isOutdated }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor = None
    seen_cursors = set()
    unresolved = 0
    while True:
        args = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr_id}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        data = _gh_json(args)
        if not data or (isinstance(data, dict) and data.get("errors")):
            return None
        try:
            connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            has_next = page_info["hasNextPage"]
        except (KeyError, TypeError):
            return None
        if not isinstance(nodes, list) or not isinstance(has_next, bool):
            return None
        unresolved += sum(
            1 for node in nodes
            if not node.get("isResolved") and not node.get("isOutdated")
        )
        if not has_next:
            return unresolved
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor


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


def is_advisory_review_account(login):
    """Whether a GitHub reviewer identity belongs to review automation."""
    normalized = (login or "").lower()
    return normalized.endswith("[bot]") or normalized in ADVISORY_REVIEW_ACCOUNTS


def check_reviews(pr, threads):
    reviews = pr.get("reviews") or []
    substantive = [r for r in reviews if (r.get("state") or "").upper() != "PENDING"]
    if not substantive:
        return False, "No review on this PR. At least one review is required."
    verdicts = latest_state_per_reviewer(reviews)
    blocking = [
        who for who, state in verdicts.items()
        if state == "CHANGES_REQUESTED"
        and not is_advisory_review_account(who)
    ]
    if blocking:
        return False, (f"{', '.join(blocking)} requested changes and has not re-approved.")
    if threads is None:
        return False, "Could not determine review-thread state; refusing rather than guessing."
    if threads > 0:
        return False, f"{threads} unresolved review thread(s)."

    # A claim means an independent agent is still reviewing. It must block
    # before any external-account or completed-attribution shortcut, otherwise
    # a bot comment can make the PR mergeable while that reviewer is working.
    claimants = label_values(pr, REVIEW_CLAIM_LABEL)
    if claimants:
        return False, (
            f"Review is still in progress: {', '.join(claimants)} holds a "
            f"{REVIEW_CLAIM_LABEL}<agent> claim. Complete the review with "
            "`claim_issue.py --pr <n> --agent <id> --complete-review`, or "
            "release the claim if no review was performed."
        )

    # GitHub cannot tell a self-review from a peer review here: every agent
    # authenticates as the same user, so every review looks like it came from
    # the same person who opened the PR. The agent identity labels are the only
    # thing that distinguishes them.
    # A review from a different non-automation GitHub account is provably not a
    # self-review, but only its latest APPROVED verdict counts. Review apps are
    # advisory: their comments and approvals can inform an agent review, but
    # cannot satisfy the independent-review gate themselves.
    pr_login = ((pr.get("author") or {}).get("login") or "").lower()
    other_accounts = sorted({
        ((r.get("author") or {}).get("login") or "").lower()
        for r in substantive
    } - {"", pr_login})

    # Authorship is required before any approval path can pass. Without the
    # governed author stamp, even a genuine external approval cannot prove the
    # PR did not bypass create_pr.py or establish who must be excluded from
    # same-account agent review.
    authors = label_values(pr, "author:")
    if not authors:
        return False, (
            "PR has no author:<id> label, so the gate cannot prove that the "
            "reviewer is independent. Create PRs with "
            "`scripts/create_pr.py --issue <n> --agent <id>`; stamp the verified "
            "author on a legacy PR before retrying."
        )
    author = authors[0]

    external_approvers = sorted(
        who for who, state in verdicts.items()
        if who.lower() in other_accounts
        and state == "APPROVED"
        and not is_advisory_review_account(who)
    )
    if external_approvers:
        note = (
            f"Approved by external reviewer(s) {', '.join(external_approvers)}, "
            "no unresolved threads."
        )
        if any((lab.get("name") or "") == "same-family-review"
               for lab in (pr.get("labels") or [])):
            note += " ⚠️  Same-family review: no cross-family agent was available."
        return True, note

    # Everything below is the same-account case: agents all authenticate as one
    # GitHub user, so only the identity labels can tell them apart.
    # Only completed attribution counts. Active reviewer claims were rejected
    # above because they represent work still in progress, not attestation.
    reviewers = label_values(pr, REVIEWED_BY_LABEL)
    peers = [r for r in reviewers if r != author]
    if reviewers and not peers:
        return False, (f"The only review is from '{author}', who wrote this PR. "
                       "A self-review does not satisfy the gate.")
    if not reviewers:
        advisory = [a for a in other_accounts if is_advisory_review_account(a)]
        if advisory:
            return False, (
                f"Automated review from {', '.join(advisory)} is advisory; no "
                f"{REVIEWED_BY_LABEL}<agent> label attributes a completed independent "
                "agent review."
            )
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


def is_merged(pr):
    return (pr.get("state") or "").upper() == "MERGED" or bool(pr.get("mergedAt"))


def merge_commit_oid(pr):
    value = pr.get("mergeCommit")
    if isinstance(value, dict):
        return value.get("oid") or ""
    return value or ""


def head_repository_slug(pr):
    repository = pr.get("headRepository") or {}
    owner = pr.get("headRepositoryOwner") or {}
    return repository.get("nameWithOwner") or (
        f"{owner.get('login')}/{repository.get('name')}"
        if owner.get("login") and repository.get("name") else ""
    )


def repository_root():
    """Returns the primary worktree root even when invoked from a linked one."""
    code, common_dir, _ = run_cmd(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False,
    )
    if code != 0 or not common_dir:
        return None
    common_dir = os.path.abspath(common_dir.strip())
    return os.path.dirname(common_dir) if os.path.basename(common_dir) == ".git" else None


def execute_merge(pr_id, pr, merge_method):
    """Runs only the server-side merge, then re-reads authoritative PR state.

    The merge command deliberately does not delete either branch. Cleanup is a
    separate, resumable phase. A non-zero command may still mean GitHub merged
    successfully, so the return code is never interpreted without a re-read.
    """
    env = dict(os.environ, ARU_ALLOW_MAIN_PUSH="1")
    merge_cmd = ["gh", "pr", "merge", str(pr_id), f"--{merge_method}"]
    head_sha = pr.get("headRefOid")
    if head_sha:
        merge_cmd += ["--match-head-commit", head_sha]
    else:
        print("[WARN] No head SHA available; merging without pinning it.", file=sys.stderr)

    proc = subprocess.run(merge_cmd, capture_output=True, text=True, env=env, check=False)
    fresh = fetch_pr(pr_id)
    if not fresh:
        return None, "Could not re-read the PR after the merge command."
    if not is_merged(fresh):
        detail = (proc.stderr or proc.stdout or "merge command returned no detail").strip()
        return None, f"GitHub still reports {fresh.get('state', '?')}; {detail}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no command detail").strip()
        return fresh, (
            f"GitHub reports merged even though the merge command exited "
            f"{proc.returncode}: {detail}"
        )
    return fresh, "GitHub accepted the merge."


def find_branch_worktree(porcelain, branch):
    """Returns the exact branch's worktree path and HEAD from porcelain data."""
    expected_ref = f"refs/heads/{branch}"
    for block in porcelain.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
        if fields.get("branch") == expected_ref:
            return fields.get("worktree"), fields.get("HEAD")
    return None, None


def prune_worktree(repo_root, branch, expected_sha):
    """Deregisters the exact worktree after atomically retaining its directory."""
    if not branch or not expected_sha:
        return False, "Branch and gated head SHA are required; no worktree removed."
    code, out, _ = run_cmd(["git", "worktree", "list", "--porcelain"], check=False, cwd=repo_root)
    if code != 0:
        return False, "Could not list worktrees."
    path, actual_sha = find_branch_worktree(out, branch)
    if not path:
        return True, "Worktree already absent."
    if actual_sha != expected_sha:
        return False, (
            f"Worktree {path} now points to {actual_sha or 'unknown'}, not gated head "
            f"{expected_sha}; left untouched."
        )
    if os.path.abspath(path) == os.path.abspath(repo_root):
        return False, "Refusing to remove the primary worktree."
    retained_root = os.path.join(repo_root, ".worktrees", ".retained")
    retained_path = os.path.join(
        retained_root, f"{expected_sha[:12]}-{os.path.basename(path)}"
    )
    if not os.path.exists(path):
        if not os.path.isdir(retained_path):
            return False, f"Worktree path {path} disappeared; no retained copy found."
        code, _, err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if code == 0:
            return True, f"Worktree already retained at {retained_path}; registration pruned."
        return False, f"Worktree retained at {retained_path}; deregistration failed: {err.strip()}"
    marker = os.path.join(path, ".git")
    try:
        with open(marker, encoding="utf-8") as marker_file:
            marker_text = marker_file.read().strip()
    except OSError as exc:
        return False, f"Could not read worktree metadata {marker}: {exc}"
    if not marker_text.startswith("gitdir: "):
        return False, f"Unexpected worktree metadata in {marker}; left untouched."
    admin_dir = os.path.realpath(marker_text.split(": ", 1)[1])
    allowed_admin_root = os.path.realpath(
        os.path.join(repo_root, ".git", "worktrees")
    )
    try:
        inside_admin_root = os.path.commonpath(
            [admin_dir, allowed_admin_root]
        ) == allowed_admin_root
    except ValueError:
        inside_admin_root = False
    if not inside_admin_root:
        return False, f"Worktree metadata points outside {allowed_admin_root}; left untouched."

    head_lock = os.path.join(admin_dir, "HEAD.lock")
    try:
        lock_fd = os.open(head_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return False, f"Could not lock worktree HEAD for exact ownership check: {exc}"
    ref_lock_fd = None
    ref_lock = ""
    try:
        ref_lock_root = os.path.realpath(
            os.path.join(repo_root, ".git", "refs", "heads")
        )
        ref_lock = os.path.realpath(
            os.path.join(ref_lock_root, f"{branch}.lock")
        )
        try:
            inside_ref_root = os.path.commonpath(
                [ref_lock, ref_lock_root]
            ) == ref_lock_root
        except ValueError:
            inside_ref_root = False
        if not inside_ref_root:
            return False, f"Branch lock points outside {ref_lock_root}; left untouched."
        try:
            os.makedirs(os.path.dirname(ref_lock), exist_ok=True)
            ref_lock_fd = os.open(
                ref_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except OSError as exc:
            return False, f"Could not lock branch ref for exact ownership check: {exc}"

        ref_code, current_ref, ref_err = run_cmd(
            ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
            check=False,
            cwd=path,
        )
        sha_code, current_sha, sha_err = run_cmd(
            ["git", "rev-parse", "HEAD"], check=False, cwd=path
        )
        if ref_code != 0 or sha_code != 0:
            detail = ref_err.strip() or sha_err.strip()
            return False, f"Could not revalidate locked worktree ownership: {detail}"
        if current_ref.strip() != f"refs/heads/{branch}" or current_sha.strip() != expected_sha:
            return False, (
                f"Worktree ownership changed to {current_ref.strip()} at "
                f"{current_sha.strip()}; left untouched."
            )
        status_code, status, status_err = run_cmd(
            [
                "git", "status", "--porcelain", "--untracked-files=all",
                "--ignored=matching",
            ],
            check=False,
            cwd=path,
        )
        if status_code != 0:
            return False, f"Could not inspect worktree {path}: {status_err.strip()}"
        if status:
            return False, (
                f"Worktree {path} has tracked, untracked, or ignored files; left untouched."
            )
        if os.path.exists(retained_path):
            return False, f"Retention destination already exists: {retained_path}"
        try:
            os.makedirs(retained_root, exist_ok=True)
            os.rename(path, retained_path)
        except OSError as exc:
            return False, f"Could not atomically retain worktree {path}: {exc}"
        code, _, err = run_cmd(
            ["git", "worktree", "remove", path], check=False, cwd=repo_root
        )
        if code == 0:
            return True, f"Retained worktree at {retained_path}; registration pruned."
        return False, (
            f"Worktree retained at {retained_path}; deregistration failed: {err.strip()}"
        )
    finally:
        if ref_lock_fd is not None:
            os.close(ref_lock_fd)
            if os.path.exists(ref_lock):
                os.unlink(ref_lock)
        os.close(lock_fd)
        if os.path.exists(head_lock):
            os.unlink(head_lock)


def retain_local_branch(repo_root, branch, expected_sha):
    """Leaves the local ref intact because Git cannot lease worktree attachment.

    A compare-and-delete can protect the ref OID, but it cannot atomically stop
    another process from attaching a new worktree to that ref. Keeping the
    local branch is the only fail-closed behavior in a concurrent factory.
    """
    if not branch or not expected_sha:
        return False, "Branch and gated head SHA are required; local branch state is unknown."
    code, actual_sha, _ = run_cmd(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False, cwd=repo_root,
    )
    if code != 0:
        return True, "Local branch already absent."
    if actual_sha.strip() != expected_sha:
        return True, (
            f"Local branch {branch} was reused at {actual_sha.strip() or 'unknown'}; "
            "unrelated ref retained."
        )
    return True, (
        f"Retained local branch {branch}; Git cannot atomically lease worktree "
        "attachment during ref deletion."
    )


def delete_remote_branch(repo_root, branch, expected_sha, head_repo_slug):
    if not branch or not expected_sha or not head_repo_slug:
        return False, (
            "Branch, gated head SHA, and head repository are required; "
            "no remote branch removed."
        )
    base_repo_slug = get_repo_slug()
    if not base_repo_slug:
        return False, "Could not identify the base repository; no remote branch removed."
    remote = (
        "origin"
        if head_repo_slug == base_repo_slug
        else f"https://github.com/{head_repo_slug}.git"
    )
    ref = f"refs/heads/{branch}"
    code, out, err = run_cmd(
        ["git", "ls-remote", "--heads", remote, ref], check=False, cwd=repo_root
    )
    if code != 0:
        return False, f"Could not inspect {head_repo_slug} branch {branch}: {err.strip()}"
    if not out:
        return True, "Remote branch already absent."
    actual_sha = out.split()[0] if out.split() else ""
    if actual_sha != expected_sha:
        return False, (
            f"Remote branch {head_repo_slug}:{branch} now points to "
            f"{actual_sha or 'unknown'}, not gated head {expected_sha}; left untouched."
        )
    code, _, err = run_cmd(
        [
            "git", "push", f"--force-with-lease={ref}:{expected_sha}",
            remote, f":{ref}",
        ],
        check=False,
        cwd=repo_root,
    )
    if code == 0:
        return True, f"Deleted remote branch {head_repo_slug}:{branch}."
    return False, (
        f"Could not atomically delete remote branch {head_repo_slug}:{branch}; "
        f"it may have changed: {err.strip()}"
    )


def ensure_issue_closed(issue_num):
    issue = _gh_json(["gh", "issue", "view", str(issue_num), "--json", "state"])
    if issue is None:
        return False, f"Could not read issue #{issue_num}."
    if (issue.get("state") or "").upper() == "CLOSED":
        return True, f"Issue #{issue_num} already closed."
    code, _, err = run_cmd(
        ["gh", "issue", "close", str(issue_num), "--reason", "completed"], check=False
    )
    if code == 0:
        return True, f"Closed issue #{issue_num}."
    return False, f"Could not close issue #{issue_num}: {err.strip()}"


def reconcile_issue_done(issue_num):
    if update_status(issue_num, "Done", require_board=True):
        return True, f"Issue #{issue_num} board and status label reconciled to Done."
    return False, f"Could not reconcile issue #{issue_num} to Done."


def clear_labels(kind, number, prefix):
    data = _gh_json(["gh", kind, "view", str(number), "--json", "labels"])
    if data is None:
        return False, f"Could not read {kind} #{number} labels."
    names = [
        label.get("name", "") for label in (data.get("labels") or [])
        if label.get("name", "").startswith(prefix)
    ]
    for name in names:
        code, _, err = run_cmd(
            ["gh", kind, "edit", str(number), "--remove-label", name], check=False
        )
        if code != 0:
            return False, f"Could not remove {name} from {kind} #{number}: {err.strip()}"
    noun = "claims" if names else "claim"
    return True, f"{kind.title()} #{number} {prefix}{noun} cleared or already absent."


def clear_issue_claims(issue_num):
    return clear_labels("issue", issue_num, "agent:")


def clear_review_claims(pr_num):
    return clear_labels("pr", pr_num, REVIEW_CLAIM_LABEL)


def clear_merger_claims(pr_num):
    return clear_labels("pr", pr_num, MERGER_CLAIM_LABEL)


def evaluate_dod(pr, issue_bodies, threads):
    """Runs every Definition-of-Done check without merging.

    Returns ``(ok, gates)`` where ``gates`` is a list of
    ``(name, passed, message)`` in evaluation order. Shared by ``--dry-run``
    and the merge work picker so eligibility cannot drift from the gate.
    """
    issue_nums = linked_issues(pr.get("body"))
    gates = [
        ("open", *check_open(pr)),
        ("issue link", *check_issue_link(pr)),
        ("ci", *check_ci(pr)),
        ("review", *check_reviews(pr, threads)),
        ("rebased", *check_rebased(pr)),
        ("size", *check_size(pr)),
    ]
    for num in issue_nums:
        gates.append((f"accept #{num}", *check_acceptance(num, issue_bodies.get(num, ""))))
    ok = all(passed for _, passed, _ in gates)
    return ok, gates


def dod_status(pr_id):
    """Fetch-and-evaluate helper for callers that only need pass/fail + reason.

    Returns ``(ok, reason)``. ``ok`` is True only when every gate passes.
    Fetch or thread-query failures fail closed with ``ok=False``.
    """
    pr = fetch_pr(pr_id)
    if not pr:
        return False, "could not fetch pull request"
    if is_merged(pr):
        return True, "already merged; close-out may still be needed"
    issue_nums = linked_issues(pr.get("body"))
    if not issue_nums:
        return False, "PR body has no Closes #<issue>"
    issue_bodies = {}
    for num in issue_nums:
        issue = _gh_json(["gh", "issue", "view", str(num), "--json", "body"])
        if issue is None:
            return False, f"could not read issue #{num}"
        issue_bodies[num] = issue.get("body") or ""
    threads = unresolved_threads(pr_id)
    ok, gates = evaluate_dod(pr, issue_bodies, threads)
    if ok:
        return True, "every Definition-of-Done gate passed"
    blocked = [name for name, passed, _ in gates if not passed]
    return False, f"unmet: {', '.join(blocked)}"


def run_closeout(pr, issue_nums, repo_root):
    """Runs every idempotent close-out step, even after an earlier failure."""
    try:
        os.chdir(repo_root)
    except OSError as exc:
        print(f"\n=== Post-merge close-out ===\n  ❌ working directory  {exc}")
        return False
    branch = pr.get("headRefName") or ""
    expected_sha = pr.get("headRefOid") or ""
    head_repo_slug = head_repository_slug(pr)
    steps = [
        ("worktree", lambda: prune_worktree(repo_root, branch, expected_sha)),
        ("local branch", lambda: retain_local_branch(repo_root, branch, expected_sha)),
        ("remote branch", lambda: delete_remote_branch(
            repo_root, branch, expected_sha, head_repo_slug
        )),
    ]
    for num in issue_nums:
        steps.extend([
            (f"close #{num}", lambda num=num: ensure_issue_closed(num)),
            (f"done #{num}", lambda num=num: reconcile_issue_done(num)),
            (f"issue claim #{num}", lambda num=num: clear_issue_claims(num)),
        ])
    steps.append(("review claim", lambda: clear_review_claims(pr.get("number"))))
    steps.append(("merger claim", lambda: clear_merger_claims(pr.get("number"))))

    all_ok = True
    print("\n=== Post-merge close-out ===")
    for name, action in steps:
        try:
            ok, message = action()
        except Exception as exc:  # Keep later recovery steps running.
            ok, message = False, f"Unexpected close-out error: {exc}"
        print(f"  {'✅' if ok else '❌'} {name:<18} {message}")
        all_ok = all_ok and ok
    return all_ok


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
    if not issue_nums:
        print("[ERROR] PR body has no 'Closes #<issue>'; close-out target is unknown.", file=sys.stderr)
        return EXIT_ERROR

    gated_head = pr.get("headRefOid") or "unknown"
    if is_merged(pr):
        print(f"=== Merge execution — PR #{args.pr}: already merged; resuming close-out ===")
        final_pr = pr
        if args.dry_run:
            print("No mutations performed in --dry-run mode.")
            return EXIT_OK
    else:
        issue_bodies = {}
        for num in issue_nums:
            issue = _gh_json(["gh", "issue", "view", str(num), "--json", "body"])
            if issue is None:
                return EXIT_ERROR
            issue_bodies[num] = issue.get("body") or ""

        threads = unresolved_threads(args.pr)
        ok, gates = evaluate_dod(pr, issue_bodies, threads)

        print(f"=== Definition of Done — PR #{args.pr}: {pr.get('title','')} ===")
        blocked = []
        for name, passed, message in gates:
            print(f"  {'✅' if passed else '❌'} {name:<11} {message}")
            if not passed:
                blocked.append(name)
        if blocked:
            print(f"\n🚫 Not merged. Unmet: {', '.join(blocked)}.")
            return EXIT_BLOCKED
        if args.dry_run:
            print("\n✅ Every gate passed. --dry-run, so nothing was merged.")
            return EXIT_OK

        print("\n=== Merge execution ===")
        final_pr, outcome = execute_merge(args.pr, pr, args.merge_method)
        if not final_pr:
            print(f"  ❌ not merged          {outcome}", file=sys.stderr)
            return EXIT_ERROR
        print(f"  ✅ server merge        {outcome}")

    merged_sha = merge_commit_oid(final_pr) or "unknown"
    audit_ok = merged_sha != "unknown"
    print(
        f"AUDIT pr=#{args.pr} gated_head_sha={gated_head} "
        f"merged_sha={merged_sha}"
    )
    if not audit_ok:
        print("[ERROR] GitHub reported merged but supplied no merge commit SHA.", file=sys.stderr)

    root = repository_root()
    if not root:
        print("[ERROR] Merge succeeded but repository root could not be resolved; rerun close-out.", file=sys.stderr)
        return EXIT_ERROR
    closeout_ok = run_closeout(final_pr, issue_nums, root)
    if not closeout_ok or not audit_ok:
        print("\n❌ Merge is complete, but close-out is incomplete. Re-run this command to resume.")
        return EXIT_ERROR
    print("\n✅ Merge and every close-out step completed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
