from __future__ import annotations

import common
import fetch_pr_feedback


def test_review_thread_graphql_uses_repository_authority(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    calls = []

    def fake_gh_json(args, *, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(fetch_pr_feedback, "gh_json", fake_gh_json)

    assert fetch_pr_feedback.fetch_feedback(17) == []
    assert calls[0][1] == common.REPOSITORY_AUTH
