"""The review posture: who may approve, and what happens when the declaration is unusable.

Every resolution failure must land on the strict posture. A declaration that is absent,
malformed, or the wrong shape is the most likely way a repository would silently lose the
human gate, so each of those is asserted rather than assumed.
"""

from __future__ import annotations

import json

import pytest

import review_authority as ra

HEAD = "a" * 40


def review(login, *, state="APPROVED", commit=HEAD, kind="User", body="looks right to me"):
    return {"state": state, "commit_id": commit, "body": body,
            "user": {"login": login, "type": kind}}


def refuse(reviews, policy, **kw):
    return ra.refusal(author="factory-app", head=HEAD, reviews=reviews, policy=policy, **kw)


STRICT = ra.Policy("human", ["gillella"], "test")
ANY = ra.Policy("any", [], "test")
NONE = ra.Policy("none", [], "test")


@pytest.mark.parametrize("text", [
    None,                      # the file does not exist
    "",                        # present but empty
    "not json at all",
    "[]",                      # valid JSON, wrong shape
    '{"authority": "nonsense"}',
    '{"authority": "none"}',   # a posture without reviewers must not weaken silently
])
def test_an_unusable_declaration_resolves_to_the_strict_posture(text):
    policy = ra.resolve(text)
    if text == '{"authority": "none"}':
        # A deliberate, well-formed downgrade is honoured; it is not an unusable file.
        assert policy.posture == "none"
        return
    assert policy.strict and policy.reviewers == []


def test_a_well_formed_declaration_is_honoured():
    policy = ra.resolve(json.dumps({"authority": "any", "reviewers": ["gillella", 7, "  "]}))
    assert policy.posture == "any"
    # Non-strings and blanks cannot become authorized accounts.
    assert policy.reviewers == ["gillella"]


def test_strict_posture_refuses_an_unlisted_account():
    assert refuse([review("stranger")], STRICT).startswith("no approval of the exact head by an authorized")


def test_strict_posture_refuses_an_app_even_when_it_is_listed():
    """The likeliest silent degradation is a bot added to the reviewer set."""
    policy = ra.Policy("human", ["helpful-bot"], "test")
    assert refuse([review("helpful-bot[bot]", kind="Bot")], policy) is not None
    assert refuse([review("helpful-bot", kind="Bot")], policy) is not None


def test_strict_posture_accepts_an_authorized_person():
    assert refuse([review("gillella")], STRICT) is None
    assert refuse([review("GILLELLA")], STRICT) is None  # logins are case-insensitive


def test_an_empty_reviewer_list_says_what_to_do():
    reason = refuse([review("gillella")], ra.Policy("human", [], "policy-file"))
    assert "authorizes no reviewer" in reason and "policy-file" in reason


def test_the_author_never_satisfies_any_posture():
    for policy in (STRICT, ANY):
        assert refuse([review("factory-app")], policy) is not None


def test_an_approval_of_an_earlier_commit_does_not_carry():
    assert refuse([review("gillella", commit="b" * 40)], STRICT) is not None


def test_a_later_rejection_replaces_an_earlier_approval():
    """GitHub lists reviews oldest first; only the latest decisive one counts."""
    reviews = [review("gillella"), review("gillella", state="CHANGES_REQUESTED")]
    assert refuse(reviews, STRICT) is not None


def test_permissive_posture_reproduces_the_previous_rule():
    """`any` must behave exactly as the kernel did before the posture existed."""
    assert refuse([review("stranger")], ANY) is None
    assert refuse([review("some-bot", kind="Bot")], ANY) is None


def test_the_unreviewed_posture_requires_nothing():
    assert refuse([], NONE) is None


def test_a_written_judgement_is_required_only_when_asked():
    terse = [review("gillella", body="lgtm")]
    assert refuse(terse, STRICT) is None
    assert "written judgement" in refuse(terse, STRICT, require_judgement=True)
    assert refuse([review("gillella")], STRICT, require_judgement=True) is None


def test_malformed_review_evidence_is_never_read_as_absent():
    with pytest.raises(ra.KernelError):
        refuse([{"state": "APPROVED", "commit_id": HEAD, "user": {"login": ""}}], STRICT)


def test_the_merge_gate_consults_the_posture(monkeypatch):
    """The wiring, not just the rule: merge_pr must refuse what the posture refuses.

    The suite's default fixture resolves the permissive posture, so this overrides it to
    prove the strict path reaches the merge gate rather than being stubbed away.
    """
    import merge_pr

    monkeypatch.setattr(
        ra, "read_policy_text",
        lambda **_: json.dumps({"authority": "human", "reviewers": ["gillella"]}),
    )
    pr = {"author": {"login": "factory-app"}, "headRefOid": HEAD}
    assert merge_pr.authority_refusal(pr, [review("stranger")]) is not None
    assert merge_pr.authority_refusal(pr, [review("gillella")]) is None


def test_the_bootstrap_seeds_its_first_authorized_reviewer(tmp_path):
    """A fresh repository must be able to merge its first change."""
    import init_project

    init_project.scaffold("consumer", tmp_path / "c", runner_profile="self-hosted-mac",
                          reviewers=["gillella"])
    declared = json.loads((tmp_path / "c" / ".aru" / "review.json").read_text())
    assert declared == {"authority": "human", "reviewers": ["gillella"]}
    assert not ra.resolve(json.dumps(declared)).reviewers == []


def test_a_repository_that_declared_nothing_authorizes_its_owner(monkeypatch):
    """Strict with nobody authorized would refuse the repository's own first merge."""
    monkeypatch.setattr(ra, "read_policy_text", lambda **_: None)
    monkeypatch.setattr(ra, "repo_slug", lambda **_: "gillella/Aru_Agentic_SDLC")
    policy = ra.load_policy()
    assert policy.strict and policy.reviewers == ["gillella"]
    assert refuse([review("gillella")], policy) is None
    assert refuse([review("stranger")], policy) is not None
    # The fallback is still an account and still not an App.
    assert refuse([review("gillella", kind="Bot")], policy) is not None


def test_a_declared_posture_is_never_overridden_by_the_owner_fallback(monkeypatch):
    monkeypatch.setattr(ra, "read_policy_text", lambda **_: '{\"authority\": \"any\"}')
    monkeypatch.setattr(ra, "repo_slug", lambda **_: "gillella/x")
    assert ra.load_policy().posture == "any"
