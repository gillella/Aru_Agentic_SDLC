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
    monkeypatch.setattr(review_policy, "gh_json", lambda argv: [] if "statuses" in argv[-1] else [{"total_count": 0, "check_runs": []}])
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: [])
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=600), timeout_seconds=600) == ("external-pending-timeout", None)
    # No activity yet: wait only through the 120 s activity window, then fall back; a skip ends it at once.
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=1), timeout_seconds=600) == ("external-pending", 119)
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=121), timeout_seconds=600) == ("external-unavailable", None)
    def with_status(status):
        monkeypatch.setattr(review_policy, "gh_json", lambda argv: [[status]] if "statuses" in argv[-1] else [{"total_count": 0, "check_runs": []}])
    with_status(cr_status("success", "Review skipped: excluded by label configuration", "2026-09-01T00:00:30Z"))
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=60), timeout_seconds=600) == ("external-unavailable", None)
    with_status(cr_status("pending", "Review in progress", "2026-09-01T00:01:00Z"))  # running: full deadline applies
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=300), timeout_seconds=600) == ("external-pending", 300)


def test_check_run_pagination_accepts_repeated_overall_total(monkeypatch):
    observed = datetime(2026, 9, 1, tzinfo=timezone.utc)
    pr = {
        "createdAt": observed.isoformat(),
        "headRefOid": "a" * 40,
        "statusCheckRollup": [],
    }
    pages = [{"total_count": 2, "check_runs": [{"name": "first"}]}, {"total_count": 2, "check_runs": [{"name": "second"}]}]
    monkeypatch.setattr(review_policy, "gh_json", lambda argv: [] if "statuses" in argv[-1] else pages)
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: [])
    monkeypatch.setattr(
        review_policy,
        "external_state",
        lambda *_args, **_kwargs: review_policy.PENDING,
    )
    assert create_pr._external_decision(42, pr, "coderabbit", observed + timedelta(seconds=1), timeout_seconds=120) == ("external-pending", 119)


def test_reviewer_status_reports_retirement_sources_bindings_and_probes(monkeypatch):
    labels = configured_policy_labels()
    monkeypatch.setattr(review_policy, "load_repository_review_policy",
                        lambda: (review_policy.review_policy_from_labels(labels), labels))
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1,openai-codex:mo,xai-cursor:mx,google-antigravity:mg")
    monkeypatch.setattr(review_policy, "registered_coding_actors",
                        lambda: {"m1": "claude-reviewer", "mo": "codex-reviewer", "mx": "cursor-reviewer"})
    monkeypatch.setattr(review_policy, "available_coding_reviewers",
                        lambda candidates, **_kwargs: [c[:3] for c in candidates if c[1] == "m1"])
    monkeypatch.setattr(review_policy, "coderabbit_capability", lambda *_args: {"state": "unavailable", "reason": "fixture"})
    status = review_policy.reviewer_status(probe=True, author_identity="MO", author_actor="author-login", runner=result)
    by_identity = {item["identity"]: item for item in status["coding_reviewers"]}
    assert status["schema"] == "aru.reviewer-status/v3"
    assert status["policy"]["selection"] == "coderabbit-first"
    assert status["policy"]["external_reviewers"] == ["coderabbit"]
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


def test_reviewer_status_cli_is_read_only_and_forwards_probe_exclusions(monkeypatch, capsys):
    observed = {}
    payload = {"schema": "aru.reviewer-status/v3", "valid": True, "policy": {
        "selection": "coderabbit-first", "external_reviewers": ["coderabbit"], "coding_fallbacks": ["claude-code"], "timeout_seconds": 900}}

    def status(**kwargs):
        observed.update(kwargs)
        return payload

    monkeypatch.setattr(create_pr, "reviewer_status", status)
    monkeypatch.setattr(sys, "argv", ["create_pr.py", "--reviewer-status", "--probe-reviewers", "--agent", "codex-author",
                                      "--author-github-login", "author-login", "--json"])
    assert create_pr.main() == 0
    assert observed == {"probe": True, "author_identity": "codex-author", "author_actor": "author-login"}
    assert '"schema": "aru.reviewer-status/v3"' in capsys.readouterr().out


def cr_status(state, description, at="2026-09-08T10:00:00Z", creator={"login": "coderabbitai[bot]", "type": "Bot"}):
    return {"context": "CodeRabbit", "state": state, "description": description, "creator": creator, "created_at": at}  # real Status API shape


def cr_check(status, conclusion=None, at="2026-09-08T10:00:00Z", app="coderabbitai", head="a" * 40, **output):
    return {"name": "CodeRabbit", "app": {"slug": app}, "head_sha": head, "status": status, "conclusion": conclusion,
            "started_at": at, "output": output}


SINCE = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("statuses,checks,since,expected", [
    ([], [], None, ("pending", "no-coderabbit-evidence-yet")),
    # Legacy commit-status surface: queued is not proof, in-progress and completed are.
    ([cr_status("pending", "Review queued")], [], None, ("pending", "review-queued")),
    ([cr_status("pending", "Review in progress")], [], None, ("available", "current-head-review-running")),
    ([cr_status("success", "Review completed")], [], None, ("available", "current-head-review-completed")),
    ([cr_status("success", "No review result here")], [], None, ("pending", "unrecognized-success")),
    ([cr_status("pending", "Review in progress", "2026-09-08T09:00:00Z")], [], None, ("unavailable", "review-stalled")),
    ([cr_status("success", "Review completed", "2020-01-01T00:00:00Z")], [], None, ("unavailable", "completed-without-current-verdict")),
    ([cr_status("success", "Review completed", "2099-01-01T00:00:00Z")], [], None, ("unavailable", "future-timestamp")),
    ([cr_status("error", "Internal error")], [], None, ("unavailable", "provider-error")),
    ([cr_status("success", "Review rate limited")], [], None, ("unavailable", "provider-denied")),
    # Label-gated skip before assignment keeps CodeRabbit eligible; after assignment it is a refusal.
    ([cr_status("success", "Review skipped: excluded by label configuration")], [], None, ("pending", "awaiting-review-label")),
    ([cr_status("success", "Review skipped: excluded by label configuration")], [], SINCE, ("unavailable", "skipped-after-assignment")),
    # App check-run surface (review_progress) is equally authentic; foreign apps and heads are ignored.
    ([], [cr_check("QUEUED")], None, ("pending", "review-queued")),
    ([], [cr_check("IN_PROGRESS")], None, ("available", "current-head-review-running")),
    ([], [cr_check("COMPLETED", "SUCCESS")], None, ("available", "current-head-review-completed")),
    ([], [cr_check("COMPLETED", "CANCELLED")], None, ("unavailable", "provider-error")),
    ([], [cr_check("COMPLETED", "SUCCESS", summary="Review skipped: excluded by label configuration")], SINCE, ("unavailable", "skipped-after-assignment")),
    ([], [cr_check("IN_PROGRESS", app="untrusted"), cr_check("IN_PROGRESS", head="b" * 40)], None, ("pending", "no-coderabbit-evidence-yet")),
    # Spoofed statuses never count; the newest trusted signal wins across surfaces.
    ([cr_status("success", "Review completed", creator={"login": "human", "type": "User"})], [], None, ("pending", "no-coderabbit-evidence-yet")),
    ([cr_status("pending", "Review queued", "2026-09-08T09:59:00Z")], [cr_check("COMPLETED", "SUCCESS")], None, ("available", "current-head-review-completed")),
    # Same-second disagreement fails closed; same-second agreement across surfaces does not.
    ([cr_status("pending", "Review in progress"), cr_status("error", "Internal error")], [], None, ("unavailable", "conflicting-evidence")),
    ([cr_status("pending", "Review in progress")], [cr_check("IN_PROGRESS")], None, ("available", "current-head-review-running")),
])
def test_capability_reads_authenticated_coderabbit_activity_on_either_surface(statuses, checks, since, expected):
    from review_evidence import coderabbit_capability_state
    now = datetime(2026, 9, 8, 10, 10, tzinfo=timezone.utc)
    result = coderabbit_capability_state(statuses, checks, head="a" * 40, observed_at=now, since=since, timeout_seconds=900)
    assert (result["state"], result["reason"]) == expected


def test_capability_transport_is_bounded_and_falls_back_on_unreadable(monkeypatch):
    monkeypatch.setattr(review_policy, "repo_slug", lambda: "owner/repo")
    def unavailable(argv, *, timeout):
        assert argv == ["api", "repos/owner/repo/commits/" + "a" * 40 + "/statuses?per_page=100"]
        assert timeout == 8
        raise create_pr.KernelError("bounded command timed out")
    monkeypatch.setattr(review_policy, "gh_json", unavailable)
    assert review_policy.coderabbit_capability("a" * 40)["state"] == "unavailable"
    monkeypatch.setattr(review_policy, "gh_json", lambda argv, *, timeout: {"message": "Not Found"})
    assert review_policy.coderabbit_capability("a" * 40)["state"] == "unavailable"  # not a status list
    # Both surfaces read and empty: eligible but unproven. A non-status payload is unreadable, not "empty".
    monkeypatch.setattr(review_policy, "gh_json", lambda argv, *, timeout: [] if "statuses" in argv[1] else {"total_count": 0, "check_runs": []})
    assert review_policy.coderabbit_capability("a" * 40)["reason"] == "no-coderabbit-evidence-yet"
    monkeypatch.setattr(review_policy, "gh_json", lambda argv, *, timeout: [{"total_count": 0, "check_runs": []}])
    assert review_policy.coderabbit_capability("a" * 40)["state"] == "unavailable"
    observed = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
    pr = {"createdAt": observed.isoformat(), "headRefOid": "a" * 40}
    monkeypatch.setattr(review_policy, "gh_paginated", lambda _endpoint: [])
    for malformed in (None, {"unexpected": "malformed status record"}, {"context": [], "state": "pending"}, {"context": "CodeRabbit", "state": {}}):
        for records in ([malformed], [cr_status("pending", "Review in progress"), malformed]):
            def inventory(argv, **_kwargs):
                data = records if "statuses?" in argv[-1] else {"total_count": 0, "check_runs": []}
                return [data] if "--slurp" in argv else data
            monkeypatch.setattr(review_policy, "gh_json", inventory)
            assert review_policy.coderabbit_capability("a" * 40)["state"] == "unavailable"
            with pytest.raises(create_pr.KernelError, match="commit status inventory is malformed"):
                review_policy.external_decision(42, pr, "coderabbit", observed + timedelta(seconds=1))


@pytest.mark.parametrize("retired", ["sourcery", "codeant"])
def test_retired_authority_never_waits_or_reads_provider_state(monkeypatch, retired):
    monkeypatch.setattr(review_policy, "repo_slug", lambda: pytest.fail("retired provider lookup"))
    assert review_policy.external_decision(42, {}, retired, datetime.now(timezone.utc)) == ("external-retired", None)


@pytest.mark.parametrize("coderabbit,expected", [
    ("unavailable", ("claude-code", "m1", "reviewer")),  # explicit denial: immediate coding fallback
    ("pending", ("coderabbit", None, None)),  # no status yet on a fresh PR: CodeRabbit is selected
    ("available", ("coderabbit", None, None)),
])
def test_denied_coderabbit_uses_independent_coding_while_unproven_stays_eligible(monkeypatch, coderabbit, expected):
    monkeypatch.setenv("ARU_CODING_REVIEWERS", "claude-code:m1@1")
    policy = review_policy.ReviewPolicy(("sourcery", "codeant", "coderabbit"), ("claude-code",), 900, {})
    result = review_policy.select_reviewer_from_pool(
        policy, number=581, author_identity="writer", author_family="openai-codex",
        author_actor="writer", external_states={"sourcery": "available", "codeant": "available", "coderabbit": coderabbit},
        reviewer_actors={"m1": "reviewer"}, probe_runner=lambda _a: pytest.fail("unused"),
        coding_probe=lambda **_kw: ("claude-code", "m1", "reviewer"))
    assert result == expected
