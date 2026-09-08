from __future__ import annotations

import pytest

import merge_pr


HEAD = "a" * 40
CREATED = "2026-09-01T10:00:00Z"
REVIEWED = "2026-09-01T10:05:00Z"


@pytest.fixture(autouse=True)
def complete_provider_inventory(monkeypatch):
    monkeypatch.setattr(merge_pr, "pull_events", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_review_checks", lambda _head: [])


def review_pr(service: str, *, checks=None):
    return {
        "number": 10,
        "body": "Closes #7",
        "createdAt": CREATED,
        "headRefOid": HEAD,
        "labels": [{"name": f"review:{service}"}],
        "statusCheckRollup": checks or [],
    }


def review(*, service: str, state: str = "APPROVED") -> dict:
    logins = {
        "sourcery": "sourcery-ai[bot]",
        "codeant": "codeant-ai[bot]",
    }
    return {
        "id": 1,
        "commit_id": HEAD,
        "state": state,
        "submitted_at": REVIEWED,
        "user": {"login": logins[service], "type": "Bot"},
    }


def test_external_review_paths_preserve_authenticated_provider_evidence(monkeypatch):
    coderabbit = review_pr(
        "coderabbit", checks=[{"context": "CodeRabbit", "state": "SUCCESS"}]
    )
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    monkeypatch.setattr(
        merge_pr,
        "pull_review_checks",
        lambda _head: [{
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "completedAt": REVIEWED,
            "head_sha": HEAD,
            "app": {"slug": "coderabbitai"},
        }],
    )
    assert merge_pr.exact_head_review(coderabbit, 10, "coderabbit") is False

    sourcery = review_pr("sourcery")
    sourcery_approval = review(service="sourcery")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [sourcery_approval])
    assert merge_pr.exact_head_review(sourcery, 10, "sourcery") is True

    codeant = review_pr("codeant")
    codeant_approval = review(service="codeant")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [codeant_approval])
    assert merge_pr.exact_head_review(codeant, 10, "codeant") is True

    human = {**codeant_approval, "user": {"login": "human", "type": "User"}}
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [human])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [])
    assert merge_pr.exact_head_review(codeant, 10, "codeant") is False

    changes = review(service="sourcery", state="CHANGES_REQUESTED")
    monkeypatch.setattr(
        merge_pr, "pull_reviews", lambda _number: [sourcery_approval, changes]
    )
    assert merge_pr.exact_head_review(sourcery, 10, "sourcery") is False


def test_rate_limited_success_check_does_not_satisfy_coderabbit(monkeypatch):
    pr = review_pr(
        "coderabbit", checks=[{"context": "CodeRabbit", "state": "SUCCESS"}]
    )
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    monkeypatch.setattr(
        merge_pr,
        "pull_review_checks",
        lambda _head: [{
            "name": "CodeRabbit",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "completedAt": REVIEWED,
            "head_sha": HEAD,
            "app": {"slug": "coderabbitai"},
            "output": {"summary": "Review rate limited"},
        }],
    )
    assert merge_pr.exact_head_review(pr, 10, "coderabbit") is False


def test_provider_approval_with_unavailability_text_does_not_satisfy_review(
    monkeypatch,
):
    pr = review_pr("sourcery")
    approval = review(service="sourcery")
    approval["body"] = "Review skipped because quota exhausted"
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [approval])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [])
    assert merge_pr.exact_head_review(pr, 10, "sourcery") is False


def test_reassignment_cannot_reuse_pre_assignment_approval(monkeypatch):
    pr = review_pr("sourcery")
    approval = review(service="sourcery")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [approval])
    monkeypatch.setattr(
        merge_pr,
        "pull_events",
        lambda _number: [{
            "event": "labeled",
            "label": {"name": "review:sourcery"},
            "created_at": "2026-09-01T10:10:00Z",
        }],
    )

    assert merge_pr.exact_head_review(pr, 10, "sourcery") is False


def test_newer_trusted_unavailable_evidence_overrides_old_approval(monkeypatch):
    pr = review_pr("sourcery")
    approval = review(service="sourcery")
    unavailable = {
        "body": "Review skipped because quota exhausted",
        "created_at": "2026-09-01T10:06:00Z",
        "user": {"login": "sourcery-ai[bot]", "type": "Bot"},
    }
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [approval])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [unavailable])

    assert merge_pr.exact_head_review(pr, 10, "sourcery") is False
