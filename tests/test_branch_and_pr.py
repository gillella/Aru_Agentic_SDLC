from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import create_branch
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


def assignment_pr(*, created_at: datetime, state="pending"):
    check = []
    if state == "unavailable":
        check = [
            {
                "name": "CodeRabbit",
                "conclusion": "ACTION_REQUIRED",
                "status": "COMPLETED",
                "completedAt": created_at.isoformat(),
            }
        ]
    if state == "available":
        check = [
            {
                "name": "CodeRabbit",
                "conclusion": "SUCCESS",
                "status": "COMPLETED",
                "completedAt": created_at.isoformat(),
            }
        ]
    return {
        "number": 42,
        "url": "https://example/pr/42",
        "createdAt": created_at.isoformat(),
        "headRefOid": "a" * 40,
        "author": {"login": "author-login"},
        "labels": [
            {"name": "review:coderabbit"},
            {"name": "author:codex-author"},
            {"name": "author-family:openai-codex"},
        ],
        "statusCheckRollup": check,
    }


def install_refresh(
    monkeypatch,
    pr,
    *,
    updated_labels=None,
    comments=None,
    events=None,
    statuses=None,
):
    responses = [pr]
    if updated_labels is not None:
        responses.append({"number": 42, "labels": updated_labels})
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: responses.pop(0))
    monkeypatch.setattr(create_pr, "repo_slug", lambda: "owner/repo")
    evidence = iter([[], comments or [], events or [], statuses or []])
    monkeypatch.setattr(create_pr, "gh_paginated", lambda _endpoint: next(evidence))
    monkeypatch.setattr(create_pr, "run", lambda _argv: None)


def test_external_registration_reads_beyond_first_hundred_labels(monkeypatch):
    commands = []

    def labels(argv):
        commands.append(argv)
        return [{"name": f"label-{index}"} for index in range(150)] + [
            {"name": "reviewer-registered:sourcery"}
        ]

    monkeypatch.setattr(create_pr, "gh_json", labels)
    states = create_pr.registered_external_states()
    assert states["sourcery"] == create_pr.AVAILABLE
    assert commands[0][commands[0].index("--limit") + 1] == "1000"


def test_authority_labels_alone_do_not_register_external_providers(monkeypatch):
    monkeypatch.setattr(
        create_pr,
        "gh_json",
        lambda _argv: [{"name": "review:coderabbit"}, {"name": "review:sourcery"}],
    )
    assert set(create_pr.registered_external_states().values()) == {create_pr.UNAVAILABLE}


def test_author_family_is_deprioritized_and_author_identity_excluded(monkeypatch):
    monkeypatch.setattr(create_pr, "_command", lambda name: f"/bin/{name}")
    calls = []

    def probe(argv):
        calls.append(argv)
        return result(argv)

    reviewer = create_pr.probe_coding_reviewer(
        author_identity="m1",
        author_family="claude-code",
        author_actor="author-login",
        rotation_key=1,
        reviewer_actors={"mo": "codex-reviewer"},
        runner=probe,
    )
    assert reviewer == ("openai-codex", "mo", "codex-reviewer")
    assert calls[0][0] == "/bin/codex"


def test_pending_external_under_15_minutes_does_not_fallback(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created)
    install_refresh(monkeypatch, pr)
    monkeypatch.setattr(
        create_pr,
        "probe_coding_reviewer",
        lambda **_kwargs: pytest.fail("capacity probe must not run before timeout"),
    )
    outcome = create_pr.refresh_assignment(42, now=created + timedelta(minutes=14, seconds=59))
    assert outcome["authority"] == "coderabbit"
    assert outcome["reason"] == "external-pending"
    assert outcome["remaining_seconds"] == 1


def test_pending_external_at_15_minutes_falls_back(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created)
    updated = [
        {"name": "review:claude-code"},
        {"name": "reviewer:claude-code-sub-1"},
        {"name": "reviewer-actor:claude-reviewer"},
        {"name": "author:codex-author"},
        {"name": "author-family:openai-codex"},
    ]
    install_refresh(monkeypatch, pr, updated_labels=updated)
    monkeypatch.setattr(
        create_pr,
        "probe_coding_reviewer",
        lambda **_kwargs: ("claude-code", "claude-code-sub-1", "claude-reviewer"),
    )
    events = []
    monkeypatch.setattr(create_pr, "run", lambda _argv: events.append("audit"))
    monkeypatch.setattr(
        create_pr,
        "replace_authority",
        lambda *_args: events.append("replace"),
    )
    outcome = create_pr.refresh_assignment(42, now=created + timedelta(minutes=15))
    assert outcome == {
        "pr": 42,
        "authority": "claude-code",
        "reviewer": "claude-code-sub-1",
        "action": "fallback",
        "reason": "external-pending-15m",
    }
    assert events == ["audit", "replace"]


def test_reviewer_replacement_rejects_changed_live_authority(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    stale = assignment_pr(created_at=created)
    changed = assignment_pr(created_at=created)
    changed["labels"][0] = {"name": "review:codeant"}
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: changed)
    monkeypatch.setattr(
        create_pr,
        "ensure_label",
        lambda *_args, **_kwargs: pytest.fail("labels must not mutate after a race"),
    )
    with pytest.raises(create_pr.KernelError, match="changed during fallback"):
        create_pr.replace_authority(
            42,
            stale,
            "claude-code",
            "claude-code-sub-1",
            "claude-reviewer",
        )


def test_reviewer_replacement_preserves_concurrent_unrelated_labels(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    original = assignment_pr(created_at=created)
    concurrent = assignment_pr(created_at=created)
    concurrent["labels"].append({"name": "acceptance:passed"})
    responses = iter([original, concurrent])
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: next(responses))
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))

    create_pr.replace_authority(
        42,
        original,
        "claude-code",
        "claude-code-sub-1",
        "claude-reviewer",
    )

    command = commands[-1]
    assert command[:4] == ["gh", "pr", "edit", "42"]
    assert "acceptance:passed" not in command
    assert command[command.index("--remove-label") + 1] == "review:coderabbit"


def test_external_recovery_before_write_aborts_fallback(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    initial = assignment_pr(created_at=created, state="unavailable")
    recovered = assignment_pr(created_at=created, state="available")
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: initial)
    monkeypatch.setattr(create_pr, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(create_pr, "gh_paginated", lambda _endpoint: [])
    monkeypatch.setattr(
        create_pr,
        "probe_coding_reviewer",
        lambda **_kwargs: ("claude-code", "claude-code-sub-1", "claude-reviewer"),
    )
    events = []
    monkeypatch.setattr(create_pr, "run", lambda _argv: events.append("audit"))

    def replacement(*args):
        events.append("revalidated")
        args[-1](recovered)

    monkeypatch.setattr(create_pr, "replace_authority", replacement)
    with pytest.raises(create_pr.KernelError, match="external reviewer recovered"):
        create_pr.refresh_assignment(42, now=created + timedelta(seconds=1))
    assert events == ["audit", "revalidated"]


def test_explicit_external_error_falls_back_immediately(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created, state="unavailable")
    updated = [
        {"name": "review:xai-cursor"},
        {"name": "reviewer:xai-cursor"},
        {"name": "reviewer-actor:cursor-reviewer"},
        {"name": "author:codex-author"},
        {"name": "author-family:openai-codex"},
    ]
    install_refresh(monkeypatch, pr, updated_labels=updated)
    monkeypatch.setattr(
        create_pr,
        "probe_coding_reviewer",
        lambda **_kwargs: ("xai-cursor", "xai-cursor", "cursor-reviewer"),
    )
    monkeypatch.setattr(create_pr, "replace_authority", lambda *_args: None)
    outcome = create_pr.refresh_assignment(42, now=created + timedelta(seconds=1))
    assert outcome["reason"] == "external-unavailable"
    assert outcome["authority"] == "xai-cursor"


@pytest.mark.parametrize(
    "message",
    [
        "Quota exhausted",
        "Provider outage",
        "Rate limit reached",
        "Reviews paused",
        "Unsupported bot-authored PR",
        "Payment required",
        "Unable to review due to capacity exhausted",
    ],
)
def test_explicit_provider_unavailability_messages_are_detected(message):
    observed_at = datetime.now(timezone.utc)
    pr = assignment_pr(created_at=observed_at)
    comment = {
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "body": message,
        "created_at": observed_at.isoformat(),
    }
    assert create_pr.external_state(pr, "coderabbit", reviews=[], comments=[comment]) == create_pr.UNAVAILABLE


def test_review_failure_is_not_misclassified_as_provider_unavailability():
    observed_at = datetime.now(timezone.utc)
    pr = assignment_pr(created_at=observed_at)
    pr["statusCheckRollup"] = [
        {
            "name": "CodeRabbit",
            "conclusion": "FAILURE",
            "status": "COMPLETED",
            "completedAt": observed_at.isoformat(),
        }
    ]
    assert create_pr.external_state(pr, "coderabbit", reviews=[], comments=[]) == create_pr.AVAILABLE


def test_latest_external_evidence_wins_after_provider_recovery():
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created)
    outage = {
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "body": "Quota exhausted",
        "created_at": created.isoformat(),
    }
    approval = {
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "body": "Substantive review complete",
        "state": "APPROVED",
        "submitted_at": (created + timedelta(minutes=2)).isoformat(),
    }
    assert (
        create_pr.external_state(
            pr,
            "coderabbit",
            reviews=[approval],
            comments=[outage],
        )
        == create_pr.AVAILABLE
    )


def test_latest_recovered_check_wins_over_old_provider_outage():
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created)
    pr["statusCheckRollup"] = [
        {
            "name": "CodeRabbit",
            "state": "SUCCESS",
            "startedAt": (created + timedelta(minutes=3)).isoformat(),
        }
    ]
    outage = {
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "body": "Quota exhausted",
        "created_at": created.isoformat(),
    }
    assert (
        create_pr.external_state(
            pr,
            "coderabbit",
            reviews=[],
            comments=[outage],
        )
        == create_pr.AVAILABLE
    )


def test_recovered_external_gets_full_timeout_from_assignment(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    assigned = created + timedelta(hours=1)
    pr = assignment_pr(created_at=created)
    event = {
        "event": "labeled",
        "label": {"name": "review:coderabbit"},
        "created_at": assigned.isoformat(),
    }
    install_refresh(monkeypatch, pr, events=[event])
    monkeypatch.setattr(
        create_pr,
        "probe_coding_reviewer",
        lambda **_kwargs: pytest.fail(
            "recovered reviewer must receive its full timeout"
        ),
    )
    outcome = create_pr.refresh_assignment(
        42,
        now=assigned + timedelta(minutes=14, seconds=59),
    )
    assert outcome["reason"] == "external-pending"
    assert outcome["remaining_seconds"] == 1


def test_no_external_or_coding_reviewer_fails_closed(monkeypatch):
    monkeypatch.setattr(create_pr, "_command", lambda name: f"/bin/{name}")
    with pytest.raises(create_pr.KernelError, match="no external or distinct coding-agent"):
        create_pr.choose_initial_reviewer(
            9,
            "codex-author",
            "openai-codex",
            "author-login",
            external_states=external_states(),
            reviewer_actors={
                "m1": "claude-reviewer-1",
                "m2": "claude-reviewer-2",
                "m3": "claude-reviewer-3",
                "mx": "cursor-reviewer",
                "mg": "google-reviewer",
            },
            probe_runner=lambda argv: result(argv, ok=False),
        )


@pytest.mark.parametrize(
    ("identity", "family"),
    [
        ("m1", "claude-code"),
        ("m2", "claude-code"),
        ("m3", "claude-code"),
        ("mo", "openai-codex"),
        ("mx", "xai-cursor"),
        ("mg", "google-antigravity"),
        ("n1", "claude-code"),
        ("n2", "claude-code"),
        ("n3", "claude-code"),
        ("no", "openai-codex"),
        ("nx", "xai-cursor"),
        ("ng", "google-antigravity"),
    ],
)
def test_configured_agent_identities_map_to_model_family(monkeypatch, identity, family):
    if identity.startswith("n"):
        monkeypatch.setenv(
            "ARU_CODING_REVIEWERS",
            "claude-code:n1@1,claude-code:n2@2,claude-code:n3@3,"
            "openai-codex:no,xai-cursor:nx,google-antigravity:ng",
        )
    assert create_pr.agent_family(identity) == family


def test_mac_mini_uses_only_its_local_reviewer_pool(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:n1@1,claude-code:n2@2,claude-code:n3@3,"
        "openai-codex:no,xai-cursor:nx,google-antigravity:ng",
    )
    monkeypatch.setattr(create_pr, "_command", lambda name: f"/bin/{name}")
    reviewer = create_pr.probe_coding_reviewer(
        author_identity="n1",
        author_family="claude-code",
        author_actor="author-login",
        rotation_key=0,
        reviewer_actors={"mo": "wrong-host", "no": "mini-codex"},
        runner=lambda argv: result(argv),
    )
    assert reviewer == ("openai-codex", "no", "mini-codex")


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        "",
        "claude-code",
        "claude-code:m1",
        "claude-code:m1@1,claude-code:m2@1",
        "openai-codex:mo@1",
        "openai-codex:mo,openai-codex:mx",
    ],
)
def test_missing_or_malformed_reviewer_configuration_fails_closed(
    monkeypatch, configuration
):
    if configuration is None:
        monkeypatch.delenv("ARU_CODING_REVIEWERS", raising=False)
    else:
        monkeypatch.setenv("ARU_CODING_REVIEWERS", configuration)
    with pytest.raises(create_pr.KernelError, match="ARU_CODING_REVIEWERS"):
        create_pr.probe_coding_reviewer(
            author_identity="author",
            author_family="human-or-other",
            author_actor="author-login",
            rotation_key=0,
            reviewer_actors={"m1": "review-bot"},
            runner=lambda argv: result(argv),
        )


def test_new_claude_subscription_is_added_by_configuration_only(monkeypatch):
    monkeypatch.setenv(
        "ARU_CODING_REVIEWERS",
        "claude-code:m1@1,claude-code:m2@2,claude-code:m3@3,claude-code:m4@4",
    )
    calls = []

    def probe(argv):
        calls.append(argv)
        return result(argv, ok=argv[1] == "4")

    reviewer = create_pr.probe_coding_reviewer(
        author_identity="author",
        author_family="human-or-other",
        author_actor="author-login",
        rotation_key=0,
        reviewer_actors={"m4": "claude-reviewer-4"},
        runner=probe,
    )
    assert reviewer == ("claude-code", "m4", "claude-reviewer-4")
    assert [call[1] for call in calls] == ["1", "2", "3", "4"]


def test_all_twelve_binding_labels_fit_github_limit():
    actor = "aru-code-factory-gillella[bot]"
    identities = (
        "m1", "m2", "m3", "mo", "mx", "mg",
        "n1", "n2", "n3", "no", "nx", "ng",
    )
    labels = [f"reviewer-binding:{identity}={actor}" for identity in identities]
    assert all(len(label) <= 50 for label in labels)


def test_refresh_with_no_coding_capacity_preserves_external_authority(monkeypatch):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created, state="unavailable")
    install_refresh(monkeypatch, pr)
    monkeypatch.setattr(create_pr, "probe_coding_reviewer", lambda **_kwargs: None)
    monkeypatch.setattr(
        create_pr,
        "replace_authority",
        lambda *_args: pytest.fail("authority must not change without capacity"),
    )
    with pytest.raises(create_pr.KernelError, match="no distinct coding-agent"):
        create_pr.refresh_assignment(42, now=created + timedelta(seconds=1))


def test_explicit_coding_reviewer_unavailability_recovers_to_registered_external(
    monkeypatch,
):
    created = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    pr = assignment_pr(created_at=created)
    pr["labels"] = [
        {"name": "review:xai-cursor"},
        {"name": "reviewer:xai-cursor"},
        {"name": "reviewer-actor:cursor-reviewer"},
        {"name": "author:codex-author"},
        {"name": "author-family:openai-codex"},
    ]
    updated = {
        "number": 42,
        "labels": [
            {"name": "review:codeant"},
            {"name": "author:codex-author"},
            {"name": "author-family:openai-codex"},
        ],
    }
    responses = [pr, updated]
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: responses.pop(0))
    monkeypatch.setattr(
        create_pr,
        "registered_external_states",
        lambda: external_states(codeant=create_pr.AVAILABLE),
    )
    events = []
    monkeypatch.setattr(create_pr, "run", lambda _argv: events.append("audit"))
    monkeypatch.setattr(create_pr, "replace_authority", lambda *_args: events.append("replace"))
    outcome = create_pr.refresh_assignment(
        42,
        now=created + timedelta(minutes=3),
        coding_unavailable_reason="full review aborted without a verdict",
    )
    assert outcome == {
        "pr": 42,
        "authority": "codeant",
        "action": "fallback",
        "reason": "coding-reviewer-unavailable",
    }
    assert events == ["audit", "replace"]


def test_create_pr_binds_head_and_exactly_one_reviewer(monkeypatch):
    record = {"number": 6, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(
        create_pr,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "url": "https://example/pr/12",
            "headRefOid": "a" * 40,
            "labels": [
                {"name": "review:coderabbit"},
                {"name": "author:codex-1"},
                {"name": "author-family:openai-codex"},
            ],
        },
    )
    statuses = []
    monkeypatch.setattr(create_pr, "set_status", lambda number, status: statuses.append((number, status)))

    outcome = create_pr.create(
        6,
        "feat: small",
        "Summary",
        "codex-1",
        external_states=external_states(coderabbit=create_pr.AVAILABLE),
        reviewer_actors={},
        author_actor="author-login",
    )
    assert outcome["reviewer"] == "coderabbit"
    assert outcome["head"] == "a" * 40
    assert statuses == [(6, "In Review")]
    body = commands[0][commands[0].index("--body") + 1]
    assert body.count("Closes #6") == 1


def test_create_pr_rejects_caller_closing_directive(monkeypatch):
    record = {"number": 1, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    with pytest.raises(create_pr.KernelError, match="closing directive"):
        create_pr.create(1, "feat: bad", "Closes #99", "codex-1")


def test_create_pr_revalidates_ownership_after_reviewer_selection(monkeypatch):
    records = iter(
        [
            {
                "number": 6,
                "labels": [
                    {"name": "status:in-progress"},
                    {"name": "agent:codex-1"},
                ],
            },
            {
                "number": 6,
                "labels": [
                    {"name": "status:in-progress"},
                    {"name": "agent:another-agent"},
                ],
            },
        ]
    )
    monkeypatch.setattr(create_pr, "issue", lambda _number: next(records))
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        create_pr,
        "run",
        lambda _argv: pytest.fail("stale owner must not create a pull request"),
    )
    with pytest.raises(create_pr.KernelError, match="ownership changed"):
        create_pr.create(
            6,
            "feat: small",
            "Summary",
            "codex-1",
            external_states=external_states(coderabbit=create_pr.AVAILABLE),
            reviewer_actors={},
            author_actor="author-login",
        )


def test_branch_requires_exclusive_claim(monkeypatch):
    monkeypatch.setattr(
        create_branch,
        "issue",
        lambda _number: {
            "number": 5,
            "title": "change",
            "labels": [{"name": "agent:someone-else"}],
        },
    )
    monkeypatch.setattr(create_branch, "status_of", lambda _record: "In Progress")
    with pytest.raises(create_branch.KernelError, match="exclusive"):
        create_branch.create_worktree(5, "fix", "codex-1")
