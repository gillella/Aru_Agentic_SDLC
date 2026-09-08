from __future__ import annotations

import subprocess

import pytest

import merge_state


@pytest.fixture
def semantic_issue(monkeypatch):
    record = {
        "state": "OPEN",
        "labels": [{"name": "status:in-review"}, {"name": "agent:writer"}],
        "body": "## Acceptance Criteria\n- [x] Reject unauthorized writes\n\n"
                "## Scope\ntouches: src/app.py\n",
    }
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    monkeypatch.setattr(merge_state, "issue", lambda _number: dict(record))
    return record


def test_same_count_acceptance_semantic_edit_changes_evidence(semantic_issue):
    before = merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] = semantic_issue["body"].replace("Reject", "Permit")
    after = merge_state.issue_gate([7], ["src/app.py"])
    assert before[0]["criteria"] == after[0]["criteria"] == 1
    assert before != after
    assert before[0]["acceptance"] == [{"done": True, "text": "Reject unauthorized writes"}]
    assert after[0]["acceptance"] == [{"done": True, "text": "Permit unauthorized writes"}]


def test_acceptance_completion_drift_fails_closed(semantic_issue):
    merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] = semantic_issue["body"].replace("[x]", "[ ]")
    with pytest.raises(merge_state.KernelError, match="incomplete Acceptance Criteria"):
        merge_state.issue_gate([7], ["src/app.py"])


def test_issue_evidence_ignores_prose_outside_acceptance(semantic_issue):
    before = merge_state.issue_gate([7], ["src/app.py"])
    semantic_issue["body"] += "\n## Evidence\nAdditional successful verification.\n"
    assert merge_state.issue_gate([7], ["src/app.py"]) == before


# The governed-merge file is near the 800-line limit. Share its isolated records
# here for additional review-state controls without introducing another test file.
import json

import merge_pr
from test_governed_merge import (
    HEAD, REVIEW_FORMS, change_review, install_review_boundary, review_world,
)


@pytest.mark.parametrize('form', REVIEW_FORMS)
@pytest.mark.parametrize('decision', [None, 'REVIEW_REQUIRED', 'APPROVED'])
def test_stable_review_evidence_order_and_nullable_decision(monkeypatch, form, decision):
    world = review_world(form)
    world['pr']['reviewDecision'] = decision
    events, commands = install_review_boundary(monkeypatch, world, lambda _w: None)
    with pytest.raises(RuntimeError, match='command spy reached'):
        merge_pr.merge(10, HEAD)
    review_reads = ['reviews'] if form.startswith('claude') else [
        'reviews', 'comments', 'assignments', 'checks']
    assert events == (['pr', 'queue', 'issue', 'ci', 'threads'] + review_reads) * 2 + (
        ['pr', 'issue', 'queue'] + review_reads + ['threads'])
    assert commands == [['gh', 'pr', 'merge', '10', '--merge', '--delete-branch',
                         '--match-head-commit', HEAD]]


@pytest.mark.parametrize('tier', [0, 1, 2, 3])
@pytest.mark.parametrize('mutation', ['stable', 'new-thread', 'reopened-thread'])
def test_final_threads_preserve_risk_tier_semantics(monkeypatch, tier, mutation):
    world = review_world('coderabbit/check')
    if mutation == 'reopened-thread':
        change_review(world, 'new-thread')
        world['threads'][0]['isResolved'] = True
    if tier < 2:
        world['pr']['labels'] = []
        world['checks'] = []
    events, commands = install_review_boundary(
        monkeypatch, world, lambda w: change_review(w, mutation), boundary='queue', tier=tier)
    if mutation == 'stable':
        with pytest.raises(RuntimeError, match='command spy reached'):
            merge_pr.merge(10, HEAD)
        assert commands and events[-1] == 'threads'
    else:
        with pytest.raises(merge_pr.KernelError, match='unresolved review thread'):
            merge_pr.merge(10, HEAD)
        assert commands == []
    assert events.count('reviews') == (0 if tier < 2 else 3)


@pytest.mark.parametrize('form,mutation', [
    ('coderabbit/approval', 'dismissed'), ('coderabbit/check', 'revoked'),
    ('coderabbit/check', 'new-thread'), ('claude-code/attestation', 'coding-body'),
])
def test_earlier_review_withdrawal_control(monkeypatch, form, mutation):
    world = review_world(form)
    events, commands = install_review_boundary(
        monkeypatch, world, lambda w: change_review(w, mutation), boundary='ci')
    with pytest.raises(merge_pr.KernelError):
        merge_pr.merge(10, HEAD)
    assert commands == []
    assert events.count('pr') == events.count('ci') == 2


@pytest.mark.parametrize('mutation', ['unavailable-comment', 'ambiguous-summary',
                                    'malformed-review', 'missing-authority', 'external-actor'])
def test_additional_final_external_evidence_refusals(monkeypatch, mutation):
    world = review_world('coderabbit/check')

    def mutate(w):
        if mutation == 'unavailable-comment':
            w['comments'].append(dict(id=9, user=w['reviews'][0]['user'],
                                      body='rate limit exceeded', updated_at='2026-09-05T09:00:00Z'))
        elif mutation == 'ambiguous-summary':
            w['checks'] *= 2
        elif mutation == 'malformed-review':
            w['reviews'].append(None)
        elif mutation == 'missing-authority':
            w['pr']['labels'].clear()
        else:
            w['pr']['labels'].append({'name': 'reviewer-actor:intruder'})

    _, commands = install_review_boundary(monkeypatch, world, mutate)
    with pytest.raises(merge_pr.KernelError):
        merge_pr.merge(10, HEAD)
    assert commands == []


@pytest.mark.parametrize('family', merge_pr.CODING_REVIEWERS)
@pytest.mark.parametrize('mutation', ['stable', 'submitted-by', 'review-actor',
                                    'issues', 'family', 'request-changes'])
def test_final_coding_binding_and_attestation(monkeypatch, family, mutation):
    world = review_world('claude-code/attestation')
    world['pr']['labels'][0]['name'] = 'review:' + family
    review = world['reviews'][0]
    payload = json.loads(merge_pr.CODING_REVIEW_MARKER_RE.findall(review['body'])[0])
    payload['family'] = family
    review['body'] = '<!-- aru-coding-review:v1 ' + json.dumps(payload) + ' -->'

    def mutate(w):
        if mutation == 'submitted-by':
            payload['submitted_by'] = 'wrong-actor'
        elif mutation == 'review-actor':
            review['user']['login'] = 'wrong-actor'
        elif mutation in {'issues', 'family'}:
            payload[mutation] = [999] if mutation == 'issues' else 'wrong-family'
        elif mutation == 'request-changes':
            payload['verdict'] = 'REQUEST_CHANGES'
            review['state'] = 'CHANGES_REQUESTED'
        review['body'] = '<!-- aru-coding-review:v1 ' + json.dumps(payload) + ' -->'

    _, commands = install_review_boundary(monkeypatch, world, mutate, boundary='queue')
    if mutation == 'stable':
        with pytest.raises(RuntimeError, match='command spy reached'):
            merge_pr.merge(10, HEAD)
        assert len(commands) == 1
    else:
        with pytest.raises(merge_pr.KernelError) as exc:
            merge_pr.merge(10, HEAD)
        assert commands == []
        if mutation == 'request-changes':
            assert str(exc.value) == family + ' exact-head authoritative review requested changes'
