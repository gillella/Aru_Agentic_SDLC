#!/usr/bin/env python3
"""Installed restricted review entry point; never run from a coding checkout."""
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path('/private/var/db/aru-trusted-review')


def bootstrap():
    if os.geteuid() != 0 or not sys.flags.isolated or Path(__file__).absolute() != ROOT / 'request.py':
        raise ValueError('wrong-execution-context')
    for path in reversed([ROOT, *ROOT.parents]):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unprotected-installation')
    # Validate import targets before loading any package module. Root remains trusted.
    for name in ('request.py', 'security.py', 'app.py', 'model.py', 'validation.py'):
        info = (ROOT / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unprotected-installation')
    sys.path.insert(0, str(ROOT))


def run(argv):
    from security import Refusal, enabled, installation, parse_request, protected_chain
    pr, head, check = parse_request(argv)
    config, raw, user = installation()
    if not check:
        try:
            enabled(ROOT, raw)
        except (OSError, ValueError, Refusal):
            raise Refusal('reviews-disabled') from None
    # Caller tokens, imports, CLI settings and executable search paths are discarded.
    os.environ.clear()
    os.umask(0o077)
    protected_chain(ROOT / 'runtime')
    descriptor = os.open(ROOT / 'runtime/request.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    connection = None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise Refusal('unprotected-lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('review-busy') from None
        from app import APP_SLUG, Connection
        connection = Connection(ROOT, config, pr)
        connection.authenticate()
        record = connection.pr_record(head)
        if check:
            return {'status': 'preflight-passed', 'pr': pr, 'head': head,
                    'model_authentication': 'unverified', 'review_published': False}
        # GitHub itself supplies deduplication evidence; no private review ledger.
        reviews = connection.pages('reviews')
        decisive = [r for r in reviews if r.get('user', {}).get('login', '').lower() == APP_SLUG + '[bot]'
                    and r.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')]
        if decisive and decisive[-1].get('commit_id') == head and decisive[-1].get('state') != 'DISMISSED':
            return {'status': 'already-reviewed', 'pr': pr, 'head': head,
                    'decision': decisive[-1]['state'], 'review_id': decisive[-1]['id']}
        from model import execute, prepare
        protected_chain(ROOT / 'runs')
        bundle = prepare(connection, record, connection.pages('files'), reviews)
        receipt = execute(ROOT, config, user, bundle)
        # Recheck immutable installed chain and human admission after model execution.
        current, current_raw, _user = installation()
        if current != config or current_raw != raw:
            raise Refusal('installation-changed')
        enabled(ROOT, raw)
        return connection.publish(bundle, receipt)
    finally:
        if connection is not None:
            connection.revoke()
        os.close(descriptor)


def main(argv=None):
    try:
        bootstrap()
        result = run(sys.argv[1:] if argv is None else argv)
    except Exception:
        # Private HTTP/model/credential diagnostics must never reach coding callers.
        error = sys.exc_info()[1]
        public = {'invalid-request', 'reviews-disabled', 'review-busy', 'stale-or-ineligible-pr',
                  'outside-review-scope', 'outside-issue-scope', 'issue-scope-changed', 'self-review',
                  'incomplete-review', 'review-timeout', 'model-unavailable', 'uncertain-publication',
                  'wrong-review-app', 'wrong-review-installation', 'wrong-review-token-scope',
                  'changed-installed-code', 'changed-model-client', 'changed-interpreter',
                  'changed-model-principal', 'unprotected-model-home', 'installation-changed'}
        reason = str(error) if str(error) in public else 'authority-unavailable'
        print(json.dumps({'status': 'refused', 'reason': reason}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
