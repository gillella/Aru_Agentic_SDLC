from __future__ import annotations

import json

import pytest

import merge_pr

HEAD = "a" * 40
OLD_HEAD = "b" * 40


def coding_pr(*, labels=None, author_login="author-login"):
    return {
        "number": 88,
        "title": "policy",
        "body": "Closes #508",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "fix/issue-508-policy",
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "labels": labels
        or [
            {"name": "review:claude-code"},
            {"name": "reviewer:claude-code-sub-1"},
            {"name": "reviewer-actor:independent-reviewer"},
            {"name": "author:codex-author"},
            {"name": "author-family:openai-codex"},
        ],
        "statusCheckRollup": [],
        "author": {"login": author_login},
    }


def payload(**overrides):
    record = {
        "head": HEAD,
        "reviewer": "claude-code-sub-1",
        "family": "claude-code",
        "submitted_by": "independent-reviewer",
        "verdict": "APPROVE",
        "summary": "I reviewed the issue contract, exact diff, surrounding code, and failure paths independently.",
        "verification": ["pytest tests/test_coding_review_gate.py -q completed successfully"],
        "findings": [
            {
                "severity": "low",
                "file": "scripts/merge_pr.py",
                "line": 300,
                "summary": "The resolved naming note does not block this exact head.",
                "resolved": True,
            }
        ],
        "issues": [508],
        "acceptance_criteria_reviewed": True,
        "diff_reviewed": True,
        "surrounding_code_reviewed": True,
    }
    record.update(overrides)
    return record


def review(attestation=None, *, state="APPROVED", actor="independent-reviewer", body=None):
    if body is None:
        body = (
            "Substantive independent review.\n\n"
            f"<!-- aru-coding-review:v1 {json.dumps(attestation or payload(), sort_keys=True)} -->"
        )
    return {
        "id": 1,
        "state": state,
        "commit_id": (attestation or {}).get("head", HEAD),
        "body": body,
        "user": {"login": actor, "type": "User"},
    }


def accepted(pr=None, reviews=None):
    return merge_pr.successful_coding_agent_review(
        pr or coding_pr(),
        reviews if reviews is not None else [review(payload())],
        "claude-code",
        [508],
    )


def test_distinct_family_exact_head_coding_agent_approval_is_accepted():
    assert accepted() is True


@pytest.mark.parametrize(
    ("pr", "attestation", "actor"),
    [
        (
            coding_pr(
                labels=[
                    {"name": "review:claude-code"},
                    {"name": "reviewer:codex-author"},
                    {"name": "reviewer-actor:independent-reviewer"},
                    {"name": "author:codex-author"},
                    {"name": "author-family:openai-codex"},
                ]
            ),
            payload(reviewer="codex-author"),
            "independent-reviewer",
        ),
        (coding_pr(), payload(submitted_by="author-login"), "author-login"),
    ],
    ids=["author-agent-identity", "author-github-actor"],
)
def test_author_self_review_is_rejected(pr, attestation, actor):
    assert accepted(pr, [review(attestation, actor=actor)]) is False


def test_multiple_authoritative_reviewers_are_rejected():
    pr = coding_pr(labels=[{"name": "review:claude-code"}, {"name": "review:codeant"}])
    with pytest.raises(merge_pr.KernelError, match="exactly one"):
        merge_pr.assigned_service(pr)


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [review(body="generic looks good")],
        [review(body="<!-- aru-coding-review:v1 {not-json} -->")],
        [review(payload(head=OLD_HEAD))],
        [review(payload(head=HEAD[:12]))],
    ],
    ids=["missing", "generic", "malformed", "stale", "abbreviated-head"],
)
def test_missing_malformed_or_stale_attestation_is_rejected(reviews):
    assert accepted(reviews=reviews) is False


def test_malformed_marker_from_unassigned_actor_does_not_spoof_authority():
    unrelated = review(
        actor="unrelated-reviewer",
        body="<!-- aru-coding-review:v1 {not-json} -->",
    )
    assert accepted(reviews=[unrelated, review(payload())]) is True


def test_request_changes_blocks_merge():
    finding = {
        "severity": "high",
        "file": "scripts/create_pr.py",
        "line": 200,
        "summary": "Capacity failure mutates the authority before a reviewer is selected.",
        "resolved": False,
    }
    attestation = payload(verdict="REQUEST_CHANGES", findings=[finding])
    assert accepted(reviews=[review(attestation, state="CHANGES_REQUESTED")]) is False


def test_unresolved_coding_agent_finding_blocks_merge():
    finding = payload()["findings"][0] | {"resolved": False}
    attestation = payload(findings=[finding])
    assert accepted(reviews=[review(attestation)]) is False


def test_spoofed_producer_and_conflicting_current_head_evidence_are_rejected():
    spoofed = review(payload(submitted_by="someone-else"))
    assert accepted(reviews=[spoofed]) is False
    assert accepted(reviews=[review(payload()), review(payload())]) is False


def test_unbound_github_actor_cannot_satisfy_assigned_coding_reviewer():
    pr = coding_pr()
    pr["labels"] = [
        label
        for label in pr["labels"]
        if not label["name"].startswith("reviewer-actor:")
    ] + [{"name": "reviewer-actor:another-reviewer"}]
    assert accepted(pr, [review(payload())]) is False


def test_exact_head_coding_approval_plus_green_ci_passes_gate(monkeypatch):
    pr = coding_pr()
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(
        merge_pr,
        "issue",
            lambda _number: {
                "body": "## Acceptance Criteria\n\n- [x] policy behavior verified",
                "labels": [{"name": "status:in-review"}],
            },
    )
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review(payload())])
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": HEAD, "state": "success", "checks": ["tests"]},
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("c" * 40, 0))
    gates = merge_pr.evaluate(88, HEAD)
    assert gates["reviewer"] == "claude-code"
    assert gates["head"] == HEAD
