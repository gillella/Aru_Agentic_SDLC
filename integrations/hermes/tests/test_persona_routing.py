from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import execution, persona_routing
from aru_project_driver.config import Config, DriverError
from aru_project_driver.state import State, read_json, write_json

try:
    from integrations import personas
except ImportError:
    try:
        import personas
    except ImportError:
        personas = None


@pytest.fixture
def driver_env(tmp_path):
    repo = tmp_path / "repository"
    worktree = repo / ".worktrees" / "feat-issue-100"
    worktree.mkdir(parents=True)
    home = tmp_path / "hermes-home"
    runtime = tmp_path / "hermes-runtime"
    runtime.mkdir()
    kernel = tmp_path / "kernel"
    kernel.mkdir()

    lanes = {
        "claude-opus": {
            "family": "claude-code",
            "capacity_key": "claude-subscription-1",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "print('claude-opus')", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        },
        "claude-sonnet": {
            "family": "claude-code",
            "capacity_key": "claude-subscription-1",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "print('claude-sonnet')", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        },
        "codex-astra": {
            "family": "openai-codex",
            "capacity_key": "openai-codex",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "print('codex-astra')", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        },
        "cursor-grok": {
            "family": "xai-cursor",
            "capacity_key": "cursor-default",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "print('cursor-grok')", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        },
        "antigravity-gemini": {
            "family": "google-antigravity",
            "capacity_key": "antigravity-default",
            "projects": ["owner/repo"],
            "command": [sys.executable, "-c", "print('antigravity-gemini')", "{prompt}"],
            "capacity_command": [sys.executable, "-c", 'print(\'{"available": true}\')'],
            "probe_command": [sys.executable, "-c", "print('OK')"],
        },
    }

    raw = {
        "version": 1,
        "state_dir": str(home / "state" / "aru_project_driver"),
        "hermes_home": str(home),
        "hermes_repo": str(runtime),
        "kernel_root": str(kernel),
        "projects": {"owner/repo": {"repo_dir": str(repo), "lanes": list(lanes)}},
        "lanes": lanes,
    }
    path = tmp_path / "driver-config.json"
    path.write_text(json.dumps(raw))
    config = Config(path)
    state = State(config.state_dir)
    project = state.project("owner/repo")
    project["enabled"] = True
    state.save("owner/repo", project)

    # Seed valid probe observations for testing matching fleet accounts
    policy = persona_routing.get_policy_snapshot(config, "owner/repo")
    fleet_b = persona_routing.build_fleet_binding(config, state, "owner/repo", worktree=worktree)
    probe_records = []
    for p in policy.personas.values():
        for effort, model in p.model_ids.items():
            for a in fleet_b.accounts:
                if a.policy.route == p.route:
                    probe_records.append({
                        "account_id": a.account_id,
                        "route": p.route,
                        "model_id": model,
                        "effort": effort,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "outcome": "ok",
                        "source": "SYNTHETIC test probe record",
                        "authenticated": True,
                        "identity_digest": a.identity_digest,
                        "modalities": ["text", "image"],
                    })
    write_json(state.root / "probe_records.json", {"records": probe_records})

    return config, state, worktree


def test_persona_routing_is_available():
    assert persona_routing.is_available()


def test_policy_snapshot_loads_and_hashes(driver_env):
    config, state, _ = driver_env
    snapshot = persona_routing.get_policy_snapshot(config, "owner/repo")
    assert snapshot is not None
    digest = persona_routing.policy_source_digest()
    assert isinstance(digest, str) and len(digest) == 64


def test_fleet_binding_construction(driver_env):
    config, state, worktree = driver_env
    binding = persona_routing.build_fleet_binding(config, state, "owner/repo", worktree=worktree)
    assert binding is not None
    assert binding.harnesses["claude-code"].workspace == str(worktree.resolve())
    assert "claude-code" in binding.harnesses
    assert "codex" in binding.harnesses
    assert "cursor" in binding.harnesses
    assert "antigravity" in binding.harnesses


def test_probe_records_observation(driver_env):
    config, state, _ = driver_env
    ok = execution.probe(config, "owner/repo", "codex-astra", state)
    assert ok
    raw = json.loads((state.root / "probe_records.json").read_text())
    records = raw if isinstance(raw, list) else raw.get("records", [])
    assert len(records) >= 1
    rec = next(r for r in records if r.get("account_id") == "openai-codex" and r.get("route") == "codex")
    assert rec["route"] == "codex"
    assert rec["model_id"] == "gpt-6-astra"
    assert rec["outcome"] == "ok"
    assert rec["account_id"] == "openai-codex"


def test_task_plan_resolution_for_different_classes(driver_env):
    config, state, worktree = driver_env
    
    # 1. Architecture decision -> Fable or fallback
    task_arch = {"number": 101, "labels": ["aru-task:architecture_decision"], "touches": ["docs/arch.md"]}
    plan_arch = persona_routing.resolve_task_plan(
        config, state, "owner/repo", task_arch, "codex-astra", str(worktree), "feat/issue-101", "0" * 40
    )
    assert plan_arch is not None
    assert plan_arch.persona in ("fable-architect", "astra-implementer", "opus-implementer")
    assert plan_arch.effective_role in ("chief_architect", "architect")

    # 2. Bounded implementation -> Astra or Opus
    task_impl = {"number": 102, "labels": ["aru-task:bounded_implementation"], "touches": ["src/app.py"]}
    plan_impl = persona_routing.resolve_task_plan(
        config, state, "owner/repo", task_impl, "codex-astra", str(worktree), "feat/issue-102", "0" * 40
    )
    assert plan_impl is not None
    assert plan_impl.persona in ("astra-implementer", "sol-implementer", "terra-implementer", "opus-implementer")
    assert plan_impl.effective_role in ("senior_implementer", "implementer")

    # 3. Triage / docs -> Haiku
    task_docs = {"number": 103, "labels": ["aru-task:triage_documentation"], "touches": ["docs/README.md"]}
    plan_docs = persona_routing.resolve_task_plan(
        config, state, "owner/repo", task_docs, "claude-sonnet", str(worktree), "feat/issue-103", "0" * 40
    )
    assert plan_docs is not None
    assert plan_docs.effective_role in ("triage_assistant", "triage", "implementer")


def test_architecture_fallback_chain(driver_env):
    config, state, worktree = driver_env
    
    # When fable is excluded / not bound, fallback to astra then opus
    task_arch = {"number": 104, "labels": ["aru-task:architecture_decision"], "touches": ["docs/ADR.md"]}
    plan = persona_routing.resolve_task_plan(
        config, state, "owner/repo", task_arch, "codex-astra", str(worktree), "feat/issue-104", "0" * 40,
        allow_optional=False,
    )
    assert plan is not None
    assert plan.persona in ("astra-implementer", "opus-implementer", "fable-architect")
    if plan.fallback_reason:
        assert len(plan.skipped) > 0


def test_reviewer_lineage_independence(driver_env):
    config, state, worktree = driver_env
    
    # Claude-authored review request -> reviewer cannot be Claude lineage
    review_claude = {
        "pr": 201,
        "author": "claude-opus",
        "author_history": [{"persona": "opus-implementer", "account_id": "claude-subscription-1", "actor": "claude-opus"}],
        "reviewer_actor": "reviewer",
        "risk_tier": 2,
    }
    plan_claude = persona_routing.resolve_review_plan(
        config, state, "owner/repo", review_claude, str(worktree), "a" * 40
    )
    assert plan_claude is not None
    # Must NOT be Claude lineage (no Sonnet, Opus, Haiku)
    assert plan_claude.persona not in ("sonnet-reviewer", "sonnet-security-reviewer", "opus-implementer", "haiku-triage")
    assert plan_claude.persona in ("astra-implementer", "sol-implementer", "terra-implementer", "grok-reviewer", "gemini-reviewer")

    # Codex-authored review request -> reviewer cannot be Codex lineage
    review_codex = {
        "pr": 202,
        "author": "codex-astra",
        "author_history": [{"persona": "astra-implementer", "account_id": "openai-codex", "actor": "codex-astra"}],
        "reviewer_actor": "reviewer",
        "risk_tier": 2,
    }
    plan_codex = persona_routing.resolve_review_plan(
        config, state, "owner/repo", review_codex, str(worktree), "b" * 40
    )
    assert plan_codex is not None
    # Must NOT be Codex lineage (no Astra, Sol, Terra, Luna, Spark)
    assert plan_codex.persona not in ("astra-implementer", "sol-implementer", "terra-implementer", "luna-implementer", "spark-implementer")
    assert plan_codex.persona in ("sonnet-reviewer", "sonnet-security-reviewer", "opus-implementer", "grok-reviewer", "gemini-reviewer")


def test_execution_launch_injects_persona_metadata_and_argv(driver_env):
    config, state, worktree = driver_env
    receipt = execution.launch(config, "owner/repo", "codex-astra", 105, str(worktree))
    
    assert receipt["id"]
    record = state.worker(receipt["id"])
    assert record["state"] in ("launching", "running")
    assert "persona" in record
    assert "plan_digest" in record
    assert "policy_digest" in record
    assert "plan_argv" in record
    assert record["persona"] in ("opus-implementer", "astra-implementer", "sol-implementer")
    assert record["model_id"] in ("claude-opus-5", "gpt-6-astra", "gpt-5.6-sol")


def test_worker_output_materializes_plan_argv(driver_env):
    config, state, worktree = driver_env
    receipt = execution.launch(config, "owner/repo", "codex-astra", 106, str(worktree))
    lane = config.lane("owner/repo", "codex-astra")
    record = state.worker(receipt["id"])
    argv, output = execution.worker_output(config, state, record, lane)
    
    assert output is not None
    output.close()
    assert argv == record["plan_argv"]
    assert "result_path" in record
    assert Path(record["result_path"]).exists()


def test_all_13_approved_personas_present(driver_env):
    config, _, _ = driver_env
    snapshot = persona_routing.get_policy_snapshot(config, "owner/repo")
    expected_13 = {
        "fable-architect", "opus-implementer", "sonnet-reviewer", "haiku-triage",
        "astra-implementer", "sol-implementer", "terra-maintainer", "luna-scout", "spark-pair",
        "grok-frontend", "composer-fixer",
        "flash-qa", "pro-design",
    }
    present = set(snapshot.personas.keys())
    assert expected_13 == present
    assert len(expected_13) == 13


def test_tamper_detection_rejects_corrupted_policy(driver_env):
    config, state, _ = driver_env
    # Corrupt document with forbidden key
    bad_doc = persona_routing._personas.default_document()
    bad_doc["forbidden_executable"] = "/bin/sh"
    with pytest.raises(Exception):
        persona_routing._personas.from_document(bad_doc)


def test_installer_packages_personas_payload(tmp_path):
    import importlib.util
    _spec = importlib.util.spec_from_file_location("driver_install", Path(__file__).resolve().parents[1] / "install.py")
    installer = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(installer)

    source_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "hermes-home"
    kernel = tmp_path / "kernel"
    kernel.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"kernel_root": str(kernel), "hermes_home": str(home)}))

    result = installer.install(source_root, config_path, apply=True)
    assert result["applied"]
    
    # Check that personas files were installed to scripts/personas
    installed_personas = home / "scripts" / "personas"
    assert installed_personas.is_dir()
    assert (installed_personas / "__init__.py").is_file()
    assert (installed_personas / "policy.py").is_file()
    assert (installed_personas / "registry.py").is_file()
    assert (installed_personas / "data" / "route-catalogs.json").is_file()
    assert not (installed_personas / "tests").exists()
