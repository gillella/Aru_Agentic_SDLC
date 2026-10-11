#!/usr/bin/env python3
"""Credential-free local/remote client; never loads a GitHub App key."""
import json
import re
import subprocess
import sys

PUBLIC_REASONS = {'invalid-request', 'reviews-disabled', 'review-busy', 'stale-or-ineligible-pr',
                  'outside-review-scope', 'outside-issue-scope', 'issue-scope-changed', 'self-review',
                  'incomplete-review', 'review-timeout', 'model-unavailable', 'uncertain-publication',
                  'wrong-review-app', 'wrong-review-installation', 'wrong-review-token-scope',
                  'submissions-disabled', 'kernel-refused', 'authority-unavailable'}


def command(argv):
    if (len(argv) not in (5, 6) or argv[0] not in ('review', 'merge', 'check')
            or argv[1] != '--pr' or argv[3] != '--expected-head'
            or (len(argv) == 6 and argv[5] != '--local')
            or not re.fullmatch('[1-9][0-9]{0,8}', argv[2])
            or not re.fullmatch('[0-9a-f]{40}', argv[4]) or argv[4] == '0' * 40):
        raise ValueError('invalid-request')
    operation, pr, head = argv[0], argv[2], argv[4]
    if operation == 'review':
        remote = ['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I',
                  '/private/var/db/aru-trusted-review/request.py']
    else:
        remote = ['/usr/bin/sudo', '-n', '-u', 'aru-merge', '/usr/bin/python3', '-I',
                  '/private/var/db/aru-trusted-merge/request.py']
    remote += ['--pr', pr, '--expected-head', head]
    if operation == 'check':
        remote += ['--check']
    if len(argv) == 6:
        return remote
    # Remote words are fixed constants or grammar-validated decimal/hex values.
    return ['/usr/bin/ssh', '-T', '-x', '-oBatchMode=yes', '-oForwardAgent=no',
            '-oClearAllForwardings=yes', '-oPermitLocalCommand=no', '-oRemoteCommand=none',
            '-oStrictHostKeyChecking=yes', '-oConnectTimeout=10', 'macmini', ' '.join(remote)]


def merge_status(value):
    if set(value) != {'pr', 'head', 'merged', 'checked', 'finalized', 'merge_commit'}:
        raise ValueError('invalid-response')
    if any(type(value[k]) is not bool for k in ('merged', 'checked', 'finalized')):
        raise ValueError('invalid-response')
    if value['finalized'] and value['merged']:
        return 'finalized'
    if value['merged'] and not value['checked']:
        return 'merged'
    if value['checked'] and not value['merged'] and not value['finalized']:
        return 'checked'
    raise ValueError('invalid-response')


def public_fields(value):
    result = {}
    for key in ('pr', 'review_id'):
        if key in value:
            if type(value[key]) is not int or value[key] <= 0:
                raise ValueError('invalid-response')
            result[key] = value[key]
    for key in ('head', 'merge_commit'):
        if key in value and value[key] is not None:
            if not isinstance(value[key], str) or not re.fullmatch('[0-9a-f]{40}', value[key]):
                raise ValueError('invalid-response')
            result[key] = value[key]
    if 'decision' in value:
        if value['decision'] not in ('APPROVED', 'CHANGES_REQUESTED'):
            raise ValueError('invalid-response')
        result['decision'] = value['decision']
    return result


def response(output, returncode, operation, pr, head):
    try:
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError('invalid-response')
        if returncode or value.get('status') == 'refused' or value.get('refused') is True:
            reason = value.get('reason', value.get('gate'))
            return {'status': 'refused', 'reason': reason if reason in PUBLIC_REASONS else 'authority-refused'}
        # Merge backend has its own result format. Never echo arbitrary private fields.
        status = value.get('status')
        if status is None:
            status = merge_status(value)
        if status not in ('reviewed', 'already-reviewed', 'preflight-passed', 'merged', 'checked', 'finalized'):
            raise ValueError('invalid-response')
        expected_statuses = {'review': ('reviewed', 'already-reviewed'),
                             'merge': ('merged', 'finalized'), 'check': ('checked',)}
        if (status not in expected_statuses[operation] or type(value.get('pr')) is not int
                or value['pr'] != pr or value.get('head') != head):
            raise ValueError('invalid-response')
        if operation == 'review' and ('decision' not in value or 'review_id' not in value):
            raise ValueError('invalid-response')
        if operation != 'review' and status != 'checked' and value.get('merge_commit') is None:
            raise ValueError('invalid-response')
        result = {'status': status}
        result.update(public_fields(value))
        return result
    except (ValueError, TypeError):
        return {'status': 'refused', 'reason': 'transport-unavailable'}


def main(argv=None):
    try:
        request = sys.argv[1:] if argv is None else argv
        args = command(request)
        process = subprocess.run(args, capture_output=True, text=True, timeout=1000)
        result = response(process.stdout, process.returncode, request[0], int(request[2]), request[4])
    except ValueError:
        result = {'status': 'refused', 'reason': 'invalid-request'}
    except (OSError, subprocess.TimeoutExpired):
        result = {'status': 'refused', 'reason': 'transport-unavailable'}
    print(json.dumps(result))
    return int(result['status'] == 'refused')


if __name__ == '__main__':
    raise SystemExit(main())
