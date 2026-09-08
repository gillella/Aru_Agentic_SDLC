from __future__ import annotations

import subprocess
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import common
import create_pr
import review_policy
import reviewer_probe


@pytest.fixture(autouse=True)
def configured_reviewers(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,claude-code:m2@2,claude-code:m3@3,"
        "openai-codex:mo,xai-cursor:mx,google-antigravity:mg",
    )


def result(argv, *, ok=True, output="OK"):
    return subprocess.CompletedProcess(argv, 0 if ok else 1, output if ok else "", "")


def external_states(**overrides):
    states = {service: create_pr.UNAVAILABLE for service in create_pr.EXTERNAL_REVIEWERS}
    states.update(overrides)
    return states


@pytest.mark.parametrize("changed", ["reviewer", "reviewer-actor", "author-actor"])
@pytest.mark.parametrize("boundary", ["selection", "before-write"])
def test_coding_recovery_preserves_same_family_reassignment(monkeypatch, changed, boundary):
    original = {"number": 42, "headRefOid": "a" * 40, "author": {"login": "author-login"},
                "labels": ["review:claude-code", "reviewer:m1", "reviewer-actor:old-bot",
                           "author:writer", "author-family:openai-codex"]}
    live, commands, reads = deepcopy(original), [], []
    def drift():
        if changed == "author-actor":
            live["author"]["login"] = "new-author"
        else:
            live["labels"] = [name for name in live["labels"] if not name.startswith(changed + ":")]
            live["labels"].append(changed + ":new-reviewer")
    def choose(*args, **kwargs):
        if boundary == "selection":
            drift()
        return "claude-code", "m3", "third-bot"
    def read(args):
        reads.append(args)
        if boundary == "before-write" and len(reads) == 2:
            drift()
        return deepcopy(live)
    monkeypatch.setattr(create_pr, "select_reviewer_from_pool", choose)
    monkeypatch.setattr(create_pr, "_attempted_reviewer_keys", lambda *args: set())
    monkeypatch.setattr(create_pr, "gh_json", read)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *args, **kwargs: None)
    monkeypatch.setattr(create_pr, "run", commands.append)
    with pytest.raises(create_pr.KernelError, match="changed during fallback"):
        create_pr.recover_coding_authority(
            42, original, "claude-code", "old review worker lost its reservation",
            datetime.now(timezone.utc), lambda argv: result(argv),
            policy=review_policy.default_review_policy(("coderabbit",)), external_states=external_states(),
        )
    assert not any(command[1:3] == ["pr", "edit"] for command in commands)
    assert len(reads) == (1 if boundary == "selection" else 2)


def test_preferred_external_is_deterministic_without_retired_or_coding_probes(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,openai-codex:mo",
    )
    monkeypatch.setattr(reviewer_probe, "_command", lambda name: f"/bin/{name}")
    states = external_states(
        coderabbit=create_pr.AVAILABLE,
        sourcery=create_pr.AVAILABLE,
    )
    actors = {"m1": "claude-reviewer", "mo": "codex-reviewer"}

    assignments = [
        create_pr.choose_initial_reviewer(
            number,
            "codex-author",
            "openai-codex",
            "author-login",
            external_states=states,
            reviewer_actors=actors,
            probe_runner=lambda argv: pytest.fail("available preferred external must not probe coding"),
        )
        for number in (0, 1, 2, 3, 11, 11)
    ]

    assert assignments == [("coderabbit", None, None)] * 6


def test_initial_coding_assignment_uses_aggregate_capacity_probe():
    calls = []

    def probe(argv):
        calls.append(argv)
        return result(argv)

    reviewer = create_pr.choose_initial_reviewer(
        7,
        "codex-author",
        "openai-codex",
        "author-login",
        external_states=external_states(),
        reviewer_actors={
            "m1": "claude-reviewer-1",
            "m2": "claude-reviewer-2",
            "m3": "claude-reviewer-3",
        },
        probe_runner=probe,
    )
    assert reviewer == ("claude-code", "m2", "claude-reviewer-2")
    assert sorted(call[1] for call in calls) == ["1", "2", "3"]
    assert all(call[-1] == "Reply exactly OK" for call in calls)


def test_initial_assignment_excludes_author_identity_and_actor(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,claude-code:m2@2",
    )
    reviewer = create_pr.choose_initial_reviewer(
        0,
        "m1",
        "claude-code",
        "author-login",
        external_states=external_states(),
        reviewer_actors={"m1": "author-login", "m2": "other-reviewer"},
        probe_runner=lambda argv: result(argv),
    )
    assert reviewer == ("claude-code", "m2", "other-reviewer")


def test_initial_assignment_prefers_a_different_author_family(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "openai-codex:mo,xai-cursor:mx",
    )
    monkeypatch.setattr(reviewer_probe, "_command", lambda name: f"/bin/{name}")
    reviewer = create_pr.choose_initial_reviewer(
        0,
        "codex-author",
        "openai-codex",
        "author-login",
        external_states=external_states(),
        reviewer_actors={"mo": "codex-reviewer", "mx": "cursor-reviewer"},
        probe_runner=lambda argv: result(argv),
    )
    assert reviewer == ("xai-cursor", "mx", "cursor-reviewer")


def test_available_external_does_not_require_local_coding_configuration(monkeypatch):
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    assert create_pr.choose_initial_reviewer(
        11,
        "codex-author",
        "openai-codex",
        "author-login",
        external_states=external_states(coderabbit=create_pr.AVAILABLE),
        reviewer_actors={"m1": "claude-reviewer"},
    ) == ("coderabbit", None, None)


def test_rate_limited_success_status_is_unavailable():
    observed_at = datetime.now(timezone.utc)
    check = {
        "name": "CodeRabbit",
        "conclusion": "SUCCESS",
        "status": "COMPLETED",
        "completed_at": (observed_at + timedelta(seconds=1)).isoformat(),
        "head_sha": "a" * 40,
        "app": {"slug": "coderabbitai"},
        "output": {"summary": "Review rate limited"},
    }
    assert (
        create_pr.external_state(
            "coderabbit",
            reviews=[],
            comments=[],
            checks=[check],
            head="a" * 40,
            since=observed_at,
        )
        == create_pr.UNAVAILABLE
    )


def test_forged_or_pre_assignment_check_cannot_control_reviewer_state():
    assigned = datetime.now(timezone.utc)
    forged = {
        "name": "CodeRabbit",
        "conclusion": "ACTION_REQUIRED",
        "completed_at": (assigned + timedelta(seconds=1)).isoformat(),
        "head_sha": "a" * 40,
        "app": {"slug": "github-actions"},
        "output": {"summary": "Quota exhausted"},
    }
    stale = {
        **forged,
        "app": {"slug": "coderabbitai"},
        "completed_at": (assigned - timedelta(seconds=1)).isoformat(),
    }
    assert create_pr.external_state(
        "coderabbit",
        reviews=[],
        comments=[],
        checks=[forged, stale],
        head="a" * 40,
        since=assigned,
    ) == create_pr.PENDING


@pytest.mark.parametrize(
    "message",
    [
        "The provider encountered an error",
        "Review failed because the service is unavailable",
    ],
)
def test_provider_failure_terms_are_unavailable_anywhere(message):
    assert common.review_evidence_unavailable({"body": message})


def test_ordinary_error_discussion_is_not_provider_unavailability():
    assert not common.review_evidence_unavailable(
        {"body": "Error handling is correct and unavailable data is rejected."}
    )


def test_skipped_provider_is_immediately_fallback_eligible(monkeypatch):
    created = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    pr = {"createdAt": created.isoformat(), "headRefOid": "a" * 40, "statusCheckRollup": []}
    comment = {
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "body": "Skipping PR review because a bot author is detected.",
        "created_at": created.isoformat(),
    }
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    evidence = iter([[], [comment], []])
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: next(evidence))
    monkeypatch.setattr(
        review_policy,
        "gh_json",
        lambda _argv: [{"total_count": 0, "check_runs": []}],
    )
    assert create_pr._external_decision(
        42, pr, "coderabbit", created + timedelta(seconds=1)
    ) == ("external-unavailable", None)


def test_coding_probes_run_concurrently(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1,claude-code:m2@2")
    both_started = threading.Event()
    lock = threading.Lock()
    started = 0

    def probe(argv):
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        assert both_started.wait(0.2)
        return result(argv)

    reviewer = reviewer_probe.probe_coding_reviewer(
        author_identity="author",
        author_family="human-or-other",
        author_actor="author-login",
        rotation_key=0,
        reviewer_actors={"m1": "reviewer-1", "m2": "reviewer-2"},
        runner=probe,
        aggregate_timeout=0.3,
    )
    assert reviewer == ("claude-code", "m1", "reviewer-1")


def test_coding_probe_has_short_aggregate_deadline(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1,claude-code:m2@2")
    release = threading.Event()

    def blocked(argv):
        release.wait(1)
        return result(argv)

    started = time.monotonic()
    reviewer = reviewer_probe.probe_coding_reviewer(
        author_identity="author",
        author_family="human-or-other",
        author_actor="author-login",
        rotation_key=0,
        reviewer_actors={"m1": "reviewer-1", "m2": "reviewer-2"},
        runner=blocked,
        aggregate_timeout=0.02,
    )
    elapsed = time.monotonic() - started
    release.set()
    assert reviewer is None
    assert elapsed < 0.2


@pytest.mark.parametrize(
    ("paths", "tier"),
    [
        (["README.md", "docs/guide.rst"], 0),
        (["src/app.py", "tests/test_app.py"], 1),
        (["AGENTS.md"], 2),
        ([".github/workflows/ci.yml"], 2),
        ([".github/PULL_REQUEST_TEMPLATE.md"], 2),
        ([".github/ISSUE_TEMPLATE/governed-task.yml"], 2),
        ([".github/PULL_REQUEST_TEMPLATE/release.md"], 2),
        ([".aru/verify.sh"], 2),
        (["hooks/enforce_touches.py"], 2),
        (["scripts/create_pr.py"], 2),
        (["scripts/merge_pr.py"], 2),
        (["scripts/merge_state.py"], 2),
        (["scripts/review_evidence.py"], 2),
        (["scripts/review_policy.py"], 2),
        (["scripts/reviewer_probe.py"], 2),
        (["integrations/hermes/aru_project_driver/controller.py"], 2),
        (["integrations/hermes/aru_project_driver/new_control.py"], 2),
        (["integrations/hermes/aru_project_driver/notes.md"], 2),
        (["integrations/hermes/install.py"], 2),
        (["integrations/hermes/skill/SKILL.md"], 2),
        (["integrations/hermes/skill/references/runtime-contract.md"], 2),
        (["integrations/hermes/aru_project_driver/deploy.py"], 3),
        (["integrations/hermes/README.md"], 0),
        (["integrations/hermes/tests/test_controller.py"], 1),
        (["integrations/hermes/aru_project_driver_examples/example.py"], 1),
        (["integrations/hermes/skill_examples/README.md"], 0),
        (["docs/KERNEL-CONTRACT.md"], 2),
        (["docs/OPERATIONS.md"], 2),
        (["security/README.md"], 2),
        ([".github/workflows/notes.md"], 2),
        (["requirements-dev.txt"], 2),
        (["CLAUDE.md"], 2),
        ([".github/copilot-instructions.md"], 2),
        ([".cursor/rules/python.md"], 2),
        ([".codex/settings.md"], 2),
        (["templates/verify.sh"], 2),
        (["src/auth/session.py"], 2),
        (["src/auth.py"], 2),
        (["src/security.py"], 2),
        (["src/payments.py"], 2),
        (["src/config.py"], 2),
        (["src/migration.sql"], 2),
        (["SECURITY.md"], 2),
        (["src/app-configuration.yaml"], 2),
        (["package-lock.json"], 2),
        ([".github/workflows/deploy-prod.yml"], 3),
        (["infra/production/terraform.tf"], 3),
        (["scripts/revert_merge.py"], 3),
        (["README.md", "src/auth/session.py"], 2),
        (["unknown.binary"], 2),
        (["../outside.py"], 3),
        ([], 3),
    ],
)
def test_review_risk_tier_fails_up(paths, tier):
    assert common.review_risk_tier(paths) == tier
