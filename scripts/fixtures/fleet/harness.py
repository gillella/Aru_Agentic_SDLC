"""Hermetic board and local-agent adapters for the full fleet lifecycle.

The fixture deliberately models GitHub as the durable queue while exercising
the real :class:`run_fleet.FleetRunner` orchestration boundary.  It performs no
network calls, launches no paid agent, and writes no developer configuration.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import patch

import claim_issue as claim_helpers
import fetch_next_issue
import fetch_next_work
import merge_pr
import run_fleet


@dataclass
class Issue:
    number: int
    touches: tuple[str, ...]
    depends_on: tuple[int, ...] = ()
    high_risk: bool = False
    unresolved_decision: str = ""
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
                unresolved_decision=item.get("unresolved_decision", ""),
                status="Blocked" if item.get("unresolved_decision") else "Ready",
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

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "Ready": "status:ready",
            "In Progress": "status:in-progress",
            "In Review": "status:in-review",
            "Blocked": "status:backlog",
        }[status]

    def _issue_record(self, issue: Issue) -> dict[str, Any]:
        labels = [{"name": self._status_label(issue.status)}]
        if issue.claim:
            labels.append({"name": f"agent:{issue.claim}"})
        dependencies = "\n".join(f"depends-on: #{number}" for number in issue.depends_on)
        touches = ", ".join(issue.touches)
        return {
            "number": issue.number,
            "title": f"fixture issue {issue.number}",
            "body": f"{dependencies}\ntouches: {touches}\nparallel-eligible: true",
            "labels": labels,
            "author": {"login": "fixture-account"},
            "assignees": [],
            "state": "OPEN",
            "updatedAt": "2026-08-14T09:00:00Z",
        }

    def _issue_records(self) -> list[dict[str, Any]]:
        return [
            self._issue_record(issue)
            for issue in self.issues.values()
            if issue.status != "Done"
        ]

    @staticmethod
    def _ci_rollup(state: str) -> list[dict[str, Any]]:
        if state == "green":
            return [{"name": "fixture-ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        if state == "failure":
            return [{"name": "fixture-ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
        return [{"name": "fixture-ci", "status": "IN_PROGRESS", "conclusion": ""}]

    def _pr_labels(self, pr: PullRequest) -> list[str]:
        labels = [f"author:{pr.author}", f"family:{pr.family}"]
        if pr.reviewer_claim:
            labels.append(f"reviewer:{pr.reviewer_claim}")
        if pr.reviewed_by:
            labels.append(f"reviewed-by:{pr.reviewed_by}")
        if pr.merger_claim:
            labels.append(f"merger:{pr.merger_claim}")
        return labels

    def _pr_record(self, pr: PullRequest) -> dict[str, Any]:
        reviews = []
        if pr.reviewed_by:
            reviews.append({
                "state": "COMMENTED",
                "id": pr.reviewed_by,
                "author": {"login": "fixture-account"},
                "submittedAt": "2026-08-14T09:00:00Z",
            })
        return {
            "number": pr.number,
            "title": f"fixture PR {pr.number}",
            "isDraft": False,
            "labels": [{"name": label} for label in self._pr_labels(pr)],
            "reviews": reviews,
            "statusCheckRollup": self._ci_rollup(pr.ci),
            "updatedAt": "2026-08-14T09:00:00Z",
            "createdAt": "2026-08-14T09:00:00Z",
            "headRefName": f"fixture/issue-{pr.issue}",
            "headRefOid": f"head-{pr.head}",
            "body": f"Closes #{pr.issue}",
            "reviewDecision": "",
            "state": "OPEN" if not pr.merged else "MERGED",
            "mergedAt": "2026-08-14T09:30:00Z" if pr.merged else None,
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "additions": 20,
            "deletions": 5,
            "author": {"login": "fixture-account"},
            "_active_review_feedback": [object()] if pr.feedback_open else [],
        }

    def _pr_records(self) -> list[dict[str, Any]]:
        return [self._pr_record(pr) for pr in self._open_prs()]

    def dod_status(self, pr_number: int) -> tuple[bool, str]:
        pr = self.pull_requests[pr_number]
        evidence = {
            "head_oid": f"head-{pr.head}",
            "review_attestations": ([{
                "agent": pr.reviewed_by,
                "head": f"head-{pr.review_head}",
                "github_login": "fixture-account",
            }] if pr.reviewed_by else []),
            "unresolved": 0,
            "unfixed": 0,
            "reviewed_head": bool(pr.reviewed_by and pr.review_head == pr.head),
            "withdrawn": 0,
        }
        issue_body = "## Acceptance Criteria\n- [x] fixture acceptance"
        ok, gates = merge_pr.evaluate_dod(
            self._pr_record(pr), {pr.issue: issue_body}, evidence,
        )
        blocked = [name for name, passed, _message in gates if not passed]
        return ok, "every Definition-of-Done gate passed" if ok else f"unmet: {', '.join(blocked)}"

    def select(self, agent: str, family: str) -> dict[str, Any]:
        if self.workers[agent] != family:
            raise AssertionError(f"family drift for {agent}")
        with (
            patch.object(fetch_next_work, "list_work_prs", side_effect=self._pr_records),
            patch.object(fetch_next_work, "list_open_issues", side_effect=self._issue_records),
            patch.object(fetch_next_work, "dod_status", side_effect=self.dod_status),
            patch.object(
                fetch_next_issue, "repository_owner_login",
                return_value="fixture-account",
            ),
            patch.object(
                fetch_next_issue, "repository_trusted_logins",
                return_value={"fixture-account"},
            ),
        ):
            return fetch_next_work.select(agent, family, round_cap=3, cross_family_wait=30)

    def _set_issue_status(self, issue_number: int, status: str, **_kwargs: Any) -> bool:
        self.issues[issue_number].status = status
        return True

    def _transport_command(self, argv: Sequence[str], **_kwargs: Any) -> tuple[int, str, str]:
        command = list(argv)
        if len(command) < 4 or command[0] != "gh":
            raise AssertionError(f"unexpected claim transport command: {command}")
        kind, action, number = command[1], command[2], int(command[3])
        if kind == "pr" and action == "comment":
            return 0, "", ""
        if action != "edit":
            raise AssertionError(f"unexpected claim transport action: {command}")
        if kind == "issue":
            issue = self.issues[number]
            if "--add-label" in command:
                issue.claim = command[command.index("--add-label") + 1].removeprefix("agent:")
            elif "--remove-label" in command:
                issue.claim = None
            return 0, "", ""
        pr = self.pull_requests[number]
        if "--add-label" in command:
            label = command[command.index("--add-label") + 1]
            if label.startswith("reviewer:"):
                pr.reviewer_claim = label.removeprefix("reviewer:")
            elif label.startswith("reviewed-by:"):
                pr.reviewed_by = label.removeprefix("reviewed-by:")
            elif label.startswith("merger:"):
                pr.merger_claim = label.removeprefix("merger:")
        elif "--remove-label" in command:
            label = command[command.index("--remove-label") + 1]
            if label.startswith("reviewer:"):
                pr.reviewer_claim = None
            elif label.startswith("merger:"):
                pr.merger_claim = None
        return 0, "", ""

    @contextmanager
    def _claim_transport(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                claim_helpers, "get_issue",
                side_effect=lambda number: self._issue_record(self.issues[number]),
            ))
            stack.enter_context(patch.object(claim_helpers, "update_status", self._set_issue_status))
            stack.enter_context(patch.object(claim_helpers, "ensure_label", return_value=True))
            stack.enter_context(patch.object(claim_helpers, "run_cmd", self._transport_command))
            stack.enter_context(patch.object(claim_helpers.time, "sleep", return_value=None))
            stack.enter_context(patch.object(
                claim_helpers, "repository_owner_login", return_value="fixture-account",
            ))
            stack.enter_context(patch.object(
                claim_helpers, "repository_trusted_logins",
                return_value={"fixture-account"},
            ))
            stack.enter_context(patch.object(
                claim_helpers, "_pr_labels",
                side_effect=lambda number: self._pr_labels(self.pull_requests[number]),
            ))
            stack.enter_context(patch.object(
                claim_helpers, "_reviewed_head_for_completion",
                side_effect=lambda number: f"{self.pull_requests[number].head:040x}",
            ))
            yield

    def _claim_with_helpers(self, work: dict[str, Any], agent: str) -> None:
        with self._claim_transport():
            if work["type"] == "issue":
                result = claim_helpers.claim_issue(work["issue"], agent)
            elif work["type"] == "review":
                result = claim_helpers.claim_review(work["pr"], agent)
            elif work["type"] == "merge":
                result = claim_helpers.claim_merge(work["pr"], agent)
            else:
                return
        if result != claim_helpers.EXIT_OK:
            raise AssertionError(f"real claim helper rejected {work}")

    def _claim_issue(self, issue: Issue, agent: str) -> None:
        if issue.claim != agent or issue.status != "In Progress":
            raise AssertionError(f"real claim helper did not claim issue #{issue.number}")
        already_recorded = any(
            event["event"] == "issue_claimed" and event["issue"] == issue.number
            for event in self.events
        )
        if not already_recorded:
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
            if pr.ci == "failure":
                pr.ci = "green"
                self.record("ci_remediated", pr=pr.number, issue=pr.issue, agent=agent)
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
        if pr.reviewer_claim != agent:
            raise AssertionError(f"real claim helper did not claim review of PR #{pr.number}")
        self.record("review_claimed", pr=pr.number, issue=pr.issue, agent=agent)
        if pr.issue in self.feedback_once and pr.feedback_rounds == 0:
            pr.feedback_rounds += 1
            pr.feedback_open = True
            with self._claim_transport():
                result = claim_helpers.release_review(pr.number, agent)
            if result != claim_helpers.EXIT_OK:
                raise AssertionError(f"real claim helper could not release review #{pr.number}")
            self.record("feedback_requested", pr=pr.number, issue=pr.issue, agent=agent)
            return 0
        pr.review_head = pr.head
        with self._claim_transport():
            result = claim_helpers.complete_review(pr.number, agent)
        if result != claim_helpers.EXIT_OK:
            raise AssertionError(f"real claim helper could not complete review #{pr.number}")
        self.record("review_completed", pr=pr.number, issue=pr.issue, agent=agent)
        return 0

    def _merge(self, pr: PullRequest, agent: str) -> int:
        if pr.merger_claim != agent:
            raise AssertionError(f"real claim helper did not claim merge of PR #{pr.number}")
        self.record("merge_claimed", pr=pr.number, issue=pr.issue, agent=agent)
        ready, reason = self.dod_status(pr.number)
        if not ready:
            raise AssertionError(f"real merge gate rejected PR #{pr.number}: {reason}")
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
        work = self.select(agent, family)["work"]
        work_type = work["type"]
        self._claim_with_helpers(work, agent)
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
        if not issue.unresolved_decision or issue.status != "Blocked":
            raise AssertionError("no durable unresolved decision to acknowledge")
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
            payload = self.fleet.select(agent, family)
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
