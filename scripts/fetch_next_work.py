#!/usr/bin/env python3
"""fetch_next_work.py - answers "what should I do next?" for one agent.

The issue picker only ever answered "which issue do I implement?", so a fleet
of agents that all prefer fresh issues buries the board in unreviewed PRs.
Review is not a CI job here - agents run this loop under their own
subscriptions and no provider API keys exist - so review has to be work an
agent claims off the board like anything else.

Three work types, in strict priority order:

  1. feedback  - a PR I authored has requested changes or unresolved threads
  2. review    - an eligible PR is waiting for someone to review it
  3. issue     - nothing to finish, so start something new

Finishing beats starting. That ordering is the whole point: it is what stops
the review queue growing faster than it drains.

  python3 fetch_next_work.py --agent agent-1 --json
  python3 fetch_next_work.py --agent agent-1 --family anthropic --claim

Review eligibility:

  | rule                          | hard? |
  |-------------------------------|-------|
  | nobody else holds reviewer:*  | hard  |
  | author:<id> is not me         | hard  |
  | family:<f> is not mine        | soft  |
  | CI green                      | hard  |
  | not a draft                   | hard  |
  | review rounds < cap           | hard  |

The family rule must be soft. An all-Claude fleet with a hard rule has zero
eligible reviewers, nothing gets reviewed, and merge_pr.py blocks everything -
a deadlock. After a PR has waited past the threshold, any *different agent* may
review it and the PR is labelled `same-family-review` so the degradation shows.

A different agent is worth a great deal on its own: a fresh session has no
memory of writing the code and no attachment to its choices. A different family
adds diverse blind spots on top of that; it is not the whole value.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

from claim_issue import (
    EXIT_CONFLICT,
    EXIT_OK,
    claim_review,
    reap_stale_reviews,
    reviewed_by,
)
from common import list_open_issues, run_cmd
from fetch_next_issue import build_candidates, reap_stale_claims

# Beyond this many rounds, another agent pass is thrash rather than progress -
# the audit found a docs PR that went six rounds. Escalate to a human instead.
DEFAULT_ROUND_CAP = 3

# How long a PR waits for a cross-family reviewer before any different agent
# may take it. Long enough that a mixed fleet routes correctly; short enough
# that a single-family fleet is never stuck.
DEFAULT_CROSS_FAMILY_WAIT_MIN = 30

PR_FIELDS = ("number,title,isDraft,labels,reviews,statusCheckRollup,updatedAt,"
             "createdAt,headRefName,body,reviewDecision")


def _label_value(labels: list[str], prefix: str) -> str | None:
    for name in labels:
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def list_open_prs() -> list[dict[str, Any]] | None:
    code, out, err = run_cmd(
        ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", PR_FIELDS],
        check=False,
    )
    if code != 0:
        print(f"[WARN] Could not list PRs: {err.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(out) if out else []
    except json.JSONDecodeError:
        print("[WARN] Could not parse the PR list.", file=sys.stderr)
        return None


def label_names(pr: dict[str, Any]) -> list[str]:
    return [lab.get("name", "") for lab in pr.get("labels", [])]


def _authored_via_branch(pr: dict[str, Any], agent: str) -> bool:
    """Infers authorship from the linked issue's claim when the PR is unstamped.

    `create_pr.py` stamps author:<id> best-effort, so a failed label write or a
    PR predating stamping leaves no author on the PR. The branch still encodes
    the issue number, and that issue still carries the agent:<id> claim of
    whoever implemented it - so authorship survives even when the stamp does
    not. Without this, an agent could be handed its own unstamped PR to review.
    """
    match = re.search(r"issue-(\d+)", pr.get("headRefName") or "", re.IGNORECASE)
    if not match:
        return False
    code, out, _ = run_cmd(
        ["gh", "issue", "view", match.group(1), "--json", "labels",
         "-q", "[.labels[].name] | join(\"\\n\")"],
        check=False,
    )
    if code != 0:
        return False  # Unknown; the label check already said "not mine".
    return f"agent:{agent}" in [line.strip() for line in out.splitlines()]


def ci_state(pr: dict[str, Any]) -> str:
    """Returns 'green', 'red', 'pending', or 'none'.

    A PR with no checks at all is 'none', not 'green'. Reviewing an unverified
    diff wastes the review, and the merge gate refuses it anyway.
    """
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "none"
    pending = False
    for check in rollup:
        status = (check.get("status") or "").upper()
        result = (check.get("conclusion") or check.get("state") or "").upper()
        if result in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
            return "red"
        if (status and status != "COMPLETED" and not result) or result in {
            "", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"
        }:
            pending = True
    return "pending" if pending else "green"


def review_rounds(pr: dict[str, Any]) -> int:
    """Counts submitted reviews, which is how many rounds this PR has had."""
    return len([r for r in (pr.get("reviews") or [])
                if (r.get("state") or "").upper() != "PENDING"])


def waiting_minutes(pr: dict[str, Any]) -> float:
    stamp = pr.get("updatedAt") or pr.get("createdAt")
    try:
        ts = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def needs_my_attention(pr: dict[str, Any], agent: str) -> bool:
    """True when this is my PR and a reviewer has asked for something.

    Deliberately narrow: only changes explicitly requested. A PR merely sitting
    unreviewed is not my problem to fix, it is someone else's to review.
    """
    if _label_value(label_names(pr), "author:") != agent:
        return False
    return (pr.get("reviewDecision") or "").upper() == "CHANGES_REQUESTED"


def review_eligibility(pr: dict[str, Any], agent: str, family: str | None,
                       round_cap: int, cross_family_wait: int) -> dict[str, Any]:
    """Decides whether `agent` may review this PR, and why not if not.

    Returns {eligible, reason, cross_family, degraded}. `degraded` marks a
    same-family review taken only because the wait threshold passed.
    """
    labels = label_names(pr)
    author = _label_value(labels, "author:")
    pr_family = _label_value(labels, "family:")
    holder = reviewed_by(labels)

    def no(reason):
        return {"eligible": False, "reason": reason, "cross_family": False, "degraded": False}

    if pr.get("isDraft"):
        return no("draft")
    if holder and holder != agent:
        return no(f"already being reviewed by '{holder}'")
    if author and author == agent:
        return no("you wrote it")
    if not author and _authored_via_branch(pr, agent):
        # Unstamped PR - stamping is best-effort and legacy PRs predate it.
        # The branch still names the issue, and the issue still carries the
        # agent:<id> claim of whoever implemented it, so authorship is
        # recoverable without the label. Refusing outright would make every
        # legacy PR unreviewable; this refuses only the ones provably mine.
        return no("you wrote it (inferred from the linked issue's claim)")

    decision = (pr.get("reviewDecision") or "").upper()
    if decision in {"APPROVED", "CHANGES_REQUESTED"}:
        # A decided PR is waiting on a human merge or on its author, not on
        # another reviewer. Re-offering it burns the round budget and
        # eventually mislabels an approved PR as needing human review.
        return no(f"already {decision.lower().replace('_', ' ')}")

    rounds = review_rounds(pr)
    if rounds >= round_cap:
        return no(f"{rounds} review rounds already; needs a human")

    state = ci_state(pr)
    if state != "green":
        return no(f"CI is {state}")

    cross = bool(family and pr_family and pr_family != family)
    if cross or not family or not pr_family:
        # Unknown family on either side is treated as cross: there is no
        # evidence of overlap, and blocking on missing metadata would idle the
        # fleet for a labelling gap.
        return {"eligible": True, "reason": "", "cross_family": True, "degraded": False}

    waited = waiting_minutes(pr)
    if waited >= cross_family_wait:
        return {"eligible": True,
                "reason": f"same family '{family}', waited {waited:.0f}m",
                "cross_family": False, "degraded": True}
    return no(f"same family '{family}'; waiting {cross_family_wait - waited:.0f}m more "
              "for a cross-family reviewer")


def mark(pr_number: int, label: str, colour: str, description: str) -> None:
    """Applies an advisory label. Never fatal - it is a signal, not a gate."""
    run_cmd(["gh", "label", "create", label, "--color", colour, "--description", description],
            check=False)
    code, _, err = run_cmd(["gh", "pr", "edit", str(pr_number), "--add-label", label],
                           check=False)
    if code != 0:
        print(f"[WARN] Could not label PR #{pr_number} '{label}': {err.strip()}", file=sys.stderr)


def select(agent: str, family: str | None, round_cap: int, cross_family_wait: int
           ) -> dict[str, Any]:
    """Builds the full picture, then picks by priority."""
    prs = list_open_prs()
    if prs is None:
        # Fail closed. Treating an unreadable queue as empty makes the selector
        # claim new implementation work as though no feedback or review were
        # waiting - growing the queue precisely while it cannot be observed.
        return {"agent": agent, "family": family,
                "work": {"type": "error", "skill": None,
                         "reason": "the pull request queue could not be read"},
                "reviewable_detail": [], "reviewable": [], "skipped_prs": [],
                "escalated_prs": [], "claimable_issues": [],
                "blocked_by_dependencies": [], "blocked_by_file_conflict": [],
                "missing_touches": []}

    # 1. Finish what I started.
    mine = [p for p in prs if needs_my_attention(p, agent)]
    feedback = min(mine, key=lambda p: p["number"]) if mine else None

    # 2. Review someone else's work.
    reviewable, skipped, escalated = [], [], []
    for pr in sorted(prs, key=lambda p: p["number"]):
        verdict = review_eligibility(pr, agent, family, round_cap, cross_family_wait)
        if verdict["eligible"]:
            reviewable.append((pr, verdict))
        else:
            skipped.append({"number": pr["number"], "why": verdict["reason"]})
            if "needs a human" in verdict["reason"]:
                escalated.append(pr["number"])

    # Cross-family first, then degraded same-family, oldest PR first within each.
    reviewable.sort(key=lambda pair: (not pair[1]["cross_family"], -waiting_minutes(pair[0])))

    # 3. Otherwise start something new - unchanged issue selection.
    issues = list_open_issues()
    parts = build_candidates(issues, agent)

    if feedback is not None:
        work = {"type": "feedback", "pr": feedback["number"], "title": feedback["title"],
                "skill": "address-pr-feedback"}
    elif reviewable:
        pr, verdict = reviewable[0]
        work = {"type": "review", "pr": pr["number"], "title": pr["title"],
                "skill": "code-review", "cross_family": verdict["cross_family"],
                "degraded": verdict["degraded"]}
    elif parts["my_in_flight"]:
        issue = parts["my_in_flight"]
        work = {"type": "issue", "issue": issue["number"], "title": issue["title"],
                "skill": "implement-next-issue", "resuming": True}
    elif parts["candidates"]:
        issue = parts["candidates"][0]
        work = {"type": "issue", "issue": issue["number"], "title": issue["title"],
                "skill": "implement-next-issue", "resuming": False}
    else:
        work = {"type": "idle", "skill": None}

    return {
        "agent": agent, "family": family, "work": work,
        # Ordered candidates, so a lost claim race costs one retry rather than
        # sending the agent back through the whole picker.
        "reviewable_detail": [
            {"pr": p["number"], "title": p["title"],
             "cross_family": v["cross_family"], "degraded": v["degraded"]}
            for p, v in reviewable
        ],
        "reviewable": [p["number"] for p, _ in reviewable],
        "skipped_prs": skipped,
        "escalated_prs": escalated,
        "claimable_issues": [i["number"] for i in parts["candidates"]],
        "blocked_by_dependencies": parts["blocked"],
        "blocked_by_file_conflict": parts["conflicted"],
        "missing_touches": parts["missing_touches"],
    }


def main():
    parser = argparse.ArgumentParser(description="Pick the next work item for one agent.")
    parser.add_argument("--agent", required=True, help="Agent id; required for every claim")
    parser.add_argument("--family", default=None,
                        help="This agent's model family (anthropic, openai, ...). "
                             "Omitting it means every PR looks cross-family.")
    parser.add_argument("--claim", action="store_true", help="Claim the selected work item")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--round-cap", type=int, default=DEFAULT_ROUND_CAP)
    parser.add_argument("--cross-family-wait", type=int, default=DEFAULT_CROSS_FAMILY_WAIT_MIN,
                        metavar="MINUTES")
    parser.add_argument("--reap-after", type=int, default=0, metavar="HOURS",
                        help="Release issue and review claims idle longer than HOURS")
    args = parser.parse_args()

    if args.reap_after:
        reap_stale_reviews(args.reap_after)
        reap_stale_claims(list_open_issues(), args.reap_after)

    res = select(args.agent, (args.family or "").lower() or None,
                 args.round_cap, args.cross_family_wait)
    work = res["work"]

    # A PR nobody may review any more must say so on the PR itself, or it sits
    # in the queue invisibly waiting for a human who was never told.
    #
    # Only when actually working. Without --claim this command is an inspection
    # - a probe, a status check, a dry run - and an inspection must not mutate
    # the board.
    if args.claim:
        for number in res["escalated_prs"]:
            mark(number, "needs-human-review", "b60205",
                 "Review round cap reached; an agent pass is no longer useful")

    if args.claim and work["type"] == "review":
        # Walk the candidates: another agent claiming the top one first should
        # cost a retry, not a wasted cycle through the whole picker.
        work["claimed"] = False
        for candidate in res["reviewable_detail"]:
            rc = claim_review(candidate["pr"], args.agent)
            if rc == EXIT_OK:
                work.update({"pr": candidate["pr"], "title": candidate["title"],
                             "cross_family": candidate["cross_family"],
                             "degraded": candidate["degraded"], "claimed": True})
                if candidate["degraded"]:
                    mark(candidate["pr"], "same-family-review", "fbca40",
                         "Reviewed by the author's own model family; no cross-family agent was free")
                break
            if rc == EXIT_CONFLICT:
                print(f"[INFO] PR #{candidate['pr']} was taken; trying the next one.",
                      file=sys.stderr)
                continue
            work["claim_result"] = "error"
            break
        if not work["claimed"] and "claim_result" not in work:
            # Every candidate was taken while we were deciding. Fall through to
            # implementation work rather than idling.
            work["claim_result"] = "all_taken"
            if res["claimable_issues"]:
                from claim_issue import claim_issue
                for number in res["claimable_issues"]:
                    if claim_issue(number, args.agent) == EXIT_OK:
                        work = {"type": "issue", "issue": number,
                                "skill": "implement-next-issue", "title": "",
                                "resuming": False, "claimed": True}
                        # Rebind the result too. Rebinding only the local name
                        # left --json reporting the unclaimed review while the
                        # issue was claimed and In Progress, so the agent would
                        # work the wrong item and strand the real claim.
                        res["work"] = work
                        break
    elif args.claim and work["type"] == "issue" and not work.get("resuming"):
        from claim_issue import claim_issue
        work["claimed"] = claim_issue(work["issue"], args.agent) == EXIT_OK

    if args.as_json:
        print(json.dumps(res, indent=2))
        return

    print("=== Aru_Agentic_SDLC: next work ===")
    print(f"👤 {args.agent}" + (f" ({args.family})" if args.family else " (family unset)"))
    if work["type"] == "feedback":
        print(f"🔁 Your PR #{work['pr']} has requested changes — address it before taking new work.")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "review":
        tag = "cross-family" if work["cross_family"] else "SAME FAMILY (degraded)"
        print(f"🔍 Review PR #{work['pr']} [{tag}]")
        print(f"   → {work['skill']}: {work['title']}")
    elif work["type"] == "issue":
        verb = "Resume" if work.get("resuming") else "Implement"
        print(f"🛠️  {verb} issue #{work['issue']}")
        print(f"   → {work['skill']}: {work['title']}")
    else:
        print("✨ Nothing to do: no reviewable PR and no claimable issue.")

    if res["skipped_prs"]:
        print("\nPRs not offered to you:")
        for item in res["skipped_prs"]:
            print(f"  #{item['number']}: {item['why']}")
    if res["escalated_prs"]:
        print(f"\n🚨 Needs a human: {res['escalated_prs']}")
    if work["type"] != "issue" and res["claimable_issues"]:
        print(f"\nIssues waiting: {res['claimable_issues']}")


if __name__ == "__main__":
    main()
