from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import handoff_contract  # noqa: E402
from aru_project_driver import handoff_evidence  # noqa: E402
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
        raise AssertionError("target issue summaries must come from the complete snapshot")

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
    assert scheduled == [first["target"] and f"handoff:{ORIGIN}:9:{first['contract_digest']}"]
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
    assert scheduled == [f"dependency:{handoff_contract.digest(data)}"]


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
