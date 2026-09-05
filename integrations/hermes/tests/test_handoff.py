from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import handoff_contract  # noqa: E402
from aru_project_driver import handoff_evidence  # noqa: E402
from aru_project_driver import dependencies, scheduler  # noqa: E402
from aru_project_driver.config import Config, DriverError  # noqa: E402
from aru_project_driver.controller import Controller  # noqa: E402
from aru_project_driver.state import State  # noqa: E402


ORIGIN = "owner/source"
TARGET = "owner/target"
HEAD = "a" * 40


def contract(*, issue=42, conditions=None):
    data = {
        "origin": ORIGIN,
        "target": TARGET,
        "issue": issue,
        "source_pr": 17,
        "source_head": HEAD,
        "conditions": conditions or [{"kind": "issue_done", "repo": TARGET, "issue": issue}],
    }
    marker = f"<!-- {handoff_contract.MARKER} {json.dumps(data, separators=(',', ':'))} -->"
    return data, f"Requirements remain authoritative.\n{marker}\n"


def record(number, status="Ready", *, body="", agents=None, errors=None):
    return {
        "number": number, "state": "OPEN", "status": status, "body": body,
        "agents": agents or [], "errors": errors or [], "touches": [f"src/{number}.py"],
        "priority": 2,
    }


class Adapter:
    def __init__(self, repo, source_body, target_status="Ready"):
        self.repo = repo
        self.source_body = source_body
        self.target_status = target_status
        self.proofs = {}
        self.calls = []

    def issue_summary(self, number):
        self.calls.append(("issue_summary", number))
        if self.repo == ORIGIN:
            return {"number": number, "state": "OPEN", "status": "In Progress",
                    "body": self.source_body, "url": f"https://example/{number}"}
        return {"number": number, "state": "OPEN", "status": self.target_status,
                "body": "", "url": f"https://example/{number}"}

    def source_pr(self, number):
        return {"state": "OPEN", "head": HEAD, "issues": [9]}

    def snapshot(self):
        self.calls.append(("snapshot",))
        if self.repo == TARGET:
            _, body = contract()
            return {
                "schema": "aru.driver.snapshot/v1", "repo": TARGET, "complete": True,
                "observed_at": "now", "issues": [record(42, self.target_status, body=body)],
                "prs": [], "ci_available": True,
                "ci": {"available": True, "queued": 0, "online_runners": 1, "free_runners": 1},
            }
        return {
            "schema": "aru.driver.snapshot/v1", "repo": ORIGIN, "complete": True,
            "observed_at": "now", "issues": [], "prs": [], "ci_available": True,
            "ci": {"available": True, "queued": 0, "online_runners": 1, "free_runners": 1},
        }

    def blocked(self, snapshot, status="Ready"):
        return {}

    def dependency_evidence(self, request):
        self.calls.append(("dependency_evidence", request["kind"]))
        return self.proofs.get(request["kind"], {"satisfied": False, "reason": "fixture proof missing"})


class Fixture:
    def __init__(self, tmp_path, *, route=True, target_status="Ready"):
        home = tmp_path / "home"
        source_dir = tmp_path / "source"
        target_dir = tmp_path / "target"
        source_dir.mkdir(parents=True)
        target_dir.mkdir(parents=True)
        lanes = {}
        for identity, repo, account in (("source-agent", ORIGIN, "source-account"),
                                        ("target-agent", TARGET, "target-account")):
            lanes[identity] = {
                "family": "openai-codex", "capacity_key": account, "projects": [repo],
                "command": [sys.executable, "-c", "pass", "{prompt}"],
                "capacity_command": [sys.executable, "-c", "pass"],
                "probe_command": [sys.executable, "-c", "pass"],
            }
        projects = {
            ORIGIN: {"repo_dir": str(source_dir), "lanes": ["source-agent"],
                     "handoff_to": [TARGET] if route else []},
            TARGET: {"repo_dir": str(target_dir), "lanes": ["target-agent"],
                     "handoff_to": []},
        }
        raw = {"version": 1, "hermes_home": str(home), "hermes_repo": str(tmp_path / "runtime"),
               "state_dir": str(home / "state/aru_project_driver"),
               "kernel_root": str(tmp_path / "kernel"), "projects": projects, "lanes": lanes}
        path = tmp_path / "config.json"
        path.write_text(json.dumps(raw))
        self.config = Config(path)
        self.state = State(self.config.state_dir)
        for repo in (ORIGIN, TARGET):
            data = self.state.project(repo)
            data["enabled"] = True
            self.state.save(repo, data)
        _, source_body = contract()
        self.adapters = {
            ORIGIN: Adapter(ORIGIN, source_body),
            TARGET: Adapter(TARGET, source_body, target_status),
        }
        self.controller = Controller(
            self.config,
            adapter_factory=lambda *args: self.adapters[args[-1]],
            availability=lambda *_: {"available": True},
            sync_reviews=lambda *_: {},
        )


def test_contract_requires_one_exact_target_done_condition():
    data, body = contract()
    assert handoff_contract.parse(body, ORIGIN) == data
    with pytest.raises(DriverError, match="exactly one"):
        handoff_contract.parse(body + body, ORIGIN)
    broken = {**data, "conditions": [{"kind": "pr_merged", "repo": TARGET,
                                       "pr": 17, "head": HEAD}]}
    with pytest.raises(DriverError, match="Done condition"):
        handoff_contract.validate(broken, ORIGIN)
    missing_source_pr = {key: value for key, value in data.items() if key != "source_pr"}
    with pytest.raises(DriverError, match="unsupported fields"):
        handoff_contract.validate(missing_source_pr, ORIGIN)


def test_github_crlf_marker_and_horizontal_whitespace_are_accepted():
    data, body = contract()
    body = body.replace("<!--", " \t<!--").replace("-->\n", "--> \t\n")
    assert handoff_contract.parse(body.replace("\n", "\r\n"), ORIGIN) == data


def test_handoff_validates_target_and_deduplicates_delivery(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    scheduled = []
    monkeypatch.setattr(
        "aru_project_driver.scheduler.schedule_wake",
        lambda *args, **kwargs: scheduled.append(kwargs["event_key"]) or {},
    )
    first = fixture.controller.handoff(ORIGIN, 9)
    duplicate = fixture.controller.handoff(ORIGIN, 9)
    assert first["accepted"] is True and first["delivered"] is True
    assert duplicate["accepted"] is True and duplicate["duplicate"] is True
    assert scheduled == [f"handoff:{ORIGIN}:9:{first['contract_digest']}"]
    assert len(fixture.state.project(TARGET)["events"]) == 1


def test_handoff_fails_closed_when_target_is_backlog_or_route_missing(tmp_path):
    fixture = Fixture(tmp_path, target_status="Backlog")
    result = fixture.controller.handoff(ORIGIN, 9)
    assert result["accepted"] is False
    assert any("explicit triage" in blocker for blocker in result["target"]["blockers"])
    missing = Fixture(tmp_path / "missing", route=False)
    with pytest.raises(DriverError, match="explicit route"):
        missing.controller.handoff(ORIGIN, 9)


def test_dependency_event_requires_all_proofs_then_wakes_source_once(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    conditions = [
        {"kind": "issue_done", "repo": TARGET, "issue": 42},
        {"kind": "pr_merged", "repo": TARGET, "pr": 17, "head": HEAD},
    ]
    data, body = contract(conditions=conditions)
    fixture.adapters[ORIGIN].source_body = body
    fixture.adapters[TARGET].proofs = {
        "issue_done": {"satisfied": True}, "pr_merged": {"satisfied": True},
    }
    scheduled = []
    monkeypatch.setattr(
        "aru_project_driver.scheduler.schedule_wake",
        lambda *args, **kwargs: scheduled.append(kwargs["event_key"]) or {},
    )
    result = fixture.controller.dependency_event(ORIGIN, 9)
    again = fixture.controller.dependency_event(ORIGIN, 9)
    assert result["accepted"] is True and result["wakeAgent"] is True
    assert again["accepted"] is False
    assert scheduled == [f"dependency:{ORIGIN}:9:{handoff_contract.digest(data)}"]


def test_dependency_event_reports_unsatisfied_proof_without_wake(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    monkeypatch.setattr(
        "aru_project_driver.scheduler.schedule_wake",
        lambda *args, **kwargs: pytest.fail("unsatisfied dependency must not wake"),
    )
    result = fixture.controller.dependency_event(ORIGIN, 9)
    assert result["accepted"] is False and result["wakeAgent"] is False
    assert "fixture proof missing" in result["blockers"]


class GithubBridge:
    repo = TARGET

    class Common:
        @staticmethod
        def gh_json(args):
            endpoint = args[1]
            if endpoint.endswith("pulls/17"):
                return {"number": 17, "head": {"sha": HEAD}, "merged": True,
                        "merged_at": "now", "merge_commit_sha": "b" * 40}
            if endpoint.endswith("releases/tags/v1"):
                return {"tag_name": "v1", "draft": False, "prerelease": False,
                        "published_at": "now", "html_url": "https://release"}
            if endpoint.endswith("commits/refs%2Ftags%2Fv1"):
                return {"sha": "c" * 40}
            if endpoint.endswith("compare/" + "b" * 40 + "..." + "c" * 40):
                return {"status": "ahead"}
            raise AssertionError(endpoint)

    common = Common()


def test_release_evidence_checks_declared_head_and_published_tag():
    result = handoff_evidence.evidence(GithubBridge(), {
        "kind": "release_contains_pr", "repo": TARGET, "tag": "v1",
        "pr": 17, "head": HEAD,
    })
    assert result["satisfied"] is True and result["commit"] == "c" * 40


@pytest.mark.parametrize("pr", [
    {"state": "OPEN", "head": "b" * 40, "issues": [9]},
    {"state": "CLOSED", "head": HEAD, "issues": [9]},
    {"state": "OPEN", "head": HEAD, "issues": [10]},
])
def test_stale_source_blocks_delivery_and_return(tmp_path, monkeypatch, pr):
    fixture = Fixture(tmp_path)
    fixture.adapters[ORIGIN].source_pr = lambda _: pr
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("stale wake"))
    for action in (fixture.controller.handoff, fixture.controller.dependency_event):
        with pytest.raises(DriverError, match="source PR head or linked issue"):
            action(ORIGIN, 9)


def test_dependency_revoked_during_final_source_read_prevents_return(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    answers = iter([True, False])
    fixture.adapters[TARGET].dependency_evidence = lambda _: {"satisfied": next(answers)}
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("revoked wake"))
    result = fixture.controller.dependency_event(ORIGIN, 9)
    assert not result["accepted"] and not result["wakeAgent"]
    assert result["blockers"] == ["dependency proof changed before return delivery"]
    assert fixture.state.project(ORIGIN)["events"] == []


def test_source_head_advanced_during_target_validation_prevents_delivery(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    heads = iter([HEAD, "b" * 40])
    fixture.adapters[ORIGIN].source_pr = lambda _: {
        "state": "OPEN", "head": next(heads), "issues": [9],
    }
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("stale wake"))
    with pytest.raises(DriverError, match="source PR head"):
        fixture.controller.handoff(ORIGIN, 9)


@pytest.mark.parametrize("repo", [ORIGIN, TARGET])
def test_stop_prevents_cross_project_delivery(tmp_path, monkeypatch, repo):
    fixture = Fixture(tmp_path)
    state = fixture.state.project(repo)
    state["enabled"] = False
    fixture.state.save(repo, state)
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("stopped wake"))
    result = fixture.controller.handoff(ORIGIN, 9)
    assert result["accepted"] is False and result["delivered"] is False


def test_missing_route_also_blocks_dependency_return(tmp_path):
    fixture = Fixture(tmp_path, route=False)
    fixture.adapters[TARGET].proofs["issue_done"] = {"satisfied": True}
    with pytest.raises(DriverError, match="explicit route"):
        fixture.controller.dependency_event(ORIGIN, 9)


def test_target_board_mismatch_and_no_capacity_are_explicit_blockers(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.adapters[TARGET].issue_summary = lambda n: {"state": "OPEN", "status": "Backlog"}
    fixture.controller.available = lambda *_: {"available": False, "reason": "quota exhausted"}
    result = fixture.controller.handoff(ORIGIN, 9)
    assert not result["accepted"]
    assert "target issue and linked Project evidence changed" in result["target"]["blockers"]
    assert "target has no currently available coding lane" in result["target"]["blockers"]


def test_scheduler_failure_does_not_poison_delivery_deduplication(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    def fail(*a, **k):
        raise scheduler.SchedulerError("receiver unavailable")
    monkeypatch.setattr(scheduler, "schedule_wake", fail)
    with pytest.raises(scheduler.SchedulerError, match="unavailable"):
        fixture.controller.handoff(ORIGIN, 9)
    assert fixture.state.project(TARGET)["events"] == []
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    assert fixture.controller.handoff(ORIGIN, 9)["delivered"]
    restarted = Controller(fixture.config, adapter_factory=fixture.controller.adapter_factory,
                           availability=fixture.controller.available)
    assert restarted.handoff(ORIGIN, 9)["duplicate"]


def test_heartbeat_discovers_handoff_and_return_without_a_local_dependency_queue(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    source = record(9, "In Progress", body=fixture.adapters[ORIGIN].source_body)
    snapshot = {"issues": [source]}
    plan, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert held == {9} and plan[0]["next_action"] == "handoff"
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    fixture.controller.handoff(ORIGIN, 9)
    fixture.adapters[TARGET].calls.clear()
    plan, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert plan[0]["next_action"] == "wait" and held == {9}
    assert fixture.adapters[TARGET].calls == [("dependency_evidence", "issue_done")]
    fixture.adapters[TARGET].proofs["issue_done"] = {"satisfied": True}
    plan, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert plan[0]["next_action"] == "dependency-satisfied" and held == set()
    fixture.controller.dependency_event(ORIGIN, 9)
    plan, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert plan[0]["next_action"] == "wait" and held == set()


def test_one_plan_shares_target_snapshot_readiness_and_identical_proofs(tmp_path):
    fixture = Fixture(tmp_path)
    data, body = contract()
    fixture.controller._dependency_contract = lambda repo, number: (data, {})
    snapshot = {"issues": [record(n, "In Progress", body=body) for n in (9, 10)]}
    result, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert held == {9, 10} and all(a["next_action"] == "handoff" for a in result)
    assert fixture.adapters[TARGET].calls == [
        ("dependency_evidence", "issue_done"), ("snapshot",), ("issue_summary", 42),
    ]
    fixture.adapters[TARGET].calls.clear()
    fixture.adapters[TARGET].proofs["issue_done"] = {"satisfied": True}
    result, held = dependencies.actions(fixture.controller, ORIGIN, snapshot)
    assert held == set() and all(a["next_action"] == "dependency-satisfied" for a in result)
    assert fixture.adapters[TARGET].calls == [("dependency_evidence", "issue_done")]


def test_dependency_quota_error_enters_global_cooldown(tmp_path):
    fixture = Fixture(tmp_path)
    adapter = fixture.adapters[ORIGIN]
    snapshot = adapter.snapshot()
    snapshot["issues"] = [record(9, "In Progress", body=adapter.source_body)]
    adapter.snapshot = lambda: snapshot
    def unavailable(_condition):
        raise DriverError("GitHub rate limit exhausted")
    fixture.adapters[TARGET].dependency_evidence = unavailable
    fixture.controller.now = lambda: 1000
    result = fixture.controller.tick(ORIGIN)
    assert result["status"] == "degraded"
    assert fixture.state.project(ORIGIN)["cooldown_until"] == 4600
    assert fixture.adapters[TARGET].calls == []


def test_merge_without_required_release_does_not_unlock_source(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    _, fixture.adapters[ORIGIN].source_body = contract(conditions=[
        {"kind": "issue_done", "repo": TARGET, "issue": 42},
        {"kind": "pr_merged", "repo": TARGET, "pr": 17, "head": HEAD},
        {"kind": "release_contains_pr", "repo": TARGET, "tag": "v1", "pr": 17, "head": HEAD},
    ])
    fixture.adapters[TARGET].proofs.update(issue_done={"satisfied": True},
                                           pr_merged={"satisfied": True})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: pytest.fail("premature return"))
    assert not fixture.controller.dependency_event(ORIGIN, 9)["wakeAgent"]


def test_blocked_source_keeps_independent_ready_lane_available(tmp_path):
    fixture = Fixture(tmp_path)
    fixture.config.project(ORIGIN)["lanes"].append("target-agent")
    fixture.config.lanes["target-agent"]["projects"].append(ORIGIN)
    adapter = fixture.adapters[ORIGIN]
    snapshot = adapter.snapshot()
    source = record(9, "In Progress", body=adapter.source_body, agents=["source-agent"])
    snapshot["issues"] = [source, record(10)]
    snapshot["prs"] = [{"number": 17, "head": HEAD, "issues": [9],
                        "author_agent": "source-agent"}]
    adapter.snapshot = lambda: snapshot
    adapter.next_work = lambda _: {"type": "merge", "pr": 17, "head": HEAD}
    adapter.candidates = lambda _snapshot, status: [record(10)] if status == "Ready" else []
    plan = fixture.controller._plan(ORIGIN)
    assert plan["free_lanes"] == ["target-agent"] and plan["ready"] == [10]
    assert all(action["type"] != "merge" for action in plan["actions"])
    assert plan["actions"][0]["type"] == "dependency"


@pytest.mark.parametrize("same", [False, True])
def test_artifact_proof_compares_immutable_consumer_and_release_files(monkeypatch, same):
    import base64
    calls = []
    def read(_bridge, repo, suffix):
        calls.append((repo, suffix))
        if suffix.startswith("commits/"):
            return {"sha": ("a" if repo == ORIGIN else "c") * 40}
        if suffix.startswith("releases/"):
            return {"tag_name": "v1", "draft": False, "prerelease": False, "published_at": "now"}
        content = b"new" if same or repo == TARGET else b"old"
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(content).decode()}
    monkeypatch.setattr(handoff_evidence, "_read", read)
    bridge = GithubBridge()
    bridge.repo = ORIGIN
    result = handoff_evidence.evidence(bridge, {
        "kind": "artifact_matches_release", "repo": ORIGIN, "ref": "main", "path": ".aru/hook.py",
        "release_repo": TARGET, "tag": "v1", "release_path": "hooks/hook.py",
    })
    assert result["satisfied"] is same
    contents = [suffix for _, suffix in calls if suffix.startswith("contents/")]
    assert contents == ["contents/.aru/hook.py?ref=" + "a" * 40,
                        "contents/hooks/hook.py?ref=" + "c" * 40]
