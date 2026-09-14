"""Exercise public feedback reads using GitHub-shaped evidence, never live GitHub."""
from copy import deepcopy

import pytest

import common
import fetch_pr_feedback as feedback

HEAD = 'a' * 40
OLD = 'b' * 40


def review(number=1, body='1. **[P1] Refuse unavailable weights.**', *,
           actor='reviewer', head=OLD, state='COMMENTED', stamp='2026-09-14T10:00:00Z'):
    return dict(databaseId=number, author={'login': actor}, commit={'oid': head}, state=state,
                body=body, url=f'https://github.com/owner/repo/pull/165#pullrequestreview-{number}',
                submittedAt=stamp, lastEditedAt=None)


def resolution(**kwargs):
    return review(2, 'Resolves review: 1\nVerified refusal and finite plate regressions at this head.',
                  head=HEAD, stamp='2026-09-14T11:00:00Z', **kwargs)


def page(reviews, *, head=HEAD, author='writer', more=False, cursor=None):
    return {'data': {'repository': {'pullRequest': {
        'headRefOid': head, 'author': {'login': author},
        'reviews': {'nodes': reviews, 'pageInfo': {'hasNextPage': more, 'endCursor': cursor}},
    }}}}


def install(monkeypatch, pages):
    """No inline comments: an APPROVED summary-only PR must still return feedback."""
    monkeypatch.setattr(feedback, 'repo_slug', lambda: 'owner/repo')
    calls = []
    responses = iter(pages)

    def read(args, *, auth):
        assert auth == common.REPOSITORY_AUTH
        calls.append(args)
        if 'reviewThreads(' in str(args):
            return {'data': {'repository': {'pullRequest': {'reviewThreads': {
                'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None},
            }}}}}
        return deepcopy(next(responses))

    monkeypatch.setattr(feedback, 'gh_json', read)
    return calls


@pytest.mark.parametrize('body', [
    '1. **[P1] Connect the workout screen.**', '[P0] Data loss',
    '### P1: Missing validation', 'P0: Cross-tenant access',
    '**P1: Weight is unavailable**', '![P1 Badge](https://img.shields.io/badge/P1-orange) Fix this',
])
@pytest.mark.parametrize('state', ['COMMENTED', 'APPROVED', 'DISMISSED'])
def test_summary_findings_survive_approval_dismissal_and_new_head(monkeypatch, body, state):
    original = review(body=body, state=state)
    approved = review(9, 'Looks good.', actor='human', head=HEAD, state='APPROVED')
    install(monkeypatch, [page([original, approved])])
    findings = feedback.fetch_feedback(165)
    assert len(findings) == 1
    assert findings[0]['review_id'] == 1
    assert findings[0]['body'] == body
    assert findings[0]['url'] == original['url']


@pytest.mark.parametrize('change', [
    'unrelated-approval', 'writer', 'another-reviewer', 'stale-head', 'earlier',
    'pending', 'dismissed', 'requests-changes', 'wrong-id', 'no-evidence', 'quoted', 'new-finding',
])
def test_resolution_must_be_explicit_independent_current_and_evidenced(monkeypatch, change):
    resolved = resolution()
    if change == 'unrelated-approval':
        resolved.update(body='Approved', state='APPROVED')
    elif change in {'writer', 'another-reviewer'}:
        resolved['author']['login'] = change
    elif change == 'stale-head':
        resolved['commit']['oid'] = OLD
    elif change == 'earlier':
        resolved['submittedAt'] = '2026-09-14T09:00:00Z'
    elif change in {'pending', 'dismissed', 'requests-changes'}:
        resolved['state'] = {'pending': 'PENDING', 'dismissed': 'DISMISSED',
                             'requests-changes': 'CHANGES_REQUESTED'}[change]
    elif change == 'wrong-id':
        resolved['body'] = resolved['body'].replace(': 1', ': 999')
    elif change == 'no-evidence':
        resolved['body'] = 'Resolves review: 1'
    elif change == 'quoted':
        resolved['body'] = 'Example only:\n```\n' + resolved['body'] + '\n```'
    elif change == 'new-finding':
        resolved['body'] += '\n[P1] Another failure remains.'
    install(monkeypatch, [page([review(), resolved])])
    assert 1 in [item['review_id'] for item in feedback.fetch_feedback(165)]


@pytest.mark.parametrize('state', ['COMMENTED', 'APPROVED'])
def test_original_reviewer_resolution_clears_only_named_review(monkeypatch, state):
    original = review(actor='app/checker')
    resolved = resolution(actor='CHECKER[bot]', state=state)
    other = review(3, '[P0] Another defect', actor='other-reviewer')
    install(monkeypatch, [page([original, other, resolved])])
    assert [item['review_id'] for item in feedback.fetch_feedback(165)] == [3]


def test_author_cannot_clear_own_summary(monkeypatch):
    install(monkeypatch, [page([review(actor='writer'), resolution(actor='writer')])])
    assert len(feedback.fetch_feedback(165)) == 1


def test_reads_all_review_pages_and_keeps_later_page_finding(monkeypatch):
    calls = install(monkeypatch, [page([review(body='No P1 findings.')], more=True, cursor='next'),
                                 page([review(101)])])
    assert [item['review_id'] for item in feedback.fetch_feedback(165)] == [101]
    assert any('after=next' in call for call in calls)


@pytest.mark.parametrize('change', [
    'errors', 'missing-reviews', 'invalid-node', 'missing-body', 'missing-author', 'bad-id',
    'bad-state', 'bad-commit', 'bad-time', 'bad-head', 'missing-pagination', 'missing-cursor',
    'duplicate', 'head-race', 'author-race', 'repeated-cursor', 'missing-edit-time', 'bad-edit-time',
])
def test_incomplete_review_evidence_never_becomes_no_feedback(monkeypatch, change):
    payload = page([review()])
    pr = payload['data']['repository']['pullRequest']
    node = pr['reviews']['nodes'][0]
    pages = [payload]
    if change == 'errors':
        payload['errors'] = [{'message': 'Unavailable'}]
    elif change == 'missing-reviews':
        del pr['reviews']
    elif change == 'invalid-node':
        pr['reviews']['nodes'] = [None]
    elif change == 'missing-body':
        del node['body']
    elif change == 'missing-author':
        node['author'] = None
    elif change == 'bad-id':
        node['databaseId'] = True
    elif change == 'bad-state':
        node['state'] = 'UNKNOWN'
    elif change == 'bad-commit':
        node['commit'] = None
    elif change == 'missing-edit-time':
        del node['lastEditedAt']
    elif change == 'bad-edit-time':
        node['lastEditedAt'] = 'yesterday'
    elif change == 'bad-time':
        node['submittedAt'] = 'yesterday'
    elif change == 'bad-head':
        pr['headRefOid'] = None
    elif change == 'missing-pagination':
        pr['reviews'].pop('pageInfo')
    elif change == 'duplicate':
        pr['reviews']['nodes'].append(review())
    else:
        pr['reviews']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'next'}
        if change == 'missing-cursor':
            pr['reviews']['pageInfo']['endCursor'] = None
        else:
            pages.append(page([review(2)], head=OLD if change == 'head-race' else HEAD,
                              author='changed' if change == 'author-race' else 'writer',
                              more=change == 'repeated-cursor', cursor='next'))
    install(monkeypatch, pages)
    with pytest.raises(common.KernelError):
        feedback.fetch_feedback(165)


def test_editing_a_resolved_summary_requires_new_reviewer_confirmation(monkeypatch):
    original = review()
    original['lastEditedAt'] = '2026-09-14T12:00:00Z'
    install(monkeypatch, [page([original, resolution()])])
    assert [item['review_id'] for item in feedback.fetch_feedback(165)] == [1]
