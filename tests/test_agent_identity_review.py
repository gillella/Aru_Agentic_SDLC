from __future__ import annotations

import pytest

import merge_pr
import review_authority
from test_merge_gate import HEAD, approval, base_pr

# The writer is the "writer" login and claimed issue #7 as agent `codex`.
CLAIM = {"number": 7, "labels": [{"name": "agent:codex"}, {"name": "status:in-review"}], "state": "OPEN", "body": ""}


@pytest.fixture
def claimed(monkeypatch):
    monkeypatch.setattr(merge_pr, "issue", lambda number: CLAIM)


def same_login(body: str, **kw):
    return approval(user={"login": "writer"}, body=body, **kw)


@pytest.mark.parametrize("body,approved", [
    # A different agent identity under the author's login is another party.
    ("Looks correct.\n\nReviewed-by-agent: opus-5", True),
    ("Reviewed-by-agent: Grok-4.7\nno findings", True),
    # The agent that claimed the issue cannot approve its own change.
    ("Reviewed-by-agent: codex", False),
    ("Reviewed-by-agent: CODEX", False),
    # No trailer, or a trailer not on its own line, is still self-approval.
    ("LGTM", False),
    ("", False),
    ("see Reviewed-by-agent: opus-5 above", False),
    # A malformed identity never passes.
    ("Reviewed-by-agent: ", False),
    ("Reviewed-by-agent: opus 5", False),
])
def test_same_login_counts_only_with_a_distinct_agent_trailer(claimed, body, approved):
    assert merge_pr.approved_at_head(base_pr(), [same_login(body)]) is approved


def test_trailer_on_an_earlier_commit_does_not_carry(claimed):
    assert merge_pr.approved_at_head(base_pr(), [same_login("Reviewed-by-agent: opus-5", commit_id="c" * 40)]) is False


def test_a_later_changes_requested_by_the_same_agent_supersedes(claimed):
    reviews = [
        same_login("Reviewed-by-agent: opus-5"),
        same_login("Reviewed-by-agent: opus-5", id=2, state="CHANGES_REQUESTED"),
    ]
    assert merge_pr.approved_at_head(base_pr(), reviews) is False


def test_other_account_approval_never_reads_the_issue(monkeypatch):
    def boom(number):
        raise AssertionError("issue lookup must not run for a cross-account approval")
    monkeypatch.setattr(merge_pr, "issue", boom)
    assert merge_pr.approved_at_head(base_pr(), [approval()]) is True


def test_untrailed_self_approval_never_reads_the_issue(monkeypatch):
    def boom(number):
        raise AssertionError("issue lookup must not run without a trailer")
    monkeypatch.setattr(merge_pr, "issue", boom)
    assert merge_pr.approved_at_head(base_pr(), [same_login("LGTM")]) is False


def test_other_party_helper_directly():
    assert review_authority.other_party("reviewer", "writer", {"body": ""}, set()) is True
    assert review_authority.other_party("writer", "writer", {"body": "Reviewed-by-agent: opus-5"}, {"codex"}) is True
    assert review_authority.other_party("writer", "writer", {"body": "Reviewed-by-agent: codex"}, {"codex"}) is False
    assert review_authority.other_party("Writer", "writer", {"body": None}, {"codex"}) is False
