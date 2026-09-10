"""Refusal regressions using temporary state and mocked process creation only."""
from types import SimpleNamespace

import pytest

from test_persona_routing import driver_env, prepare_managed_dispatch

__all__ = ["driver_env"]
from aru_project_driver import execution, persona_routing
from aru_project_driver.config import DriverError


def test_unclassified_task_is_refused(driver_env):
    config, state, worktree = driver_env
    with pytest.raises(Exception):
        persona_routing.resolve_task_plan(
            config, state, "owner/repo", {"number": 101, "touches": ["src/app.py"]},
            "codex-astra", str(worktree), "feat/issue-101", "a" * 40,
        )


def test_missing_scope_is_refused(driver_env):
    config, state, worktree = driver_env
    with pytest.raises(Exception):
        persona_routing.resolve_task_plan(
            config, state, "owner/repo",
            {"number": 101, "labels": ["aru-task:bounded_implementation"]},
            "codex-astra", str(worktree), "feat/issue-101", "a" * 40,
        )


def test_corrupt_evidence_is_refused(driver_env):
    config, state, _ = driver_env
    (state.root / "probe_records.json").write_text("not json")
    with pytest.raises(Exception):
        persona_routing.load_evidence_store(config, state)


def test_self_review_is_not_repaired_by_renaming_actor(driver_env):
    config, state, worktree = driver_env
    binding = {
        "pr": 201, "issue": 101, "author": "claude-opus", "author_actor": "same-actor",
        "reviewer_actor": "same-actor", "risk_tier": 2,
        "author_history": [{"persona": "opus-implementer",
                            "account_id": "claude-subscription-1", "actor": "same-actor"}],
    }
    binding.update(repo="owner/repo", head="a" * 40, touches=["src/app.py"],
                   reviewer_persona="astra-implementer", reviewer_account="openai-codex",
                   authority=persona_routing._personas.default_snapshot().persona("astra-implementer").lineage,
                   authority_source="synthetic kernel binding", external_first_released=True,
                   external_first_reason="synthetic refusal")
    with pytest.raises(persona_routing._personas.errors.ReviewIndependenceError):
        persona_routing.resolve_review_plan(config, state, "owner/repo", binding,
                                            str(worktree), "a" * 40, current_binding=dict(binding))


def test_resolution_failure_never_starts_generic_supervisor(driver_env, monkeypatch):
    config, state, worktree = driver_env
    prepare_managed_dispatch(config, monkeypatch)
    calls = []

    def refuse(*args, **kwargs):
        raise DriverError("persona policy unavailable")

    def fake_spawn(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(pid=999999)

    monkeypatch.setattr(persona_routing, "resolve_task_plan", refuse)
    monkeypatch.setattr(execution.subprocess, "Popen", fake_spawn)
    with pytest.raises(DriverError, match="persona"):
        execution.launch(config, "owner/repo", "codex-astra", 101, str(worktree))
    assert not calls, "a generic supervisor was started after persona refusal"


def test_probe_must_create_new_matching_evidence(driver_env):
    config, state, _ = driver_env
    # Remove all fixture evidence: otherwise the pre-seeded record hides a broken probe.
    (state.root / "probe_records.json").unlink()
    assert execution.probe(config, "owner/repo", "codex-astra", state)
    assert (state.root / "probe_records.json").exists(), "probe success created no receipt"


def test_missing_package_never_reserves_capacity(driver_env, monkeypatch):
    config, state, worktree = driver_env
    prepare_managed_dispatch(config, monkeypatch)
    monkeypatch.setattr(persona_routing, "_personas", None)
    with pytest.raises(DriverError, match="personas package"):
        execution.launch(config, "owner/repo", "codex-astra", 101, str(worktree))
    assert not state.capacity_busy("openai-codex")
    assert state.workers("owner/repo") == []


def test_digest_drift_with_valid_sha_shape_is_refused(driver_env):
    config, _, _ = driver_env
    config.raw["projects"]["owner/repo"]["personas_source_digest"] = "f" * 64
    with pytest.raises(DriverError, match="source integrity mismatch"):
        persona_routing.get_policy_snapshot(config, "owner/repo")


def test_missing_required_integrity_pins_refuses(driver_env):
    config, _, _ = driver_env
    config.raw["projects"]["owner/repo"]["personas_required"] = True
    with pytest.raises(DriverError, match="digest pins"):
        persona_routing.get_policy_snapshot(config, "owner/repo")


def test_invalid_author_is_not_dropped(driver_env):
    config, _, _ = driver_env
    policy = persona_routing.get_policy_snapshot(config, "owner/repo")
    with pytest.raises(DriverError, match="incomplete author history"):
        persona_routing.extract_author_identities([{"agent": "old-worker"}], policy)


def test_conflicting_task_labels_are_refused(driver_env):
    config, state, worktree = driver_env
    with pytest.raises(persona_routing._personas.errors.ContradictoryTaskError):
        persona_routing.resolve_task_plan(config, state, "owner/repo", {
            "number": 101, "touches": ["src/app.py"],
            "labels": ["aru-task:bounded_implementation", "aru-task:architecture_decision"],
        }, "codex-astra", str(worktree), "feat/issue-101", "a" * 40)


def test_changed_review_authority_is_refused(driver_env):
    config, state, worktree = driver_env
    with pytest.raises(DriverError, match="matching kernel authority"):
        persona_routing.resolve_review_plan(config, state, "owner/repo", {"head": "a" * 40},
                                            str(worktree), "a" * 40,
                                            current_binding={"head": "b" * 40})


def test_explicit_empty_policy_does_not_select_defaults(driver_env):
    config, _, _ = driver_env
    config.raw["projects"]["owner/repo"]["personas_policy"] = {}
    with pytest.raises(persona_routing._personas.errors.PersonaPolicyError):
        persona_routing.get_policy_snapshot(config, "owner/repo")


def test_evidence_missing_records_is_not_empty_success(driver_env):
    config, state, _ = driver_env
    (state.root / "probe_records.json").write_text('{"wrong_key": []}')
    with pytest.raises(DriverError, match="evidence is unreadable"):
        persona_routing.load_evidence_store(config, state)
