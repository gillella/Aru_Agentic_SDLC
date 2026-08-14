"""Hermetic board and local-agent adapters for the full fleet lifecycle.

The fixture deliberately models GitHub as the durable queue while exercising
the real :class:`run_fleet.FleetRunner` orchestration boundary.  It performs no
network calls, launches no paid agent, and writes no developer configuration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import run_fleet


@dataclass
class Issue:
    number: int
    touches: tuple[str, ...]
    depends_on: tuple[int, ...] = ()
    high_risk: bool = False
    status: str = "Ready"
    claim: str | None = None
    implementations: int = 0
    workspace_clean: bool = True


@dataclass
class PullRequest:
    number: int
    issue: int
    author: str
    family: str
    ci: str
    head: int = 1
    review_head: int = 0
    reviewed_by: str | None = None
    reviewer_claim: str | None = None
    merger_claim: str | None = None
    feedback_open: bool = False
    feedback_rounds: int = 0
    fixes: int = 0
    verifications: int = 1
    merged: bool = False


class HermeticFleet:
    """Small deterministic substitute for GitHub issues, PRs, and board state."""

    def __init__(self, scenario: dict[str, Any]):
        self.workers = {
            item["agent"]: item["family"] for item in scenario["workers"]
        }
        self.issues = {
            item["number"]: Issue(
                number=item["number"],
                touches=tuple(item["touches"]),
                depends_on=tuple(item.get("depends_on", ())),
                high_risk=item.get("high_risk", False),
                status="Blocked" if item.get("high_risk", False) else "Ready",
            )
            for item in scenario["issues"]
        }
        self.crash_once = set(scenario.get("crash_once", ()))
        self.feedback_once = set(scenario.get("feedback_once", ()))
        self.initial_ci = {
            int(number): state for number, state in scenario.get("initial_ci", {}).items()
        }
        self.crashes_seen: set[tuple[str, int]] = set()
        self.pull_requests: dict[int, PullRequest] = {}
        self.next_pr = 101
        self.human_acknowledged = False
        self.events: list[dict[str, Any]] = []
        self.fake_adapter_launches = 0
        self.paid_adapter_launches = 0
        self.configuration_writes: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> "HermeticFleet":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    def _open_prs(self) -> list[PullRequest]:
        return [pr for pr in self.pull_requests.values() if not pr.merged]

    def _active_claims(self) -> list[str]:
        claims = [
            f"issue:{issue.number}:{issue.claim}"
            for issue in self.issues.values()
            if issue.claim
        ]
        for pr in self._open_prs():
            if pr.reviewer_claim:
                claims.append(f"review:{pr.number}:{pr.reviewer_claim}")
            if pr.merger_claim:
                claims.append(f"merge:{pr.number}:{pr.merger_claim}")
        return claims

    def status(self) -> dict[str, Any]:
        open_issues = [issue for issue in self.issues.values() if issue.status != "Done"]
        open_prs = self._open_prs()
        if not open_issues and not open_prs and not self._active_claims():
            state = "complete"
        elif open_issues and all(issue.status == "Blocked" for issue in open_issues) and not open_prs:
            state = "blocked"
        else:
            state = "waiting"
        return {
            "state": state,
            "summary": f"{state}: hermetic fleet fixture",
            "open_issues_count": len(open_issues),
            "open_prs_count": len(open_prs),
            "active_claims": self._active_claims(),
        }

    def _paths_conflict(self, candidate: Issue) -> bool:
        active_numbers = {
            issue.number
            for issue in self.issues.values()
            if issue.number != candidate.number and issue.status in {"In Progress", "In Review"}
        }
        active_paths = {
            path
            for number in active_numbers
            for path in self.issues[number].touches
        }
        return bool(active_paths.intersection(candidate.touches))

    def _eligible_issue(self, agent: str) -> Issue | None:
        resumed = sorted(
            (issue for issue in self.issues.values() if issue.claim == agent),
            key=lambda issue: issue.number,
        )
        if resumed:
            return resumed[0]
        for issue in sorted(self.issues.values(), key=lambda item: item.number):
            if issue.status != "Ready" or issue.claim:
                continue
            if any(self.issues[number].status != "Done" for number in issue.depends_on):
                continue
            if self._paths_conflict(issue):
                continue
            return issue
        return None

    @staticmethod
    def _merge_ready(pr: PullRequest) -> bool:
        return (
            pr.ci == "green"
            and pr.reviewed_by is not None
            and pr.review_head == pr.head
            and not pr.feedback_open
        )

    def peek(self, agent: str, family: str) -> dict[str, Any]:
        if self.workers[agent] != family:
            raise AssertionError(f"family drift for {agent}")
        authored = sorted(
            (
                pr for pr in self._open_prs()
                if pr.author == agent and (pr.feedback_open or pr.ci == "failure")
            ),
            key=lambda pr: pr.number,
        )
        if authored:
            return {"type": "feedback", "pr": authored[0].number}
        mergeable = sorted(
            (pr for pr in self._open_prs() if self._merge_ready(pr) and not pr.merger_claim),
            key=lambda pr: pr.number,
        )
        if mergeable:
            return {"type": "merge", "pr": mergeable[0].number}
        reviewable = sorted(
            (
                pr for pr in self._open_prs()
                if pr.author != agent
                and pr.ci == "green"
                and not pr.feedback_open
                and pr.review_head != pr.head
                and not pr.reviewer_claim
            ),
            key=lambda pr: (self.workers[pr.author] == family, pr.number),
        )
        if reviewable:
            return {"type": "review", "pr": reviewable[0].number}
        issue = self._eligible_issue(agent)
        if issue:
            return {"type": "issue", "issue": issue.number}
        return {"type": "idle"}

    def _claim_issue(self, issue: Issue, agent: str) -> None:
        if issue.claim not in {None, agent}:
            raise AssertionError(f"duplicate claim on issue #{issue.number}")
        if issue.claim is None:
            issue.claim = agent
            issue.status = "In Progress"
            issue.workspace_clean = False
            self.record("issue_claimed", issue=issue.number, agent=agent)

    def _implement(self, issue: Issue, agent: str) -> int:
        crash_key = (agent, issue.number)
        if issue.number in self.crash_once and crash_key not in self.crashes_seen:
            self.crashes_seen.add(crash_key)
            self.record("worker_crashed", issue=issue.number, agent=agent)
            return 75
        issue.implementations += 1
        if issue.implementations != 1:
            raise AssertionError(f"duplicate implementation of issue #{issue.number}")
        pr = PullRequest(
            number=self.next_pr,
            issue=issue.number,
            author=agent,
            family=self.workers[agent],
            ci=self.initial_ci.get(issue.number, "green"),
        )
        self.next_pr += 1
        self.pull_requests[pr.number] = pr
        issue.claim = None
        issue.status = "In Review"
        self.record("implemented", issue=issue.number, agent=agent, pr=pr.number)
        self.record("handed_off", issue=issue.number, agent=agent, pr=pr.number)
        return 0

    def _address_feedback(self, pr: PullRequest, agent: str) -> int:
        if pr.author != agent:
            raise AssertionError("feedback routed to a non-author")
        if pr.feedback_open:
            pr.feedback_open = False
            pr.fixes += 1
            pr.head += 1
            pr.review_head = 0
            pr.reviewed_by = None
            pr.verifications += 1
            self.record("feedback_fixed", pr=pr.number, issue=pr.issue, agent=agent)
        elif pr.ci == "failure":
            pr.ci = "green"
            pr.verifications += 1
            self.record("ci_remediated", pr=pr.number, issue=pr.issue, agent=agent)
        else:
            raise AssertionError("feedback work had no actionable state")
        return 0

    def _review(self, pr: PullRequest, agent: str) -> int:
        if pr.author == agent or self.workers[pr.author] == self.workers[agent]:
            raise AssertionError("review was not independent and cross-family")
        if pr.reviewer_claim not in {None, agent}:
            raise AssertionError(f"duplicate review claim on PR #{pr.number}")
        pr.reviewer_claim = agent
        self.record("review_claimed", pr=pr.number, issue=pr.issue, agent=agent)
        if pr.issue in self.feedback_once and pr.feedback_rounds == 0:
            pr.feedback_rounds += 1
            pr.feedback_open = True
            pr.reviewer_claim = None
            self.record("feedback_requested", pr=pr.number, issue=pr.issue, agent=agent)
            return 0
        pr.review_head = pr.head
        pr.reviewed_by = agent
        pr.reviewer_claim = None
        self.record("review_completed", pr=pr.number, issue=pr.issue, agent=agent)
        return 0

    def _merge(self, pr: PullRequest, agent: str) -> int:
        pr.merger_claim = agent
        self.record("merge_claimed", pr=pr.number, issue=pr.issue, agent=agent)
        if not self._merge_ready(pr):
            raise AssertionError(f"merge gates did not pass for PR #{pr.number}")
        pr.merged = True
        pr.merger_claim = None
        issue = self.issues[pr.issue]
        issue.status = "Done"
        issue.claim = None
        issue.workspace_clean = True
        self.record("merged", pr=pr.number, issue=pr.issue, agent=agent)
        return 0

    def execute_one(self, agent: str, family: str) -> int:
        self.fake_adapter_launches += 1
        work = self.peek(agent, family)
        work_type = work["type"]
        if work_type == "issue":
            issue = self.issues[work["issue"]]
            self._claim_issue(issue, agent)
            return self._implement(issue, agent)
        if work_type == "feedback":
            return self._address_feedback(self.pull_requests[work["pr"]], agent)
        if work_type == "review":
            return self._review(self.pull_requests[work["pr"]], agent)
        if work_type == "merge":
            return self._merge(self.pull_requests[work["pr"]], agent)
        raise AssertionError("fake adapter launched without eligible work")

    def advance_ci(self, issue_number: int, state: str) -> None:
        if state not in {"pending", "failure", "green"}:
            raise ValueError(f"unsupported CI state: {state}")
        pr = next(pr for pr in self.pull_requests.values() if pr.issue == issue_number)
        pr.ci = state
        self.record("ci_changed", issue=issue_number, pr=pr.number, state=state)

    def acknowledge_human_gate(self, issue_number: int) -> None:
        issue = self.issues[issue_number]
        if not issue.high_risk or issue.status != "Blocked":
            raise AssertionError("no durable high-risk gate to acknowledge")
        self.human_acknowledged = True
        issue.status = "Ready"
        self.record("human_gate_acknowledged", issue=issue_number)

    def final_audit(self) -> dict[str, Any]:
        return {
            "open_issues": [i.number for i in self.issues.values() if i.status != "Done"],
            "open_prs": [pr.number for pr in self._open_prs()],
            "claims": self._active_claims(),
            "not_done": [i.number for i in self.issues.values() if i.status != "Done"],
            "dirty_workspaces": [
                i.number for i in self.issues.values() if not i.workspace_clean
            ],
        }


class HelperRouter:
    """Return fixture JSON through the same command boundary as the real helpers."""

    def __init__(self, fleet: HermeticFleet):
        self.fleet = fleet
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], _cwd: Path) -> run_fleet.CommandResult:
        command = list(argv)
        self.calls.append(command)
        script = Path(command[1]).name
        if script == "fleet_status.py":
            payload = self.fleet.status()
        elif script == "fetch_next_work.py":
            agent = command[command.index("--agent") + 1]
            family = command[command.index("--family") + 1]
            payload = {"work": self.fleet.peek(agent, family)}
        else:
            raise AssertionError(f"unexpected helper command: {command}")
        return run_fleet.CommandResult(0, json.dumps(payload), "")


class FakeLocalAgent:
    """Finite, free adapter implementing one governed lifecycle unit per call."""

    def __init__(self, fleet: HermeticFleet, agent: str, family: str):
        self.fleet = fleet
        self.agent = agent
        self.family = family

    def __call__(self, _argv: Sequence[str], _cwd: Path) -> int:
        return self.fleet.execute_one(self.agent, self.family)
