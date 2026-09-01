from __future__ import annotations

import pytest

import create_pr
import review_policy


def external_states(**overrides):
    states = {service: create_pr.UNAVAILABLE for service in create_pr.EXTERNAL_REVIEWERS}
    states.update(overrides)
    return states


def verification_body(command: str) -> str:
    return f"## Summary\n\nSummary\n\n## Verification\n\n- `{command}`"


def test_create_pr_binds_head_and_exactly_one_reviewer(monkeypatch):
    record = {"number": 6, "labels": [{"name": "agent:codex-1"}]}
    lifecycle = ["In Progress"]
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: lifecycle[0])
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["scripts/create_pr.py"])
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(
        create_pr,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "url": "https://example/pr/12",
            "state": "OPEN",
            "createdAt": "2026-09-01T12:00:00Z",
            "headRefOid": "a" * 40,
            "labels": [
                {"name": "review:coderabbit"},
                {"name": "author:codex-1"},
                {"name": "author-family:openai-codex"},
            ],
        },
    )
    statuses = []

    def transition(number, status, **kwargs):
        kwargs["pre_mutation_check"]()
        statuses.append((number, status, kwargs["expected_current"]))
        lifecycle[0] = status

    monkeypatch.setattr(create_pr, "set_status", transition)

    outcome = create_pr.create(
        6,
        "feat: small",
        verification_body("python3 -m pytest tests/test_create_pr_creation.py -q"),
        "codex-1",
        external_states=external_states(coderabbit=create_pr.AVAILABLE),
        reviewer_actors={},
        author_actor="author-login",
    )
    assert outcome["reviewer"] == "coderabbit"
    assert outcome["review_required"] is True
    assert outcome["risk_tier"] == 2
    assert outcome["head"] == "a" * 40
    assert statuses == [(6, "In Review", "In Progress")]
    assert outcome["next_action"] == "refresh-reviewer"
    assert outcome["retry_at"] == "2026-09-01T12:02:00+00:00"
    body = commands[0][commands[0].index("--body") + 1]
    assert body.count("Closes #6") == 1
    assert "aru-local-verification" not in body


def test_create_pr_rejects_caller_closing_directive(monkeypatch):
    record = {"number": 1, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    with pytest.raises(create_pr.KernelError, match="closing directive"):
        create_pr.create(1, "feat: bad", "Closes #99", "codex-1")


def test_missing_authority_continuation_is_read_only(monkeypatch):
    monkeypatch.setattr(
        review_policy,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "labels": [
                {"name": "author:codex-1"},
                {"name": "author-family:openai-codex"},
            ],
        },
    )
    monkeypatch.setattr(
        create_pr,
        "refresh_assignment",
        lambda *_args, **_kwargs: pytest.fail("continuation must not mutate labels"),
    )

    outcome = create_pr.reviewer_continuation(12)

    assert outcome["authority"] is None
    assert outcome["next_action"] == "refresh-reviewer"
    assert outcome["retry_at"]


def test_create_pr_closes_new_pr_when_post_create_head_is_wrong(monkeypatch):
    record = {"number": 1, "labels": [{"name": "agent:codex-1"}]}
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: "In Progress")
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-1-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["README.md"])
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    snapshots = iter(
        [
            {
                "number": 12,
                "url": "https://example/pr/12",
                "state": "OPEN",
                "createdAt": "2026-09-01T12:00:00Z",
                "headRefOid": "b" * 40,
                "labels": [
                    {"name": "author:codex-1"},
                    {"name": "author-family:openai-codex"},
                ],
            },
            {"number": 12, "state": "CLOSED"},
        ]
    )
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: next(snapshots))
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))

    with pytest.raises(create_pr.KernelError, match="published head"):
        create_pr.create(1, "docs: clarify", "Summary only", "codex-1")

    assert commands[-1][:4] == ["gh", "pr", "close", "12"]


def test_create_pr_skips_review_for_low_risk_change(monkeypatch):
    record = {"number": 1, "labels": [{"name": "agent:codex-1"}]}
    lifecycle = ["In Progress"]
    monkeypatch.setattr(create_pr, "issue", lambda _number: record)
    monkeypatch.setattr(create_pr, "status_of", lambda _record: lifecycle[0])
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-1-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["README.md"])
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(
        create_pr,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "url": "https://example/pr/12",
            "state": "OPEN",
            "createdAt": "2026-09-01T12:00:00Z",
            "headRefOid": "a" * 40,
            "labels": [
                {"name": "author:codex-1"},
                {"name": "author-family:openai-codex"},
            ],
        },
    )

    def transition(_number, status, **kwargs):
        kwargs["pre_mutation_check"]()
        lifecycle[0] = status

    monkeypatch.setattr(create_pr, "set_status", transition)
    outcome = create_pr.create(1, "docs: clarify", "Summary only", "codex-1")

    assert outcome["reviewer"] is None
    assert outcome["review_required"] is False
    assert outcome["risk_tier"] == 0
    assert not any(
        value.startswith("review:") for command in commands for value in command
    )
