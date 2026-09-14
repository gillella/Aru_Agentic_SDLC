from __future__ import annotations

import pytest

import common
import fetch_pr_feedback


def response(*, nodes=None, has_next=False, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [] if nodes is None else nodes,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    }
                }
            }
        }
    }


def thread(*, resolved=False, outdated=False, comments=None):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/app.py",
        "line": 12,
        "comments": {
            "nodes": comments
            if comments is not None
            else [
                {
                    "author": {"login": "reviewer"},
                    "body": "Fix this",
                    "url": "https://example.test/review/1",
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }


def test_feedback_uses_repository_authority_and_returns_latest_unresolved(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda args, *, auth=None: (
            calls.append((args, auth))
            or response(
                nodes=[
                    thread(resolved=True),
                    thread(outdated=True),
                    thread(),
                ]
            )
        ),
    )

    assert fetch_pr_feedback.fetch_threads(17) == [
        {
            "path": "src/app.py",
            "line": 12,
            "author": "reviewer",
            "body": "Fix this",
            "url": "https://example.test/review/1",
        }
    ] * 2
    assert calls[0][1] == common.REPOSITORY_AUTH


def test_feedback_paginates_all_threads(monkeypatch):
    pages = iter(
        [
            response(nodes=[thread(resolved=True)], has_next=True, cursor="next"),
            response(nodes=[thread()]),
        ]
    )
    calls = []
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda args, **_kwargs: calls.append(args) or next(pages),
    )

    assert len(fetch_pr_feedback.fetch_threads(17)) == 1
    assert "after=next" in calls[1]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": {}}, "incomplete"),
        (response(nodes=[None]), "malformed"),
        (
            response(
                nodes=[
                    {
                        **thread(),
                        "comments": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True},
                        },
                    }
                ]
            ),
            "truncated",
        ),
        (response(nodes=[thread(comments=[])]), "no comment"),
        (response(nodes=[], has_next=True, cursor=None), "pagination"),
    ],
)
def test_feedback_fails_closed_on_incomplete_evidence(monkeypatch, payload, message):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(fetch_pr_feedback, "gh_json", lambda *_args, **_kwargs: payload)

    with pytest.raises(common.KernelError, match=message):
        fetch_pr_feedback.fetch_threads(17)
