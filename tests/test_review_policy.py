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
        "review-policy:primary=coderabbit",
        "review-policy:fallback-1=claude-code",
        "review-policy:fallback-2=openai-codex",
        "review-policy:fallback-3=xai-cursor",
        "review-policy:fallback-4=google-antigravity",
        "review-policy:timeout=600",
    )


def test_missing_policy_labels_preserve_external_first_defaults():
    policy = review_policy.review_policy_from_labels(
        labels("reviewer-registered:coderabbit", "reviewer-registered:sourcery")
    )
    assert policy.primary == "coderabbit"
    assert policy.fallbacks == review_policy.CODING_REVIEWERS
    assert policy.timeout_seconds == 120
    assert set(policy.sources.values()) == {"kernel-default"}


def test_repository_labels_configure_primary_fallbacks_and_timeout():
    policy = review_policy.review_policy_from_labels(configured_policy_labels())
    assert policy.as_dict() == {
        "primary": "coderabbit",
        "fallbacks": [
            "claude-code",
            "openai-codex",
            "xai-cursor",
            "google-antigravity",
        ],
        "timeout_seconds": 600,
        "sources": {
            "primary": "repository-label",
            "fallbacks": "repository-labels",
            "timeout_seconds": "repository-label",
        },
    }


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("review-policy:unknown=value",), "unsupported declarations"),
        (
            ("review-policy:primary=coderabbit", "review-policy:primary=sourcery"),
            "primary is ambiguous",
        ),
        (("review-policy:fallback-2=claude-code",), "contiguous"),
        (
            (
                "review-policy:primary=coderabbit",
                "review-policy:fallback-1=coderabbit",
            ),
            "duplicate authorities",
        ),
        (("review-policy:primary=unknown",), "unsupported authority"),
        (("review-policy:primary=sourcery",), "unregistered external"),
        (("review-policy:timeout=fast",), "integer number"),
        (("review-policy:timeout=30",), "60-86400"),
    ],
)
def test_malformed_or_contradictory_policy_fails_closed(extra, message):
    with pytest.raises(create_pr.KernelError, match=message):
        review_policy.review_policy_from_labels(
            labels("reviewer-registered:coderabbit", *extra)
        )


def test_configured_primary_overrides_static_external_order():
    policy = review_policy.review_policy_from_labels(
        labels(
            "reviewer-registered:coderabbit",
            "reviewer-registered:sourcery",
            "review-policy:primary=sourcery",
            "review-policy:fallback-1=claude-code",
        )
    )
    selected = create_pr.choose_initial_reviewer(
        540,
        "codex-author",
        "openai-codex",
        "author-login",
        policy=policy,
        external_states={
            "coderabbit": create_pr.AVAILABLE,
            "sourcery": create_pr.AVAILABLE,
            "codeant": create_pr.UNAVAILABLE,
        },
        reviewer_actors={},
    )
    assert selected == ("sourcery", None, None)


def test_missing_coding_configuration_skips_to_ordered_external_fallback(
    monkeypatch,
):
    monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    policy = review_policy.ReviewPolicy(
        primary="claude-code",
        fallbacks=("coderabbit",),
        timeout_seconds=120,
        sources={},
    )
    selected = create_pr.choose_initial_reviewer(
        540,
        "codex-author",
        "openai-codex",
        "author-login",
        policy=policy,
        external_states={
            "coderabbit": create_pr.AVAILABLE,
            "sourcery": create_pr.UNAVAILABLE,
            "codeant": create_pr.UNAVAILABLE,
        },
        reviewer_actors={},
    )
    assert selected == ("coderabbit", None, None)


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
        primary="openai-codex",
        fallbacks=("xai-cursor",),
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


def test_reviewer_status_reports_sources_bindings_probes_and_unused_trials(monkeypatch):
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
    status = review_policy.reviewer_status(
        probe=True,
        author_identity="MO",
        author_actor="author-login",
        runner=result,
    )
    by_identity = {item["identity"]: item for item in status["coding_reviewers"]}
    assert status["schema"] == "aru.reviewer-status/v1"
    assert status["policy"]["primary"] == "coderabbit"
    assert by_identity["m1"]["probe"] == "ok"
    assert by_identity["mo"]["probe"] == "ineligible"
    assert by_identity["mx"]["probe"] == "failed"
    assert by_identity["mg"]["reviewer_actor"] is None
    assert status["valid"] is False
    assert "configured coding identity mg has no reviewer binding" in status["errors"]
    assert "registered external reviewer sourcery is not used by the policy" in status["warnings"]
    assert status["configuration_sources"]["runtime_availability"] == "bounded on-demand probe"


def test_policy_label_names_fit_github_limit():
    assert all(len(name) <= 50 for name in configured_policy_labels())


def test_reviewer_status_cli_is_read_only_and_forwards_probe_exclusions(
    monkeypatch, capsys
):
    observed = {}
    payload = {
        "schema": "aru.reviewer-status/v1",
        "valid": True,
        "policy": {
            "primary": "coderabbit",
            "fallbacks": ["claude-code"],
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
    assert '"schema": "aru.reviewer-status/v1"' in capsys.readouterr().out
