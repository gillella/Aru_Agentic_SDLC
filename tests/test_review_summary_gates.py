"""Summary-only findings must reach author routing and every merge review gate."""
import pytest

import fetch_next_work
import fetch_pr_feedback
import merge_pr
from test_governed_merge import install_gate, ready_pr
from test_review_summary_findings import HEAD, install, page, review


def test_author_gets_summary_feedback_before_ci_or_merge(monkeypatch):
    install(monkeypatch, [page([review()])])
    monkeypatch.setattr(fetch_next_work, 'gh_json', lambda *_a, **_k: [])  # No inline comments.
    monkeypatch.setattr(fetch_next_work, 'authored_prs', lambda _: [ready_pr()])
    monkeypatch.setattr(fetch_next_work, 'ci_verdict', lambda _: {
        'head': HEAD, 'state': 'success', 'checks': ['aru-governed-pr']})
    monkeypatch.setattr(fetch_next_work, 'evaluate', lambda *_a: {})
    monkeypatch.setattr(fetch_next_work, 'ready_issues', lambda: pytest.fail('author must remediate'))
    result = fetch_next_work.select('codex-astra')
    assert result['type'] == 'feedback'
    assert result['items'][0]['review_id'] == 1


@pytest.mark.parametrize('phase', ['admission', 'final-revalidation', 'close-out'])
def test_approved_pr_with_summary_findings_is_refused_at_every_review_gate(monkeypatch, phase):
    install_gate(monkeypatch)
    install(monkeypatch, [page([review()])])
    monkeypatch.setattr(merge_pr, 'fetch_feedback', fetch_pr_feedback.fetch_feedback)
    monkeypatch.setattr(merge_pr, 'finalization_verdict', lambda _: {
        'head': HEAD, 'state': 'success', 'checks': ['aru-governed-pr']})
    with pytest.raises(merge_pr.GateRefusal) as caught:
        if phase == 'admission':
            merge_pr.evaluate(10, HEAD)
        elif phase == 'final-revalidation':
            merge_pr.revalidate_review(ready_pr(), 10)
        else:
            merge_pr.require_ci_review(ready_pr(), 10, HEAD, finalizing=True)
    assert caught.value.gate == 'unresolved-findings'


def test_merge_dry_run_refuses_summary_findings_before_authorization(monkeypatch):
    install_gate(monkeypatch)
    install(monkeypatch, [page([review()])])
    monkeypatch.setattr(merge_pr, 'fetch_feedback', fetch_pr_feedback.fetch_feedback)
    monkeypatch.setattr(merge_pr.merge_authority, 'post', lambda *_a: pytest.fail('authorized a defect'))
    monkeypatch.setattr(merge_pr, 'run', lambda *_a: pytest.fail('submitted a defective merge'))
    with pytest.raises(merge_pr.GateRefusal, match='unresolved'):
        merge_pr.merge(10, HEAD, dry_run=True)
