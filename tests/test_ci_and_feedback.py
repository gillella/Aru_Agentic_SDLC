from __future__ import annotations

import pytest

import check_ci
import fetch_pr_feedback


def test_ci_requires_at_least_one_non_review_check(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "statusCheckRollup": [
                {"context": "CodeRabbit", "state": "SUCCESS"},
            ],
        },
    )
    assert check_ci.ci_verdict(3)["state"] == "pending"


def test_ci_fails_when_any_current_check_fails(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "statusCheckRollup": [
                {"name": "Verify", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"context": "CodeRabbit", "state": "SUCCESS"},
            ],
        },
    )
    result = check_ci.ci_verdict(3)
    assert result["state"] == "failure"
    assert result["checks"] == [{"name": "Verify", "state": "failure"}]


def test_feedback_collects_unresolved_threads(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda _argv, *, auth: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "a.py",
                                    "line": 7,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "author": {"login": "reviewer"},
                                                "body": "fix this",
                                                "url": "https://example/thread",
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        if auth == fetch_pr_feedback.REPOSITORY_AUTH
        else pytest.fail("expected repository authority"),
    )
    feedback = fetch_pr_feedback.fetch_feedback(9)
    assert feedback[0]["path"] == "a.py"
    assert feedback[0]["author"] == "reviewer"


def test_feedback_fails_closed_on_comment_truncation(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda _argv, *, auth: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "nodes": [{}],
                                        "pageInfo": {"hasNextPage": True},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        }
        if auth == fetch_pr_feedback.REPOSITORY_AUTH
        else pytest.fail("expected repository authority"),
    )
    with pytest.raises(fetch_pr_feedback.KernelError, match="truncated"):
        fetch_pr_feedback.fetch_feedback(9)
