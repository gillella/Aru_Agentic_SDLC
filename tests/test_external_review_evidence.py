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


@pytest.mark.parametrize("service,case,expected", [
    ("coderabbit", "green-check", False), ("coderabbit", "rate-limited-check", False),
    ("sourcery", "approval", True), ("codeant", "approval", True),
    ("codeant", "human", False), ("sourcery", "changes", False),
    ("sourcery", "skipped", False), ("sourcery", "reassigned", False),
    ("sourcery", "newer-unavailable", False),
])
def test_external_review_requires_current_trusted_substantive_evidence(monkeypatch, service, case, expected):
    pr = review_pr(service)
    reviews, checks, comments, events = [], [], [], []
    if service == "coderabbit":
        pr["statusCheckRollup"] = [{"context": "CodeRabbit", "state": "SUCCESS"}]
        checks = [{"name": "CodeRabbit", "status": "COMPLETED", "conclusion": "SUCCESS",
                   "completedAt": REVIEWED, "head_sha": HEAD, "app": {"slug": "coderabbitai"}}]
        if case == "rate-limited-check":
            checks[0]["output"] = {"summary": "Review rate limited"}
    else:
        reviews = [review(service=service)]
        if case == "human":
            reviews[0]["user"] = {"login": "human", "type": "User"}
        elif case == "changes":
            reviews.append(review(service=service, state="CHANGES_REQUESTED"))
        elif case == "skipped":
            reviews[0]["body"] = "Review skipped because quota exhausted"
        elif case == "reassigned":
            events = [{"event": "labeled", "label": {"name": "review:sourcery"},
                       "created_at": "2026-09-01T10:10:00Z"}]
        elif case == "newer-unavailable":
            comments = [{"body": "Review skipped because quota exhausted",
                         "created_at": "2026-09-01T10:06:00Z", "user": reviews[0]["user"]}]
    for name, records in (("pull_reviews", reviews), ("pull_review_checks", checks),
                          ("pull_comments", comments), ("pull_events", events)):
        monkeypatch.setattr(merge_pr, name, lambda _n, records=records: records)
    assert merge_pr.exact_head_review(pr, 10, service) is expected


SOURCERY_NOTICE = ("Hi @gillella! \U0001F44B\n\nYour private repo does not have access to Sourcery.\n\n"
                   "Please [upgrade](https://app.sourcery.ai/login) to continue using Sourcery \u2728")


def test_sourcery_private_repo_notice_is_recognized_as_unavailability():
    import common
    assert common.review_evidence_unavailable({"body": SOURCERY_NOTICE})
    assert not common.review_evidence_unavailable({"body": "this helper does not have access to the config"})


def test_status_evidence_feeds_external_state_after_assignment():
    from datetime import datetime, timezone
    import review_evidence as ev
    def status(state, description, at=REVIEWED, creator={"login": "coderabbitai[bot]", "type": "Bot"}):
        return {"context": "CodeRabbit", "state": state, "description": description, "creator": creator, "created_at": at}
    base = dict(reviews=[], comments=[], checks=[], head=HEAD, since=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
    assert ev.external_state("coderabbit", statuses=[status("pending", "Review in progress")], **base) == ev.PENDING
    assert ev.external_state("coderabbit", statuses=[status("success", "Review completed")], **base) == ev.PENDING
    assert ev.external_state("coderabbit", statuses=[status("success", "Review skipped: excluded by label configuration")], **base) == ev.UNAVAILABLE
    ignored = [status("error", "Review failed", "2026-09-01T09:00:00Z"),  # before assignment
               status("error", "Review failed", creator={"login": "human", "type": "User"})]  # spoofed
    assert ev.external_state("coderabbit", statuses=ignored, **base) == ev.PENDING
