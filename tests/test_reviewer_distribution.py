from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import common
import create_pr


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


def test_initial_assignment_rotates_all_eligible_authorities(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,openai-codex:mo",
    )
    monkeypatch.setattr(common, "_reviewer_command", lambda name: f"/bin/{name}")
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
            probe_runner=lambda argv: result(argv),
        )
        for number in range(4)
    ]

    assert assignments == [
        ("coderabbit", None, None),
        ("sourcery", None, None),
        ("claude-code", "m1", "claude-reviewer"),
        ("openai-codex", "mo", "codex-reviewer"),
    ]


def test_initial_coding_assignment_probes_only_selected_candidate():
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
    assert [call[1] for call in calls] == ["2"]
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
    monkeypatch.setattr(common, "_reviewer_command", lambda name: f"/bin/{name}")
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


def test_unavailable_rotated_candidate_advances_without_state(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1")
    calls = []

    def unavailable(argv):
        calls.append(argv)
        return result(argv, ok=False)

    reviewer = create_pr.choose_initial_reviewer(
        1,
        "codex-author",
        "openai-codex",
        "author-login",
        external_states=external_states(coderabbit=create_pr.AVAILABLE),
        reviewer_actors={"m1": "claude-reviewer"},
        probe_runner=unavailable,
    )
    assert reviewer == ("coderabbit", None, None)
    assert [call[1] for call in calls] == ["1"]


def test_initial_assignment_is_deterministic(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1")
    arguments = {
        "author_identity": "codex-author",
        "author_family": "openai-codex",
        "author_actor": "author-login",
        "external_states": external_states(coderabbit=create_pr.AVAILABLE),
        "reviewer_actors": {"m1": "claude-reviewer"},
        "probe_runner": lambda argv: result(argv),
    }
    assert create_pr.choose_initial_reviewer(11, **arguments) == (
        create_pr.choose_initial_reviewer(11, **arguments)
    )


def test_registered_coders_cannot_silently_degrade_to_external_only(monkeypatch):
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    with pytest.raises(create_pr.KernelError, match="missing while reviewer bindings exist"):
        create_pr.choose_initial_reviewer(
            11,
            "codex-author",
            "openai-codex",
            "author-login",
            external_states=external_states(coderabbit=create_pr.AVAILABLE),
            reviewer_actors={"m1": "claude-reviewer"},
        )


def test_rate_limited_success_status_is_unavailable():
    observed_at = datetime.now(timezone.utc)
    pr = {
        "createdAt": observed_at.isoformat(),
        "statusCheckRollup": [
            {
                "name": "CodeRabbit",
                "conclusion": "SUCCESS",
                "status": "COMPLETED",
                "completedAt": observed_at.isoformat(),
            }
        ],
    }
    status = {
        "context": "CodeRabbit",
        "state": "success",
        "description": "Review rate limited",
        "created_at": (observed_at + timedelta(seconds=1)).isoformat(),
    }
    assert (
        create_pr.external_state(
            pr,
            "coderabbit",
            reviews=[],
            comments=[],
            statuses=[status],
        )
        == create_pr.UNAVAILABLE
    )


@pytest.mark.parametrize(
    "message",
    [
        "The provider encountered an error",
        "Review failed because the service is unavailable",
    ],
)
def test_provider_failure_terms_are_unavailable_anywhere(message):
    assert common.review_evidence_unavailable({"body": message})
