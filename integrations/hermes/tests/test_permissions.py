"""Task grants and result containment with synthetic workers, never privileged probes."""
from __future__ import annotations

import json
import os
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import driver, execution, permissions, scheduler
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import write_json
from test_controller import Harness, REPO
import test_execution as execution_tests


@pytest.fixture
def synthetic_execution(tmp_path):
    return execution_tests.setup.__wrapped__(tmp_path)


@pytest.fixture
def scoped(tmp_path):
    root = tmp_path / "repo"
    tree = root / ".worktrees" / "fix-issue-638-test"
    tree.mkdir(parents=True)
    home = tmp_path / "home"
    policy = {"version": 1, "lanes": ["model-one"], "python": "/usr/bin/python3", "test_python": "/opt/venv/bin/python",
              "app_runner": "/opt/bin/aru-app", "recovery_epoch": "approved-638"}
    lane = {"family": "claude-code", "capacity_key": "one", "projects": [permissions.FACTORY],
            "command": ["claude-sub", "2", "--model", "claude-opus-5", "--print",
                        "--permission-mode", "acceptEdits", "--permission-prompts", "none", "{prompt}"],
            "capacity_command": ["observer"], "probe_command": ["probe"]}
    raw = {"version": 1, "hermes_home": str(home), "hermes_repo": str(tmp_path / "runtime"),
           "state_dir": str(home / "state/aru_project_driver"), "kernel_root": str(root),
           "projects": {permissions.FACTORY: {"repo_dir": str(root), "lanes": ["model-one"],
                                             "worker_permissions": policy}}, "lanes": {"model-one": lane}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    config = Config(path)
    record = {"id": "scoped", "repo": permissions.FACTORY, "agent": "model-one", "issue": 638,
              "worktree": str(tree), "kind": "implementation", "pr": None, "head": None,
              "prompt": "Do task", "started_at": 1, "state": "exited", "capacity_key": "one"}
    adapter = SimpleNamespace(revalidate=lambda *a, **k: {"touches": ["integrations/hermes/**"]})
    def run(argv, cwd):
        assert cwd == tree
        output = f"{tree}\n{root}/.git\n" if argv[1] == "rev-parse" else "fix/issue-638-test\n"
        return SimpleNamespace(stdout=output, returncode=0)
    return config, record, adapter, run


def compiled(scoped, **kwargs):
    return permissions.compile_policy(*scoped, **kwargs)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("python", "python -c"), ("app_runner", "/"),
    ("python", "/usr/bin/python*"), ("lanes", ["other"]), ("lanes", ["model-one", "model-one"]),
    ("recovery_epoch", ""), ("allowedTools", ["Bash"]),
])
def test_invalid_policy_refused(scoped, field, value):
    config, *_ = scoped
    raw = deepcopy(config.raw)
    raw["projects"][permissions.FACTORY]["worker_permissions"][field] = value
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="worker_permissions"):
        Config(config.path)


@pytest.mark.parametrize("change", ["provider", "bypass", "extra", "other-project"])
def test_no_implicit_provider_or_global_permission_conversion(scoped, change):
    config, *_ = scoped
    raw = deepcopy(config.raw)
    lane = raw["lanes"]["model-one"]
    if change == "provider":
        lane["family"] = "openai-codex"
    elif change == "bypass":
        lane["command"][6] = "bypassPermissions"
    elif change == "extra":
        lane["command"].insert(4, "--allowedTools=Bash")
    else:
        raw["projects"]["gillella/other"] = raw["projects"].pop(permissions.FACTORY)
        lane["projects"] = ["gillella/other"]
    config.path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="worker_permissions"):
        Config(config.path)


def test_author_permissions_are_exact_task_commands_and_scoped_file_rules(scoped):
    result = compiled(scoped)
    config, record, *_ = scoped
    argv, commands = result["argv"], result["commands"]
    assert argv[:5] == config.lanes["model-one"]["command"][:5]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--add-dir") + 1] == str(config.kernel_root)
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert ["git", "push", "origin", "HEAD:refs/heads/fix/issue-638-test"] in commands
    assert ["git", "add", "--", "integrations/hermes"] in commands
    assert ["/opt/bin/aru-app", "--repo", permissions.FACTORY, "--", "gh", "issue", "view", "638",
            "--repo", permissions.FACTORY, "--json", "number,body,labels,state"] in commands
    assert not any("review" in c or "merge" in c or "-c" in c for c in commands)
    assert not any("*" in item for c in commands for item in c)
    assert f"Edit(/{record['worktree']}/integrations/hermes/**)" in argv
    assert f"Edit(/{record['worktree']}/.aru-worker-body.md)" in argv
    assert 'old_string=""' in argv[-1]
    assert not any(a in {"Bash", "Read", "Edit", "Write", "--dangerously-skip-permissions"} for a in argv)
    before = deepcopy(result)
    config.lanes["model-one"]["command"][3] = "changed-model"
    assert result == before  # snapshot must not alias mutable config
    assert result["policy_fingerprint"] != permissions.fingerprint(config, record["repo"], record["agent"])


@pytest.mark.parametrize("branch", ["main", "master", "fix/issue-637-test", "fix/issue-638-x;evil"])
def test_wrong_branch_fails_closed(scoped, branch):
    config, record, adapter, original = scoped
    def run(argv, cwd):
        return original(argv, cwd) if argv[1] == "rev-parse" else SimpleNamespace(stdout=branch, returncode=0)
    with pytest.raises(DriverError, match="feature branch"):
        permissions.compile_policy(config, record, adapter, run)


def test_canonical_and_non_git_worktrees_fail_closed(scoped):
    config, record, adapter, run = scoped
    with pytest.raises(DriverError, match="Git worktree"):
        permissions.compile_policy(config, record, adapter, lambda *a: SimpleNamespace(stdout="bad", returncode=0))
    record["worktree"] = str(config.kernel_root)
    with pytest.raises(DriverError, match="isolated worktree"):
        compiled(scoped)


def test_no_write_preflight_has_real_capability_commands_without_launch_or_binding(scoped, monkeypatch, capsys):
    config, record, adapter, run = scoped
    (config.state_dir / "binding.json").unlink()
    monkeypatch.setattr(driver, "KernelAdapter", lambda *a: adapter)
    monkeypatch.setattr(execution, "run_bounded", run)
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    assert driver.main(["--config", str(config.path), "preflight", "--project", record["repo"],
                        "--issue", "638", "--agent", "model-one", "--worktree", record["worktree"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["executed"] is False and not (config.state_dir / "binding.json").exists()
    assert result["argv"][result["argv"].index("--output-format") + 1] == "stream-json"
    assert "--disallowedTools" in result["argv"]
    assert ["/usr/bin/python3", "--version"] in result["commands"]
    assert not any("pytest" in c or "push" in c or "review" in c for c in result["commands"])
    assert "AGENTS.md" in result["argv"][-1] and "bare OK is insufficient" in result["argv"][-1]


@pytest.mark.parametrize("preflight", [False, True])
def test_compiled_prompt_retains_literal_commands_and_denial_stop(scoped, preflight):
    result = compiled(scoped, preflight=preflight)
    prompt = result["argv"][-1]
    for command in result["commands"]:
        literal = shlex.join(command)
        assert prompt.splitlines().count(literal) == 1
        assert f"Bash({literal})" in result["argv"]
    for instruction in ("Bash.command must equal one listed command byte-for-byte",
                        "one command per Bash invocation", "no extra flags or alternate spellings",
                        "no shell wrappers, composition, substitutions, pipelines or suffixes",
                        "Do not append echo/printf or exit-code reporting",
                        "Use native tool output and result metadata",
                        "Stop on the first permission denial", "do not retry with alternate tools"):
        assert instruction in prompt


def test_preflight_preserves_managed_read_grants_without_write_or_test_grants(scoped):
    managed, probe = compiled(scoped), compiled(scoped, preflight=True)
    assert probe["commands"] == managed["commands"][:10]
    for plan in (managed, probe):
        argv = plan["argv"]
        grants = argv[argv.index("--allowedTools") + 1:argv.index("--output-format")]
        assert [g for g in grants if g.startswith("Bash(")] == [
            f"Bash({shlex.join(c)})" for c in plan["commands"]]
    assert [g for g in probe["argv"] if g.startswith("Read(")] == [
        g for g in managed["argv"] if g.startswith("Read(")]
    assert not any(g.startswith(("Edit(", "Write(")) for g in probe["argv"])
    assert probe["argv"][-5:-1] == ["--disallowedTools", "Edit", "Write", "--verbose"]
    assert probe["argv"][:5] == managed["argv"][:5]
    assert managed["argv"][managed["argv"].index("--output-format") + 1] == "json"


@pytest.mark.parametrize("payload,code,outcome", [
    ({"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git fetch origin"}}]}, 0, "permission_denied"),
    ({"is_error": True, "errors": ["execution failed"], "subtype": "error_during_execution"}, 0, "worker_error"),
    ({}, 1, "worker_error"), ({}, 0, "reported_success"),
    ({"permission_denials": None}, 0, "result_unavailable"),
    ({"permission_denials": ["bad"]}, 0, "result_unavailable"),
    ({"is_error": "false"}, 0, "result_unavailable"),
])
def test_structured_exit_zero_does_not_prove_artifacts_or_completion(tmp_path, payload, code, outcome):
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                "permission_denials": [], "result": "claimed done", **payload}))
    result = permissions.observe_result(path, code)
    assert result["outcome"] == outcome and result["retry_blocked"] is True
    assert result.get("governed_completion") is not True
    assert result["reason"]


@pytest.mark.parametrize("content", ["", "OK", "[]", "{}", '{"type":"result"}'])
def test_missing_or_malformed_result_never_becomes_success(tmp_path, content):
    path = tmp_path / "result"
    assert permissions.observe_result(path, 0)["outcome"] == "result_unavailable"
    path.write_text(content)
    assert permissions.observe_result(path, 0)["outcome"] == "result_unavailable"


@pytest.mark.parametrize("stop_after_child", [False, True])
def test_worker_observes_denial_and_keeps_process_exit_separate(synthetic_execution, monkeypatch, stop_after_child):
    config, state, tree = synthetic_execution
    descriptor, record = execution_tests.reserve_worker(config, state, tree)
    # An immutable synthetic snapshot exercises the existing supervisor, with no Claude call.
    fp = permissions.fingerprint(config, record["repo"], record["agent"])
    record.update(policy_fingerprint=fp, permission_snapshot={"argv": ["synthetic"], "policy_fingerprint": fp})
    write_json(state.worker_path(record["id"]), record)
    def process(argv, **kwargs):
        kwargs["stdout"].write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
            "permission_denials": [{"tool_name": "Read", "tool_input": {"file_path": "/canonical/AGENTS.md"}}]}))
        kwargs["stdout"].flush()
        if stop_after_child:
            state.request_stop(record["repo"])
        return SimpleNamespace(pid=123, wait=lambda **k: 0)
    monkeypatch.setattr(execution.subprocess, "Popen", process)
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: {})
    assert execution.worker_main(config, record["id"], descriptor) == 0
    receipt = state.worker(record["id"])
    assert receipt["exit_code"] == 0 and receipt["outcome"] == "permission_denied"
    assert "/canonical/AGENTS.md" in receipt["reason"] and receipt["retry_blocked"]
    assert not state.capacity_busy("same-subscription")
    assert not list((state.root / "cooldowns").glob("*.json"))


def test_repeated_reconcile_and_heartbeat_do_not_respawn_failed_task(tmp_path):
    h = Harness(tmp_path)
    h.one_lane()
    h.kernel.issues = h.kernel.issues[:1]
    h.controller.reconcile(REPO)
    os.close(h.descriptors.pop())
    receipt = h.launched[0].copy()
    receipt.update(state="exited", exit_code=0, retry_blocked=True, reason="Claude permission denial: git",
                   policy_fingerprint=permissions.fingerprint(h.config, REPO, receipt["agent"]))
    write_json(h.state.worker_path(receipt["id"]), receipt)
    before = len(h.launched)
    for _ in range(3):
        assert h.controller.reconcile(REPO)["status"] == "degraded"
        tick = h.controller.tick(REPO)
        assert tick["status"] == "degraded" and not tick["wakeAgent"]
    assert len(h.launched) == before
    h.config.lanes[receipt["agent"]]["command"][2] = "changed authorized command"
    assert h.controller.reconcile(REPO)["status"] == "running"
    assert len(h.launched) == before + 1
    h.close()


def test_changed_head_unblocks_but_event_and_clock_do_not(scoped):
    config, record, *_ = scoped
    record.update(retry_blocked=True, reason="denied", policy_fingerprint=permissions.fingerprint(config, record["repo"], record["agent"]))
    assert permissions.retry_blocker(config, record, {})
    assert permissions.retry_blocker(config, record, {"event": "new", "time": 9999999999})
    assert permissions.retry_blocker(config, record, {"pr": 700, "head": "a" * 40}) is None
    config.projects[record["repo"]]["worker_permissions"]["recovery_epoch"] = "approved-recovery"
    assert permissions.retry_blocker(config, record, {}) is None


def test_launch_snapshots_compiled_policy_and_binds_author_app(scoped, monkeypatch):
    config, record, adapter, run = scoped
    state = execution.State(config.state_dir)
    data = state.project(record["repo"])
    data["enabled"] = True
    state.save(record["repo"], data)
    monkeypatch.setattr(execution, "KernelAdapter", lambda *a: adapter)
    monkeypatch.setattr(execution, "run_bounded", run)
    processes = []
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *a, **k: processes.append(k) or SimpleNamespace(pid=123))
    result = execution.launch(config, record["repo"], record["agent"], 638, record["worktree"])
    receipt = state.worker(result["id"])
    assert receipt["permission_snapshot"]["task"]["touches"] == ["integrations/hermes/**"]
    assert processes[0]["env"][execution.APP_RUNNER_ENV] == "/opt/bin/aru-app"
    assert not processes[0]["shell"]
    config.lanes["model-one"]["command"][3] = "changed-model"
    with pytest.raises(DriverError, match="policy changed after admission"):
        execution.worker_output(config, state, receipt, config.lanes["model-one"])
    assert not (state.root / "logs" / f"{receipt['id']}.result.json").exists()


def test_other_project_sharing_lane_keeps_original_command(scoped):
    config, record, *_ = scoped
    raw = deepcopy(config.raw)
    raw["projects"]["gillella/other"] = {"repo_dir": str(config.kernel_root), "lanes": ["model-one"]}
    raw["lanes"]["model-one"]["projects"].append("gillella/other")
    config.path.write_text(json.dumps(raw))
    current = Config(config.path)
    record["repo"] = "gillella/other"
    assert execution.prepare_policy(current, record, config.kernel_root) is None
    lane = current.lane("gillella/other", "model-one")
    argv, output = execution.worker_output(current, execution.State(current.state_dir), record, lane)
    assert argv == [p.replace("{prompt}", record["prompt"]) for p in lane["command"]]
    assert output is None


def test_new_events_reconcile_against_existing_failed_receipt(tmp_path):
    h = Harness(tmp_path)
    h.one_lane()
    h.kernel.issues = h.kernel.issues[:1]
    h.controller.reconcile(REPO)
    os.close(h.descriptors.pop())
    receipt = h.launched[0].copy()
    receipt.update(state="exited", retry_blocked=True, reason="denied",
                   policy_fingerprint=permissions.fingerprint(h.config, REPO, receipt["agent"]))
    write_json(h.state.worker_path(receipt["id"]), receipt)
    for event in ("completion", "heartbeat-recovery", "operator-message"):
        h.controller.event(REPO, event, "event", inline=True)
        result = h.controller.reconcile(REPO)
        assert result["status"] == "degraded" and not result["launched"]
        assert driver._honest_health(h.config, REPO, {"wakeAgent": False})["status"] == "degraded"
    assert len(h.launched) == 1
    h.close()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_malformed_retry_receipts_fail_closed(scoped, value):
    config, record, *_ = scoped
    state = execution.State(config.state_dir)
    record.update(retry_blocked=value, policy_fingerprint="a" * 64, reason="denied")
    write_json(state.worker_path(record["id"]), record)
    with pytest.raises(DriverError, match="retry blocker"):
        state.worker(record["id"])


@pytest.mark.parametrize("stop_at", ["before-worker", "before-restart", "during-revalidation"])
def test_policy_stop_without_child_resumes_once_after_restart(tmp_path, monkeypatch, stop_at):
    h = Harness(tmp_path)
    h.one_lane()
    h.kernel.issues = h.kernel.issues[:1]
    h.controller.reconcile(REPO)
    record = h.launched[0].copy()
    fp = permissions.fingerprint(h.config, REPO, record["agent"])
    record.update(prompt="synthetic", admission_stop=h.state.stop_nonce(REPO),
                  policy_fingerprint=fp, permission_snapshot={"argv": ["synthetic"], "policy_fingerprint": fp})
    write_json(h.state.worker_path(record["id"]), record)
    wakes = []
    monkeypatch.setattr(scheduler, "ensure_heartbeat", lambda *a, **k: {"heartbeat_job_id": "one"})
    monkeypatch.setattr(scheduler, "schedule_wake", lambda *a, **k: wakes.append(k) or {})
    monkeypatch.setattr(execution.subprocess, "Popen", lambda *a, **k: pytest.fail("child crossed Stop"))
    if stop_at == "during-revalidation":
        monkeypatch.setattr(execution, "revalidate_worker", lambda *a: h.state.request_stop(REPO))
    else:
        h.state.request_stop(REPO)
        if stop_at == "before-restart":
            driver.start(h.config, REPO)  # isolated synthetic state only
    try:
        assert execution.worker_main(h.config, record["id"], h.descriptors.pop()) == 1
        receipt = h.state.worker(record["id"])
        assert "stop" in receipt["reason"].lower()
        assert not receipt.get("retry_blocked")
        assert "child_pid" not in receipt
        assert not h.state.capacity_busy(record["capacity_key"])
        if stop_at != "before-restart":
            assert not wakes
            assert h.controller.reconcile(REPO)["status"] == "stopped"
            driver.start(h.config, REPO)
        assert h.controller.reconcile(REPO)["status"] == "running"
        for _ in range(3):
            h.controller.reconcile(REPO)
            h.controller.tick(REPO)
        assert len(h.launched) == 2
        assert h.kernel.record(1)["agents"] == [record["agent"]]
    finally:
        h.close()


@pytest.mark.parametrize("field", ["result", "errors", "subtype", "permission_denials"])
def test_large_result_details_stay_in_log_not_receipt_or_status(tmp_path, field):
    payload = {"type": "result", "subtype": "success", "is_error": False,
               "permission_denials": [], "result": "done"}
    detail = "synthetic-detail-" * 32000
    payload[field] = ([{"tool_name": "Bash", "tool_input": {"command": detail}}]
                      if field == "permission_denials" else detail)
    path = tmp_path / "result.json"
    original = json.dumps(payload)
    path.write_text(original)
    result = permissions.observe_result(path, 0)
    assert len(json.dumps(result).encode()) <= 16 * 1024
    assert len(result["reason"].encode()) <= 2048
    assert result["retry_blocked"]
    assert result["details_omitted"] is True
    assert path.read_text() == original  # full denial evidence remains outside receipts


def test_oversized_finished_log_is_bounded_and_still_blocks(tmp_path):
    path = tmp_path / "result.json"
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    result = permissions.observe_result(path, 0)
    assert result["outcome"] == "result_unavailable" and result["retry_blocked"]
    assert "too large" in result["reason"]
    assert path.stat().st_size == 8 * 1024 * 1024
