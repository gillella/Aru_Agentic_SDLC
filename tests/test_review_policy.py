from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import create_pr
import review_policy


def labels(*values: str) -> tuple[str, ...]:
    return tuple(values)


def result(argv, *, ok=True):
    return subprocess.CompletedProcess(argv, 0 if ok else 1, "OK" if ok else "", "")


def configured_policy_labels() -> tuple[str, ...]:
    return labels(
        "reviewer-registered:coderabbit",
        "reviewer-registered:sourcery",
        "reviewer-registered:codeant",
        "review-policy:timeout=600",
    )


def test_registration_cannot_reenable_retired_reviewers():
    policy = review_policy.review_policy_from_labels(
        labels("reviewer-registered:coderabbit", "reviewer-registered:sourcery")
    )
    assert policy.external_reviewers == ("coderabbit",)
    assert policy.coding_fallbacks == review_policy.CODING_REVIEWERS
    assert policy.timeout_seconds == 900
    assert policy.sources == {
        "external_reviewers": "registration-labels",
        "coding_fallbacks": "ARU_CODING_REVIEWERS",
        "timeout_seconds": "kernel-default",
    }


def test_repository_labels_configure_coderabbit_and_completion_timeout():
    policy = review_policy.review_policy_from_labels(configured_policy_labels())
    assert policy.as_dict() == {
        "selection": "coderabbit-first",
        "external_reviewers": ["coderabbit"],
        "coding_fallbacks": [
            "claude-code",
            "openai-codex",
            "xai-cursor",
            "google-antigravity",
        ],
        "timeout_seconds": 600,
        "sources": {
            "external_reviewers": "registration-labels",
            "coding_fallbacks": "ARU_CODING_REVIEWERS",
            "timeout_seconds": "repository-label",
        },
    }


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("review-policy:unknown=value",), "unsupported declarations"),
        (("review-policy:primary=coderabbit",), "unsupported declarations"),
        (("review-policy:fallback-1=claude-code",), "unsupported declarations"),
        (
            ("review-policy:timeout=600", "review-policy:timeout=900"),
            "timeout is ambiguous",
        ),
        (("review-policy:timeout=fast",), "integer number"),
        (("review-policy:timeout=30",), "60-86400"),
    ],
)
def test_malformed_or_contradictory_policy_fails_closed(extra, message):
    with pytest.raises(create_pr.KernelError, match=message):
        review_policy.review_policy_from_labels(
            labels("reviewer-registered:coderabbit", *extra)
        )


@pytest.mark.parametrize("number", [540, 542, 543])
def test_usable_coderabbit_remains_preferred_without_unused_coding_config(monkeypatch, number):
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    policy = review_policy.review_policy_from_labels(configured_policy_labels())
    selected = create_pr.choose_initial_reviewer(
        number, "codex-author", "openai-codex", "author-login", policy=policy,
        external_states={service: create_pr.AVAILABLE for service in create_pr.EXTERNAL_REVIEWERS},
        reviewer_actors={},
    )
    assert selected == ("coderabbit", None, None)


def test_coding_fallback_excludes_prior_candidates(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1,claude-code:m2@2")
    policy = review_policy.default_review_policy(("coderabbit", "sourcery", "codeant"))
    observed = {}

    def coding_probe(**kwargs):
        observed.update(kwargs)
        return "claude-code", "m2", "claude-reviewer"

    selected = review_policy.select_reviewer_from_pool(
        policy,
        number=542,
        author_identity="codex-author",
        author_family="openai-codex",
        author_actor="author-login",
        external_states={service: create_pr.AVAILABLE for service in create_pr.EXTERNAL_REVIEWERS},
        reviewer_actors={"m1": "claude-reviewer", "m2": "claude-reviewer"},
        probe_runner=result,
        excluded={
            "external:coderabbit",
            "external:sourcery",
            "external:codeant",
            "coding:m1",
        },
        coding_probe=coding_probe,
    )

    assert selected == ("claude-code", "m2", "claude-reviewer")
    assert set(observed["excluded_identities"]) == {"m1"}


def test_attempted_reviewer_history_is_exact_head_and_identity_aware(monkeypatch):
    head = "a" * 40
    marker = {
        "head": head,
        "previous_authority": "coderabbit",
        "previous_reviewer": None,
        "new_authority": "sourcery",
        "reviewer": None,
    }
    stale = {**marker, "head": "b" * 40, "new_authority": "codeant"}
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        review_policy,
        "gh_paginated",
        lambda _endpoint: [
            {
                "body": "## Aru authoritative reviewer fallback\n\n"
                f"<!-- aru-review-assignment:v1 {review_policy.json.dumps(marker)} -->"
            },
            {
                "body": "## Aru authoritative reviewer fallback\n\n"
                f"<!-- aru-review-assignment:v1 {review_policy.json.dumps(stale)} -->"
            },
        ],
    )

    assert review_policy.attempted_reviewer_keys(
        42,
        head=head,
        authority="sourcery",
        identity=None,
    ) == {"external:coderabbit", "external:sourcery"}


def test_policy_order_still_prefers_a_non_author_coding_family(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "openai-codex:mo,xai-cursor:mx")
    monkeypatch.setattr(review_policy, "registered_coding_actors", lambda: {
        "mo": "codex-reviewer",
        "mx": "cursor-reviewer",
    })
    monkeypatch.setattr(
        review_policy,
        "available_coding_reviewers",
        lambda candidates, **_kwargs: [candidate[:3] for candidate in candidates],
    )
    policy = review_policy.ReviewPolicy(
        external_reviewers=(),
        coding_fallbacks=("openai-codex", "xai-cursor"),
        timeout_seconds=900,
        sources={},
    )
    selected = create_pr.choose_initial_reviewer(
        540,
        "codex-author",
        "openai-codex",
        "author-login",
        policy=policy,
        external_states={service: create_pr.UNAVAILABLE for service in create_pr.EXTERNAL_REVIEWERS},
    )
    assert selected == ("xai-cursor", "mx", "cursor-reviewer")


def test_configured_timeout_is_used_by_external_decision(monkeypatch):
    observed = datetime(2026, 9, 1, tzinfo=timezone.utc)
    pr = {
        "createdAt": observed.isoformat(),
        "headRefOid": "a" * 40,
        "statusCheckRollup": [],
    }
    monkeypatch.setattr(
        review_policy,
        "gh_json",
        lambda _argv: [{"total_count": 0, "check_runs": []}],
    )
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: [])
    decision = create_pr._external_decision(
        42,
        pr,
        "coderabbit",
        observed + timedelta(seconds=600),
        timeout_seconds=600,
    )
    assert decision == ("external-pending-timeout", None)


def test_check_run_pagination_accepts_repeated_overall_total(monkeypatch):
    observed = datetime(2026, 9, 1, tzinfo=timezone.utc)
    pr = {
        "createdAt": observed.isoformat(),
        "headRefOid": "a" * 40,
        "statusCheckRollup": [],
    }
    monkeypatch.setattr(
        review_policy,
        "gh_json",
        lambda _argv: [
            {"total_count": 2, "check_runs": [{"name": "first"}]},
            {"total_count": 2, "check_runs": [{"name": "second"}]},
        ],
    )
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: [])
    monkeypatch.setattr(
        review_policy,
        "external_state",
        lambda *_args, **_kwargs: review_policy.PENDING,
    )
    decision = create_pr._external_decision(
        42,
        pr,
        "coderabbit",
        observed + timedelta(seconds=1),
        timeout_seconds=120,
    )
    assert decision == ("external-unavailable", None)


def test_reviewer_status_reports_retirement_sources_bindings_and_probes(monkeypatch):
    monkeypatch.setattr(
        review_policy, "load_repository_review_policy", lambda: (
            review_policy.review_policy_from_labels(configured_policy_labels()),
            configured_policy_labels(),
        )
    )
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,openai-codex:mo,xai-cursor:mx,google-antigravity:mg",
    )
    monkeypatch.setattr(review_policy, "registered_coding_actors", lambda: {
        "m1": "claude-reviewer",
        "mo": "codex-reviewer",
        "mx": "cursor-reviewer",
    })
    monkeypatch.setattr(
        review_policy,
        "available_coding_reviewers",
        lambda candidates, **_kwargs: [
            candidate[:3] for candidate in candidates if candidate[1] == "m1"
        ],
    )
    monkeypatch.setattr(review_policy, "coderabbit_capability", lambda *_args: {"state": "unavailable", "reason": "fixture"})
    status = review_policy.reviewer_status(
        probe=True,
        author_identity="MO",
        author_actor="author-login",
        runner=result,
    )
    by_identity = {item["identity"]: item for item in status["coding_reviewers"]}
    assert status["schema"] == "aru.reviewer-status/v3"
    assert status["policy"]["selection"] == "coderabbit-first"
    assert status["policy"]["external_reviewers"] == [
        "coderabbit"
    ]
    assert by_identity["m1"]["probe"] == "ok"
    assert by_identity["mo"]["probe"] == "ineligible"
    assert by_identity["mx"]["probe"] == "failed"
    assert by_identity["mg"]["reviewer_actor"] is None
    assert status["valid"] is False
    assert "configured coding identity mg has no reviewer binding" in status["errors"]
    assert status["warnings"] == []
    assert {item["policy_role"] for item in status["external_reviewers"]} == {"preferred", "retired"}
    assert status["configuration_sources"]["runtime_availability"] == "bounded on-demand probe"


def test_pure_external_status_does_not_require_unused_coding_configuration(
    monkeypatch,
):
    policy = review_policy.ReviewPolicy(
        external_reviewers=("coderabbit",),
        coding_fallbacks=(),
        timeout_seconds=120,
        sources={},
    )
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    monkeypatch.setattr(
        review_policy,
        "load_repository_review_policy",
        lambda: (policy, ("reviewer-registered:coderabbit",)),
    )
    monkeypatch.setattr(review_policy, "registered_coding_actors", lambda: {})
    status = review_policy.reviewer_status()
    assert status["valid"] is True
    assert status["errors"] == []
    assert status["coding_reviewers"] == []


def test_policy_label_names_fit_github_limit():
    assert all(len(name) <= 50 for name in configured_policy_labels())


def test_reviewer_status_cli_is_read_only_and_forwards_probe_exclusions(
    monkeypatch, capsys
):
    observed = {}
    payload = {
        "schema": "aru.reviewer-status/v3",
        "valid": True,
        "policy": {
            "selection": "coderabbit-first",
            "external_reviewers": ["coderabbit"],
            "coding_fallbacks": ["claude-code"],
            "timeout_seconds": 900,
        },
    }

    def status(**kwargs):
        observed.update(kwargs)
        return payload

    monkeypatch.setattr(create_pr, "reviewer_status", status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_pr.py",
            "--reviewer-status",
            "--probe-reviewers",
            "--agent",
            "codex-author",
            "--author-github-login",
            "author-login",
            "--json",
        ],
    )
    assert create_pr.main() == 0
    assert observed == {
        "probe": True,
        "author_identity": "codex-author",
        "author_actor": "author-login",
    }
    assert '"schema": "aru.reviewer-status/v3"' in capsys.readouterr().out


@pytest.mark.parametrize("override,expected", [
    ({}, "available"),
    ({"head_sha": "b" * 40}, "unavailable"),
    ({"app": {"slug": "untrusted"}}, "unavailable"),
    ({"status": "QUEUED"}, "unavailable"),
    ({"status": "COMPLETED", "conclusion": "SUCCESS"}, "unavailable"),
    ({"output": {"summary": "Access denied"}}, "unavailable"),
    ({"output": {"summary": "Review skipped"}}, "unavailable"),
    ({"output": {"summary": "Rate limit exceeded"}}, "unavailable"),
    ({"started_at": "2026-09-08T09:00:00Z"}, "unavailable"),
])
def test_capability_requires_recent_current_head_running_trusted_app(override, expected):
    from review_evidence import coderabbit_check_capability
    now = datetime(2026, 9, 8, 10, 10, tzinfo=timezone.utc)
    check = dict(name="CodeRabbit", app={"slug": "coderabbitai"}, head_sha="a" * 40,
                 status="IN_PROGRESS", started_at="2026-09-08T10:00:00Z")
    check.update(override)
    data = {"total_count": 1, "check_runs": [check]}
    assert coderabbit_check_capability(data, "a" * 40, now)["state"] == expected


def test_capability_transport_is_bounded_and_falls_back_on_unreadable(monkeypatch):
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    def unavailable(argv, *, timeout):
        assert argv == ["api", "repos/owner/repo/commits/" + "a" * 40 + "/check-runs?per_page=100&filter=latest"]
        assert timeout == 8
        raise create_pr.KernelError("bounded command timed out")
    monkeypatch.setattr(review_policy, "gh_json", unavailable)
    assert review_policy.coderabbit_capability("a" * 40)["state"] == "unavailable"


@pytest.mark.parametrize("retired", ["sourcery", "codeant"])
def test_retired_authority_never_waits_or_reads_provider_state(monkeypatch, retired):
    monkeypatch.setattr(review_policy, "repo_slug", lambda: pytest.fail("retired provider lookup"))
    assert review_policy.external_decision(42, {}, retired, datetime.now(timezone.utc)) == ("external-retired", None)


def test_missing_capability_immediately_uses_independent_coding_even_with_legacy_pool(monkeypatch):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1")
    policy = review_policy.ReviewPolicy(("sourcery", "codeant", "coderabbit"), ("claude-code",), 900, {})
    result = review_policy.select_reviewer_from_pool(
        policy, number=581, author_identity="writer", author_family="openai-codex",
        author_actor="writer", external_states={"sourcery": "available", "codeant": "available", "coderabbit": "pending"},
        reviewer_actors={"m1": "reviewer"}, probe_runner=lambda _a: pytest.fail("unused"),
        coding_probe=lambda **_kw: ("claude-code", "m1", "reviewer"))
    assert result == ("claude-code", "m1", "reviewer")
