from __future__ import annotations

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver.config import Config, DriverError  # noqa: E402
from aru_project_driver import controller, retries  # noqa: E402
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
        self.free_runners = 1
        self.calls = []
        self.work = {}
        self.fail_branch_once = False
        self.selector = KernelAdapter(harness.config.kernel_root, harness.repo_dir, REPO)

    def snapshot(self):
        self.calls.append(("snapshot",))
        return deepcopy({
            "schema": SCHEMA, "repo": REPO, "complete": True,
            "issues": self.issues, "prs": self.prs, "ci_available": self.ci_available,
            "ci": {"available": self.ci_available, "queued": self.queued,
                   "online_runners": 1, "free_runners": self.free_runners},
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
        self.descriptors = []
        self.on_probe = lambda identity: None
        self.on_launch = lambda receipt: None
        self.kernel = FakeKernel(self)
        self.controller = controller.Controller(
            self.config, adapter_factory=lambda *args: self.kernel,
            availability=self.availability, probe=self.probe, launch=self.launch,
            now=lambda: self.clock,
        )

    def availability(self, config, repo, identity, state):
        lane = config.lane(repo, identity)
        busy = state.capacity_busy(lane["capacity_key"], lane.get("max_sessions", 1))
        return {"available": self.available[identity] and not busy, "reason": "isolated test observation"}

    def probe(self, config, repo, identity, state):
        self.probes.append(identity)
        self.on_probe(identity)
        return self.probe_ok


    def launch(self, config, repo, identity, number, worktree, **kwargs):
        lane = config.lane(repo, identity)
        capacity_key = lane["capacity_key"]
        descriptor, slot = None, 0
        for slot in range(lane.get("max_sessions", 1)):  # first free session slot, like execution.launch
            capacity = self.state.capacity_path(capacity_key, slot)
            capacity.parent.mkdir(parents=True, exist_ok=True)
            candidate = os.open(capacity, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(candidate)
                continue
            descriptor = candidate
            break
        if descriptor is None:
            raise DriverError("shared subscription was reserved by another worker")
        self.descriptors.append(descriptor)
        receipt = {
            "id": f"launched-{identity}-{number}-{len(self.launched)}", "repo": repo, "agent": identity,
            "issue": number, "worktree": worktree, "capacity_key": capacity_key, "capacity_slot": slot,
            "state": "running", "kind": "implementation", "started_at": self.clock, "pid": os.getpid(), **kwargs,
        }
        retries.stamp(config, receipt, kwargs.get("work_type", "claimed_issue"))
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


@pytest.mark.parametrize("operation", ["tick", "reconcile"])
def test_duplicate_claims_degrade_before_sync_or_mutation(harness, operation):
    harness.kernel.issues = [issue(n, status="In Progress", agent="codex-one") for n in (1, 2)]
    result = getattr(harness.controller, operation)(REPO)
    assert result["status"] == "degraded" and "multiple active claims" in result["reason"]
    assert harness.probes == [] and mutations(harness) == []

@pytest.mark.parametrize("operation", ["tick", "reconcile"])
@pytest.mark.parametrize("missing", ["capacity_key", "state", "started_at"])
def test_malformed_worker_degrades_without_launching(harness, operation, missing):
    record = {"id": "damaged", "repo": REPO, "agent": "codex-one", "issue": 1,
              "capacity_key": "account", "state": "running", "started_at": 1}
    del record[missing]
    write_json(harness.state.worker_path("damaged"), record)
    result = getattr(harness.controller, operation)(REPO)
    assert result["status"] == "degraded" and "operational worker" in result["reason"]
    assert harness.probes == [] and mutations(harness) == []

@pytest.mark.parametrize("queued,expected", [(0, 2), (4, 0)])
def test_online_busy_ci_uses_bounded_queue_admission(harness, queued, expected):
    harness.kernel.free_runners = 0
    harness.kernel.queued = queued
    result = harness.controller.reconcile(REPO)
    assert len(result["launched"]) == expected

def test_event_capacity_blocks_before_creating_native_wake(harness, monkeypatch):
    from aru_project_driver import scheduler, state as state_module
    from aru_project_driver.config import DriverError
    monkeypatch.setattr(state_module, "MAX_EVENT_KEYS", 1)
    assert harness.controller.event(REPO, "first", "event", inline=True)["accepted"]
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("full journal must not create a wake"))
    with pytest.raises(DriverError, match="receipt capacity"):
        harness.controller.event(REPO, "second", "event")


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
    assert harness.kernel.calls == []


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
        "state": "exited", "exit_code": 0, "started_at": 900, "finished_at": 900,
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


def test_shared_subscription_with_two_sessions_admits_two_lanes_and_counts_both(harness):
    shared = harness.config.lanes["codex-one"]["capacity_key"]
    harness.config.lanes["claude-one"]["capacity_key"] = shared
    harness.config.lanes["third"] = dict(harness.config.lanes["codex-one"])
    harness.config.project(REPO)["lanes"].append("third")
    harness.available["third"] = True
    for lane in harness.config.lanes.values():
        lane["max_sessions"] = 2
    # Two session slots admit two lanes on the shared account; the third is over the bound.
    ready, blocked = harness.controller._lane_observations(REPO, harness.kernel.snapshot())
    assert len(ready) == 2 and blocked["third"]["reason"] == "shared account already represented"
    result = harness.controller.reconcile(REPO)
    assert len(result["launched"]) == 2 and {item["capacity_key"] for item in harness.launched} == {shared}
    assert {item["capacity_slot"] for item in harness.launched} == {0, 1}
    assert harness.controller._worker_count(REPO) == 2  # two live reservations on one account

@pytest.mark.parametrize("failure", [False, True])
def test_stop_during_precheck_suppresses_native_brain_even_on_failure(harness, failure):
    original = harness.controller._plan
    def plan(repo):
        result = original(repo)
        harness.state.request_stop(repo)
        if failure:
            raise DriverError("in-flight authority read failed")
        return result
    harness.controller._plan = plan
    result = harness.controller.tick(REPO)
    assert result["wakeAgent"] is False
    assert not harness.state.project(REPO)["enabled"]

def test_stop_during_event_scheduling_is_not_a_delivered_wake(harness, monkeypatch):
    from aru_project_driver import scheduler

    def schedule(*a, **k):
        harness.state.request_stop(REPO)
        return {"wake_job_id": "already-admitted"}
    monkeypatch.setattr(scheduler, "schedule_wake", schedule)
    result = harness.controller.event(REPO, "interrupted-event", "event")
    assert result["status"] == "stopped" and not result["wakeAgent"]
    assert not harness.state.has_event(REPO, "interrupted-event")


def waiting_pr(harness):
    harness.kernel.issues = [issue(1, status="In Review", agent="codex-one")]
    harness.kernel.prs = [pr(9, "codex-one", linked=1)]
    harness.kernel.work["codex-one"] = {"type": "review", "pr": 9, "head": HEAD,
                                        "next_action": "review-by-another-account"}


def test_review_wait_is_visible_quiet_and_does_not_launch(harness, monkeypatch):
    waiting_pr(harness)
    monkeypatch.setattr(controller.scheduler, "schedule_wake", lambda *a, **k: pytest.fail("wait must not schedule"))
    expected = {"type": "wait", "reason": "awaiting approval by another GitHub account",
                "agent": "codex-one", "pr": 9, "head": HEAD, "issue": 1}
    for _ in range(2):
        plan = harness.controller._plan(REPO)
        assert plan["actions"] == [expected] and not plan["actionable"]
        assert plan["free_lanes"] == ["claude-one"] and not plan["resumes"]
        assert harness.controller.tick(REPO)["wakeAgent"] is False
        result = harness.controller.reconcile(REPO)
        assert result["status"] == "waiting" and result["actions"] == [expected]
        assert result["launched"] == []
    assert not harness.probes and mutations(harness) == []
    assert not harness.controller._action_due({"type": "review"})


@pytest.mark.parametrize("kind", ["merge", "finalize"])
def test_approved_pr_returns_to_existing_actions(harness, kind):
    waiting_pr(harness)
    harness.kernel.work["codex-one"] = {"type": kind, "pr": 9, "head": HEAD}
    result = harness.controller.reconcile(REPO)
    assert not result["launched"]
    assert result["actions"] == [{"type": kind, "pr": 9, "head": HEAD, "agent": "codex-one", "issue": 1}]
    assert harness.controller._action_due(result["actions"][0])


@pytest.mark.parametrize("state", ["launching", "running", "exited", "launch_failed"])
def test_legacy_review_receipts_are_inert_even_with_obsolete_payload(harness, state):
    waiting_pr(harness)
    record = {"id": "legacy", "kind": "review", "repo": REPO, "agent": "codex-one",
              "issue": 1, "state": state, "capacity_key": "retired", "started_at": 1,
              "quota_decision": {"obsolete": True}, "review": {"obsolete": True}}
    path = harness.state.worker_path(record["id"])
    write_json(path, record)
    before = path.read_bytes()
    assert harness.controller._worker_count(REPO) == 0
    assert harness.controller.tick(REPO)["wakeAgent"] is False
    result = harness.controller.reconcile(REPO)
    assert result["actions"][0]["type"] == "wait" and not result["launched"]
    assert path.read_bytes() == before and harness.state.workers(REPO) == []
    with pytest.raises(DriverError, match="cannot be resumed"):
        harness.state.worker(record["id"])
    assert not harness.probes and mutations(harness) == []


@pytest.mark.parametrize("head", [None, "short", 3])
def test_malformed_review_wait_fails_closed(harness, head):
    waiting_pr(harness)
    harness.kernel.work["codex-one"]["head"] = head
    result = harness.controller.reconcile(REPO)
    assert result["status"] == "degraded" and "full current head" in result["reason"]
    assert not harness.launched and not harness.probes


def test_legacy_review_with_live_lock_does_not_count_as_author_writer(harness):
    waiting_pr(harness)
    path = harness.kernel.branch(1, "codex-one")
    old = harness.launch(harness.config, REPO, "claude-one", 1, path, kind="review", pr=9, head=HEAD)
    assert harness.state.capacity_holder(old["capacity_key"]) == old["id"]
    assert harness.controller._worker_count(REPO) == 0
    # Physical locks stay exclusive, but the old receipt cannot hide a merge action.
    harness.kernel.work["codex-one"] = {"type": "merge", "pr": 9, "head": HEAD}
    result = harness.controller.reconcile(REPO)
    assert result["actions"][0]["type"] == "merge" and not result["launched"]


def test_waiting_pr_leaves_another_lane_available_for_implementation(harness):
    waiting_pr(harness)
    harness.kernel.issues.append(issue(2))
    result = harness.controller.reconcile(REPO)
    assert [(r["agent"], r["issue"], r["kind"]) for r in result["launched"]] == [("claude-one", 2, "implementation")]
    assert result["actions"][0]["type"] == "wait"
