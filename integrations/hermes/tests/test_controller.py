from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver.config import Config  # noqa: E402
from aru_project_driver.controller import Controller  # noqa: E402
from aru_project_driver.kernel import KernelAdapter, KernelAdapterError, SCHEMA  # noqa: E402
from aru_project_driver.state import State, write_json  # noqa: E402


REPO = "example/project"
HEAD = "a" * 40


def issue(number, *, status="Ready", agent=None, path=None):
    return {
        "number": number, "title": f"Issue {number}", "body": "## Acceptance Criteria\n- [ ] Done",
        "state": "OPEN", "status": status, "agents": [agent] if agent else [],
        "touches": [path or f"src/issue_{number}.py"], "errors": [],
        "boundary_errors": [], "priority": 2, "dependencies": {}, "labels": [],
    }


def pr(number, agent, linked=None):
    return {
        "number": number, "head": HEAD, "author_agent": agent, "author_actor": "test-author",
        "issue": linked, "issues": [linked] if linked else [], "labels": [],
        "touches": [f"review/pr_{number}.py"], "errors": [],
    }


class FakeKernel:
    """Mutable authoritative fixture; no GitHub or kernel process is executed."""

    def __init__(self, harness):
        self.harness = harness
        self.issues = [issue(1), issue(2)]
        self.prs = []
        self.ci_available = True
        self.queued = 0
        self.calls = []
        self.work = {}
        self.continuations = {}
        self.fail_branch_once = False
        self.selector = KernelAdapter(harness.config.kernel_root, harness.repo_dir, REPO)

    def snapshot(self):
        self.calls.append(("snapshot",))
        return deepcopy({
            "schema": SCHEMA, "repo": REPO, "complete": True,
            "issues": self.issues, "prs": self.prs, "ci_available": self.ci_available,
            "ci": {"available": self.ci_available, "queued": self.queued,
                   "online_runners": 1, "free_runners": 1},
        })

    def candidates(self, snapshot, status="Ready"):
        return self.selector.candidates(snapshot, status)

    def blocked(self, snapshot, status="Ready"):
        return self.selector.blocked(snapshot, status)

    def next_work(self, agent):
        self.calls.append(("next_work", agent))
        if agent in self.work:
            return deepcopy(self.work[agent])
        for record in self.issues:
            if record["agents"] == [agent]:
                return {"type": "claimed_issue", "issue": record["number"]}
        return {"type": "idle"}

    def reviewer_continuation(self, number):
        self.calls.append(("reviewer_continuation", number))
        return deepcopy(self.continuations[number])

    def record(self, number):
        return next(record for record in self.issues if record["number"] == number)

    def promote(self, number):
        self.calls.append(("promote", number))
        record = self.record(number)
        assert record["status"] == "Backlog"
        record["status"] = "Ready"
        return deepcopy(record)

    def claim(self, number, agent):
        self.calls.append(("claim", number, agent))
        intent = [item for item in self.harness.state.workers(REPO)
                  if item["issue"] == number and item["agent"] == agent]
        assert len(intent) == 1 and intent[0]["state"] == "claiming"
        assert intent[0]["worktree"] is None  # Receipt precedes both claim and branch.
        record = self.record(number)
        assert record["status"] == "Ready" and record["agents"] == []
        record.update(status="In Progress", agents=[agent])
        return {"issue": number, "agent": agent, "status": "In Progress"}

    def branch(self, number, agent):
        self.calls.append(("branch", number, agent))
        assert self.record(number)["agents"] == [agent]
        if self.fail_branch_once:
            self.fail_branch_once = False
            raise KernelAdapterError("simulated interruption after a confirmed claim")
        worktree = self.harness.repo_dir / ".worktrees" / f"feat-issue-{number}-test"
        worktree.mkdir(parents=True, exist_ok=True)
        return str(worktree)

    def revalidate(self, number, agent=None):
        self.calls.append(("revalidate", number, agent))
        record = self.record(number)
        assert record["agents"] == [agent]
        assert record["status"] in {"In Progress", "In Review"}
        return deepcopy(record)


class Harness:
    def __init__(self, tmp_path):
        self.repo_dir = tmp_path / "consumer"
        self.repo_dir.mkdir()
        home = tmp_path / "hermes-home"
        lanes = {}
        for identity in ("codex-one", "claude-one"):
            lanes[identity] = {
                "family": "openai-codex" if identity.startswith("codex") else "claude-code",
                "capacity_key": f"account-{identity}", "projects": [REPO],
                "command": [sys.executable, "-c", "pass", "{prompt}"],
                "capacity_command": [sys.executable, "-c", "print('unused')"],
                "probe_command": [sys.executable, "-c", "print('unused')"],
            }
        raw = {
            "version": 1, "hermes_home": str(home), "hermes_repo": str(tmp_path / "runtime"),
            "state_dir": str(home / "state/aru_project_driver"), "kernel_root": str(tmp_path / "kernel"),
            "projects": {REPO: {"repo_dir": str(self.repo_dir), "lanes": list(lanes),
                                "max_workers": 2, "max_review_backlog": 4, "auto_triage": False}},
            "lanes": lanes,
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(raw))
        self.config = Config(path)
        self.state = State(self.config.state_dir)
        project = self.state.project(REPO)
        project["enabled"] = True
        self.state.save(REPO, project)
        self.clock = 1000.0
        self.available = {identity: True for identity in lanes}
        self.probe_ok = True
        self.probes = []
        self.launched = []
        self.synced = []
        self.descriptors = []
        self.on_probe = lambda identity: None
        self.on_launch = lambda receipt: None
        self.kernel = FakeKernel(self)
        self.controller = Controller(
            self.config, adapter_factory=lambda *args: self.kernel,
            availability=self.availability, probe=self.probe, launch=self.launch,
            sync_reviews=self.sync_reviews, now=lambda: self.clock,
        )

    def availability(self, config, repo, identity, state):
        busy = state.capacity_busy(config.lane(repo, identity)["capacity_key"])
        return {"available": self.available[identity] and not busy, "reason": "isolated test observation"}

    def probe(self, config, repo, identity, state):
        self.probes.append(identity)
        self.on_probe(identity)
        return self.probe_ok

    def sync_reviews(self, repo, actions):
        self.synced.append((repo, deepcopy(actions)))
        return {"registered": len(actions)}

    def launch(self, config, repo, identity, number, worktree, **kwargs):
        capacity_key = config.lane(repo, identity)["capacity_key"]
        capacity = self.state.capacity_path(capacity_key)
        capacity.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(capacity, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.descriptors.append(descriptor)
        receipt = {
            "id": f"launched-{identity}-{number}", "repo": repo, "agent": identity,
            "issue": number, "worktree": worktree, "capacity_key": capacity_key,
            "state": "running", "started_at": self.clock, "pid": os.getpid(), **kwargs,
        }
        os.ftruncate(descriptor, 0)
        os.write(descriptor, receipt["id"].encode())
        os.fsync(descriptor)
        write_json(self.state.worker_path(receipt["id"]), receipt)
        self.launched.append(receipt)
        self.on_launch(receipt)
        return receipt

    def one_lane(self):
        self.config.project(REPO)["lanes"] = ["codex-one"]
        self.config.project(REPO)["max_workers"] = 1

    def stop(self):
        data = self.state.project(REPO)
        data["enabled"] = False
        self.state.save(REPO, data)

    def close(self):
        for descriptor in self.descriptors:
            os.close(descriptor)


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    try:
        yield value
    finally:
        value.close()


def mutations(harness):
    return [call for call in harness.kernel.calls if call[0] in {"promote", "claim", "branch"}]


def test_two_independent_accounts_fill_once_each(harness):
    result = harness.controller.reconcile(REPO)
    assert result["status"] == "running"
    assert {(item["agent"], item["issue"]) for item in result["launched"]} == {
        ("codex-one", 1), ("claude-one", 2),
    }
    assert len([call for call in mutations(harness) if call[0] == "claim"]) == 2
    assert harness.controller.reconcile(REPO)["launched"] == []
    assert len(harness.launched) == 2


def test_top_conflicting_issue_does_not_starve_independent_ready_work(harness):
    harness.one_lane()
    harness.kernel.issues = [
        issue(1, status="In Progress", agent="external-owner", path="src/**"),
        issue(2, path="src/conflict.py"), issue(3, path="docs/independent.md"),
    ]
    result = harness.controller.reconcile(REPO)
    assert [item["issue"] for item in result["launched"]] == [3]


@pytest.mark.parametrize("enabled", [False, True])
def test_backlog_promotion_requires_explicit_opt_in(harness, enabled):
    harness.one_lane()
    harness.config.project(REPO)["auto_triage"] = enabled
    harness.kernel.issues = [issue(1, status="Backlog")]
    result = harness.controller.reconcile(REPO)
    assert [call for call in mutations(harness) if call[0] == "promote"] == (
        [("promote", 1)] if enabled else []
    )
    assert [item["issue"] for item in result["launched"]] == ([1] if enabled else [])


@pytest.mark.parametrize("change", ["offline", "queue_full", "review_full", "candidate_changed"])
def test_fresh_post_probe_snapshot_blocks_changed_admission(harness, change):
    harness.one_lane()

    def changed(_identity):
        if change == "offline":
            harness.kernel.ci_available = False
        elif change == "queue_full":
            harness.kernel.queued = 4
        elif change == "review_full":
            harness.kernel.prs = [pr(number, "external-owner") for number in range(10, 14)]
        else:
            harness.kernel.record(1)["status"] = "Backlog"

    harness.on_probe = changed
    assert harness.controller.reconcile(REPO)["launched"] == []
    assert mutations(harness) == []


@pytest.mark.parametrize("change", ["offline", "queue_full", "review_full"])
def test_new_admission_rechecked_between_claims(harness, change):
    def changed(_receipt):
        if change == "offline":
            harness.kernel.ci_available = False
        elif change == "queue_full":
            harness.kernel.queued = 4
        else:
            harness.kernel.prs = [pr(number, "external-owner") for number in range(10, 14)]

    harness.on_launch = changed
    result = harness.controller.reconcile(REPO)
    assert [item["issue"] for item in result["launched"]] == [1]
    assert harness.kernel.record(2)["status"] == "Ready"
    assert [call for call in mutations(harness) if call[0] == "claim"] == [("claim", 1, "codex-one")]


def test_journal_before_claim_recovers_interruption_without_claiming_new_work(harness):
    harness.one_lane()
    harness.kernel.fail_branch_once = True
    failed = harness.controller.reconcile(REPO)
    assert failed["status"] == "degraded" and failed["launched"] == []
    records = harness.state.workers(REPO)
    assert len(records) == 1 and records[0]["state"] == "claiming"
    assert records[0]["issue"] == 1 and records[0]["worktree"] is None
    assert harness.kernel.record(1)["agents"] == ["codex-one"]
    harness.clock += 601
    resumed = harness.controller.reconcile(REPO)
    assert [item["issue"] for item in resumed["launched"]] == [1]
    assert resumed["launched"][0]["kind"] == "remediation"
    assert [call for call in mutations(harness) if call[0] == "claim"] == [("claim", 1, "codex-one")]
    assert harness.kernel.record(2)["status"] == "Ready"


def test_identical_actionable_plan_retries_after_failed_probe(harness):
    harness.one_lane()
    first = harness.controller.tick(REPO)
    assert first["wakeAgent"] is True
    harness.probe_ok = False
    assert harness.controller.reconcile(REPO)["launched"] == []
    harness.clock += 601
    second = harness.controller.tick(REPO)
    assert second["wakeAgent"] is True
    assert first["plan"]["fingerprint"] == second["plan"]["fingerprint"]
    assert mutations(harness) == []


@pytest.mark.parametrize("action", ["merge", "finalize"])
def test_pr_convergence_stays_actionable_when_coding_quota_is_unavailable(harness, action):
    harness.one_lane()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [] if action == "finalize" else [pr(9, "codex-one", linked=1)]
    harness.kernel.work["codex-one"] = {"type": action, "issue": 1, "pr": 9, "head": HEAD}
    harness.available["codex-one"] = False
    tick = harness.controller.tick(REPO)
    assert tick["wakeAgent"] is True
    result = harness.controller.reconcile(REPO)
    assert result["actions"][0]["type"] == action
    assert result["launched"] == [] and harness.probes == []
    assert mutations(harness) == []


def test_authored_pr_without_claim_reserves_its_account(harness):
    harness.one_lane()
    harness.kernel.prs = [pr(9, "codex-one")]
    harness.kernel.work["codex-one"] = {"type": "wait", "pr": 9, "head": HEAD}
    result = harness.controller.reconcile(REPO)
    assert result["launched"] == []
    assert mutations(harness) == []
    assert ("next_work", "codex-one") in harness.kernel.calls


def test_unavailable_first_model_does_not_hide_available_alias_on_same_account(harness):
    harness.config.lanes["claude-one"]["capacity_key"] = harness.config.lanes["codex-one"]["capacity_key"]
    harness.available["codex-one"] = False
    result = harness.controller.reconcile(REPO)
    assert [(item["agent"], item["issue"]) for item in result["launched"]] == [("claude-one", 1)]
    assert harness.probes == ["claude-one"]


def test_future_review_deadline_registered_by_tick_without_waking_brain(harness):
    harness.one_lane()
    deadline = datetime.fromtimestamp(harness.clock + 120, timezone.utc).isoformat()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    action = {"type": "wait", "pr": 9, "head": HEAD, "authority": "coderabbit",
              "next_action": "refresh-reviewer", "retry_at": deadline}
    harness.kernel.work["codex-one"] = action
    result = harness.controller.tick(REPO)
    assert result["wakeAgent"] is False
    assert len(harness.synced) == 1
    assert harness.synced[0] == (REPO, [{**action, "agent": "codex-one", "issue": 1}])
    assert harness.probes == [] and mutations(harness) == []


@pytest.mark.parametrize("operation", ["tick", "reconcile"])
def test_duplicate_claims_degrade_before_sync_or_mutation(harness, operation):
    harness.kernel.issues = [issue(n, status="In Progress", agent="codex-one") for n in (1, 2)]
    result = getattr(harness.controller, operation)(REPO)
    assert result["status"] == "degraded" and "multiple active claims" in result["reason"]
    assert harness.probes == [] and harness.synced == [] and mutations(harness) == []


@pytest.mark.parametrize("operation", ["tick", "reconcile"])
@pytest.mark.parametrize("deadline", ["invalid", "2026-09-05T12:00:00"])
def test_invalid_review_deadline_degrades_before_sync_or_mutation(harness, operation, deadline):
    harness.one_lane()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    harness.kernel.work["codex-one"] = {
        "type": "wait", "pr": 9, "head": HEAD, "authority": "coderabbit",
        "next_action": "refresh-reviewer", "retry_at": deadline,
    }
    result = getattr(harness.controller, operation)(REPO)
    assert result["status"] == "degraded" and "deadline" in result["reason"]
    assert harness.probes == [] and harness.synced == [] and mutations(harness) == []


def test_stopped_project_does_not_inspect_or_refill(harness):
    harness.stop()
    assert harness.controller.reconcile(REPO) == {"status": "stopped", "launched": []}
    assert harness.controller.tick(REPO)["wakeAgent"] is False
    assert harness.kernel.calls == [] and harness.probes == [] and harness.synced == []


def test_inline_events_are_idempotent_and_stopped_projects_ignore_them(harness):
    first = harness.controller.event(REPO, "delivery-1", "event", inline=True)
    duplicate = harness.controller.event(REPO, "delivery-1", "event", inline=True)
    assert first["accepted"] is True and first["wakeAgent"] is True
    assert duplicate["accepted"] is False and duplicate["wakeAgent"] is False
    assert len(harness.state.project(REPO)["events"]) == 1
    harness.stop()
    stopped = harness.controller.event(REPO, "delivery-2", "event", inline=True)
    assert stopped["accepted"] is False and stopped["wakeAgent"] is False
    assert len(harness.state.project(REPO)["events"]) == 1
    assert harness.kernel.calls == [] and harness.synced == []


@pytest.mark.parametrize("authority,wake", [
    ("claude-code", True), ("openai-codex", True),
    ("xai-cursor", True), ("google-antigravity", True),
    ("coderabbit", False), ("sourcery", False), ("codeant", False),
])
def test_coding_review_assignment_wakes_brain_while_external_wait_stays_silent(harness, authority, wake):
    harness.one_lane()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    harness.kernel.work["codex-one"] = {
        "type": "wait", "pr": 9, "head": HEAD, "authority": authority,
        "next_action": "await-authoritative-review", "retry_at": None,
    }
    harness.available["codex-one"] = False
    result = harness.controller.tick(REPO)
    assert result["wakeAgent"] is wake
    assert harness.probes == [] and mutations(harness) == []


def test_selected_pr_for_another_issue_is_an_ownership_conflict(harness):
    harness.one_lane()
    harness.kernel.issues = [issue(20, status="In Progress", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=10)]
    harness.kernel.work["codex-one"] = {
        "type": "feedback", "issue": 10, "pr": 9, "head": HEAD,
        "items": [{"body": "A real finding on issue 10's PR."}],
    }
    result = harness.controller.reconcile(REPO)
    assert result["launched"] == []
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "ownership_conflict"
    assert result["actions"][0]["pr"] == 9
    assert harness.probes == [] and mutations(harness) == []


def test_in_review_feedback_resumes_owned_worktree_with_exact_pr_context(harness):
    harness.one_lane()
    record = issue(1, status="In Review", agent="codex-one")
    record["body"] = record["body"].replace("- [ ]", "- [x]")
    harness.kernel.issues = [record]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    harness.kernel.work["codex-one"] = {"type": "feedback", "issue": 1, "pr": 9, "head": HEAD}
    worktree = harness.repo_dir / ".worktrees/feat-issue-1-test"
    worktree.mkdir(parents=True)
    receipt = {
        "id": "finished-author", "repo": REPO, "agent": "codex-one", "issue": 1,
        "state": "exited", "exit_code": 0, "started_at": 900,
        "capacity_key": harness.config.lanes["codex-one"]["capacity_key"],
        "worktree": str(worktree), "pr": 9, "head": HEAD,
    }
    write_json(harness.state.worker_path(receipt["id"]), receipt)
    result = harness.controller.reconcile(REPO)
    assert len(result["launched"]) == 1
    launched = result["launched"][0]
    assert (launched["issue"], launched["pr"], launched["head"], launched["kind"]) == (
        1, 9, HEAD, "remediation",
    )
    assert launched["worktree"] == str(worktree)
    assert harness.kernel.record(1)["status"] == "In Review"
    assert [call for call in mutations(harness) if call[0] == "claim"] == []


def test_project_worker_cap_does_not_count_another_projects_account_holder(harness):
    other = "example/other-project"
    other_dir = harness.repo_dir.parent / "other-project"
    other_worktree = other_dir / ".worktrees/feat-issue-99-test"
    other_worktree.mkdir(parents=True)
    harness.config.projects[other] = {
        "repo_dir": str(other_dir), "lanes": ["codex-one"], "max_workers": 1,
    }
    harness.config.lanes["codex-one"]["projects"].append(other)
    harness.config.project(REPO)["max_workers"] = 1
    other_receipt = harness.launch(harness.config, other, "codex-one", 99, str(other_worktree))
    assert harness.state.capacity_holder(other_receipt["capacity_key"]) == other_receipt["id"]
    assert harness.controller._worker_count(REPO) == 0
    assert harness.controller._worker_count(other) == 1
    result = harness.controller.reconcile(REPO)
    assert [(item["agent"], item["issue"]) for item in result["launched"]] == [("claude-one", 1)]
    assert harness.controller._worker_count(REPO) == 1
    assert harness.controller._worker_count(other) == 1


def test_pending_ci_still_registers_assigned_external_review_deadline_without_brain(harness):
    harness.one_lane()
    deadline = datetime.fromtimestamp(harness.clock + 120, timezone.utc).isoformat()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    opened = pr(9, "codex-one", linked=1)
    opened["labels"] = ["review:coderabbit"]
    harness.kernel.prs = [opened]
    harness.kernel.work["codex-one"] = {
        "type": "wait", "pr": 9, "head": HEAD, "verification": "pending",
    }
    harness.kernel.continuations[9] = {
        "authority": "coderabbit", "next_action": "refresh-reviewer", "retry_at": deadline,
    }
    result = harness.controller.tick(REPO)
    assert result["wakeAgent"] is False
    assert ("reviewer_continuation", 9) in harness.kernel.calls
    assert len(harness.synced) == 1
    assert harness.synced[0] == (REPO, [{
        "type": "wait", "pr": 9, "head": HEAD, "verification": "pending",
        "agent": "codex-one", "issue": 1, **harness.kernel.continuations[9],
    }])
    assert harness.probes == [] and mutations(harness) == []


def test_pending_ci_with_ambiguous_review_authority_fails_closed(harness):
    harness.one_lane()
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    opened = pr(9, "codex-one", linked=1)
    opened["labels"] = ["review:coderabbit", "review:sourcery"]
    harness.kernel.prs = [opened]
    harness.kernel.work["codex-one"] = {
        "type": "wait", "pr": 9, "head": HEAD, "verification": "pending",
    }
    result = harness.controller.tick(REPO)
    assert result["status"] == "degraded"
    assert ("reviewer_continuation", 9) not in harness.kernel.calls
    assert harness.synced == [] and harness.probes == [] and mutations(harness) == []
