"""Offline trust-boundary tests: no App keys, model runs or GitHub calls."""
import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'integrations/review-authority'
HEAD = 'a' * 40
BASE = 'b' * 40
MODEL = 'gemini-3.8-flash-high'


def load_modules(monkeypatch):
    result = {}
    for name in ('security', 'validation', 'app', 'model', 'request', 'client', 'install', 'bundle'):
        spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        result[name] = module
    return SimpleNamespace(**result)


@pytest.fixture
def modules(monkeypatch):
    return load_modules(monkeypatch)


def snapshot():
    return {'head': HEAD, 'base': BASE, 'nonce': 'fresh-snapshot',
            'changed_paths': ['code.py'], 'head_sources': {'code.py': 'first\nsecond\n'}}


def events(bundle=None):
    bundle = bundle or snapshot()
    answer = {'head': bundle['head'], 'nonce': bundle['nonce'], 'decision': 'APPROVE',
              'summary': 'No actionable defects in both lines.', 'reviewed_paths': ['code.py'],
              'findings': [], 'limitations': [], 'resolutions': []}
    return [{'event': 'init', 'init': {'model': MODEL}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'conversation_id': 'session-1',
                                         'structured_output': answer}}]


def record():
    return {'number': 828, 'state': 'open', 'draft': False, 'body': 'Closes #826\n',
            'user': {'login': 'aru-code-factory-gillella[bot]'},
            'head': {'sha': HEAD, 'repo': {'full_name': 'gillella/Aru_Agentic_SDLC'}},
            'base': {'sha': BASE, 'repo': {'full_name': 'gillella/Aru_Agentic_SDLC'}}}


@pytest.mark.parametrize('argv', [[], ['--pr', '0', '--expected-head', HEAD],
    ['--pr', '1;whoami', '--expected-head', HEAD], ['--pr', '1', '--expected-head', 'A' * 40],
    ['--pr', '1', '--expected-head', '0' * 40], ['--pr', '1', '--expected-head', HEAD, '--approve'],
    ['--pr', '1', '--expected-head', HEAD, '--check', '--key-file', 'x']])
def test_receiver_accepts_no_caller_decision_or_command(modules, argv):
    with pytest.raises(modules.security.Refusal, match='invalid-request'):
        modules.security.parse_request(argv)
    assert modules.security.parse_request(['--pr', '828', '--expected-head', HEAD]) == (828, HEAD, False)


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(state='closed'), lambda value: value.update(draft=True),
    lambda value: value['head'].update(sha=BASE),
    lambda value: value['head']['repo'].update(full_name='attacker/fork'),
    lambda value: value.update(body='Closes #827'),
    lambda value: value.update(body='Closes #826\nCloses #827'),
    lambda value: value['user'].update(login='arureviewauthority[bot]')])
def test_ineligible_prs_refuse_before_model(modules, monkeypatch, mutation):
    connection = modules.app.Connection(ROOT, {'repository': 'gillella/Aru_Agentic_SDLC', 'issues': [826]}, 828)
    value = record()
    mutation(value)
    monkeypatch.setattr(connection, 'api', lambda *args: value)
    with pytest.raises(modules.security.Refusal):
        connection.pr_record(HEAD)


def test_issue_and_base_remain_bound(modules, monkeypatch):
    connection = modules.app.Connection(ROOT, {'repository': 'gillella/Aru_Agentic_SDLC', 'issues': [826, 827]}, 828)
    value = record()
    monkeypatch.setattr(connection, 'api', lambda *args: value)
    connection.pr_record(HEAD)
    value['body'] = 'Closes #827'
    with pytest.raises(modules.security.Refusal, match='issue-scope-changed'):
        connection.pr_record(HEAD, BASE)
    value['body'] = 'Closes #826'
    value['base']['sha'] = HEAD
    with pytest.raises(modules.security.Refusal, match='stale-or-ineligible-pr'):
        connection.pr_record(HEAD, BASE)


@pytest.mark.parametrize('path,method', [('/repos/attacker/other/pulls/828', 'GET'),
    ('https://attacker.test/', 'GET'), ('/repos/gillella/Aru_Agentic_SDLC/issues/826', 'PATCH'),
    ('/repos/gillella/Aru_Agentic_SDLC/pulls/999/reviews', 'POST'),
    ('/repos/gillella/Aru_Agentic_SDLC/contents/../key?ref=' + HEAD, 'GET'),
    ('/repos/gillella/Aru_Agentic_SDLC/pulls/828/files?per_page=100&page=11', 'GET')])
def test_connection_has_no_generic_write_endpoint(modules, path, method):
    connection = modules.app.Connection(ROOT, {'repository': 'gillella/Aru_Agentic_SDLC'}, 828)
    assert not connection.allowed(path, method)


@pytest.mark.parametrize('mutation', [
    lambda value: value[-1]['result'].update(status='FAILED'),
    lambda value: value[0]['init'].update(model='different-model'),
    lambda value: value[-1]['result']['structured_output'].update(head=BASE),
    lambda value: value[-1]['result']['structured_output'].update(nonce='replayed'),
    lambda value: value[-1]['result']['structured_output'].update(reviewed_paths=[]),
    lambda value: value[-1]['result']['structured_output'].update(reviewed_paths=['code.py', 'code.py']),
    lambda value: value[-1]['result']['structured_output'].update(limitations=['Missing context']),
    lambda value: value[-1]['result']['structured_output'].update(findings=[
        {'path': 'code.py', 'line': 1, 'severity': 'high', 'body': 'Incorrect input handling'}]),
    lambda value: value.insert(1, {'step_update': {'step_type': 'tool', 'tool_name': 'shell'}}),
    lambda value: value.insert(1, {'step_update': {'step_type': 'subagent'}})])
def test_incomplete_or_contradictory_session_cannot_approve(modules, mutation):
    value = events()
    mutation(value)
    with pytest.raises(ValueError):
        modules.validation.validate(value, snapshot(), MODEL)


def test_actual_session_success_and_actionable_changes(modules):
    value = events()
    end, answer = modules.validation.validate(value, snapshot(), MODEL)
    assert end['conversation_id'] == 'session-1' and answer['decision'] == 'APPROVE'
    answer.update(decision='REQUEST_CHANGES', findings=[
        {'path': 'code.py', 'line': 2, 'severity': 'medium', 'body': 'Input is not checked'}])
    assert modules.validation.validate(value, snapshot(), MODEL)[1]['decision'] == 'REQUEST_CHANGES'
    answer['findings'][0]['line'] = 3
    with pytest.raises(ValueError, match='Invalid finding line'):
        modules.validation.validate(value, snapshot(), MODEL)


def test_unfinished_or_post_terminal_tools_refuse(modules):
    value = events()
    step = {'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'finish',
            'tool_info': {}, 'conversation_id': 'session-1', 'step_index': 0, 'state': 'ACTIVE'}}
    value.insert(1, step)
    with pytest.raises(ValueError, match='Unfinished'):
        modules.validation.validate(value, snapshot(), MODEL)
    step['step_update']['state'] = 'DONE'
    assert modules.validation.validate(value, snapshot(), MODEL)[1]['decision'] == 'APPROVE'
    value.append(copy.deepcopy(step))
    with pytest.raises(ValueError, match='Unbound'):
        modules.validation.validate(value, snapshot(), MODEL)


def test_missing_or_truncated_source_never_reaches_model(modules):
    connection = SimpleNamespace(repository='gillella/Aru_Agentic_SDLC', pr=828, issue=826,
                                 token='fake', source=lambda *args: 'source', api=lambda *args: {})
    with pytest.raises(modules.security.Refusal, match='incomplete-source'):
        modules.model.prepare(connection, record(), [{'filename': 'code.py'}], [])
    connection = modules.app.Connection(ROOT, {'repository': 'gillella/Aru_Agentic_SDLC'}, 828)
    connection.api = lambda *args: [{}] * 100
    with pytest.raises(modules.security.Refusal, match='truncated-github-evidence'):
        connection.pages('files')


def test_publication_rechecks_and_reads_back_exact_account(modules, monkeypatch):
    connection = modules.app.Connection(ROOT, {'repository': 'gillella/Aru_Agentic_SDLC',
                                               'model': MODEL, 'issues': [826]}, 828)
    calls = []
    def api(path, token, method='GET', payload=None):
        calls.append((method, payload))
        if method == 'GET':
            return record()
        assert payload['commit_id'] == HEAD and payload['event'] == 'APPROVE'
        return {'commit_id': HEAD, 'state': 'APPROVED', 'user': {'login': 'arureviewauthority[bot]'}, 'id': 55}
    monkeypatch.setattr(connection, 'api', api)
    receipt = {'result': events()[-1]['result']['structured_output'],
               'conversation_id': 'session-1', 'transcript_sha256': 'c' * 64}
    assert connection.publish(snapshot(), receipt)['review_id'] == 55
    assert [method for method, _ in calls] == ['GET', 'POST', 'GET']
    monkeypatch.setattr(connection, 'api', lambda path, token, method='GET', payload=None:
                        record() if method == 'GET' else {'commit_id': BASE})
    with pytest.raises(modules.security.Refusal, match='uncertain-publication'):
        connection.publish(snapshot(), receipt)


def test_disabled_and_concurrent_requests_never_authenticate(modules, monkeypatch, tmp_path):
    security, request = modules.security, modules.request
    # The production entry point owns its process; this in-process fixture must
    # restore the test runner's environment and umask after exercising that boundary.
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    monkeypatch.setattr(os, 'umask', lambda value: 0o022)
    (tmp_path / 'runtime').mkdir()
    monkeypatch.setattr(request, 'ROOT', tmp_path)
    monkeypatch.setattr(security, 'installation', lambda: ({}, b'config', None))
    monkeypatch.setattr(security, 'protected_chain', lambda path: None)
    monkeypatch.setattr(security, 'enabled', lambda *args: (_ for _ in ()).throw(security.Refusal('reviews-disabled')))
    monkeypatch.setattr(modules.app.Connection, 'authenticate', lambda self: pytest.fail('must not authenticate'))
    with pytest.raises(security.Refusal, match='reviews-disabled'):
        request.run(['--pr', '828', '--expected-head', HEAD])
    monkeypatch.setattr(security, 'enabled', lambda *args: None)
    # Exercise the actual nonblocking lock; fake only privileged file ownership.
    real_stat = os.fstat
    def root_stat(fd):
        info = real_stat(fd)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1,
                               st_size=info.st_size)
    monkeypatch.setattr(os, 'fstat', root_stat)
    import fcntl
    with (tmp_path / 'runtime/request.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(security.Refusal, match='review-busy'):
            request.run(['--pr', '828', '--expected-head', HEAD])


def test_marker_is_configuration_bound_and_duplicate_json_refuses(modules, monkeypatch):
    security = modules.security
    raw = b'accepted configuration'
    monkeypatch.setattr(security, 'read_file', lambda *args, **kwargs:
                        json.dumps({'version': 1, 'config_sha256': security.digest(raw)}).encode())
    security.enabled(ROOT, raw)
    with pytest.raises(security.Refusal, match='reviews-disabled'):
        security.enabled(ROOT, b'changed issue scope')
    with pytest.raises(security.Refusal, match='duplicate-json-key'):
        security.parse_json('{"version":1,"version":2}')


@pytest.mark.parametrize('path', ['../key', '/key', 'a/.git/config', 'a\\b', 'a//b', 'a\n'])
def test_unsafe_snapshot_paths_refuse(modules, path):
    assert not modules.app.safe_path(path)


def test_clients_use_fixed_protected_backends_only(modules):
    client = modules.client
    review = client.command(['review', '--pr', '828', '--expected-head', HEAD, '--local'])
    assert review == ['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I',
                      '/private/var/db/aru-trusted-review/request.py', '--pr', '828', '--expected-head', HEAD]
    remote = client.command(['merge', '--pr', '828', '--expected-head', HEAD])
    assert remote[-2] == 'macmini' and '-oForwardAgent=no' in remote
    assert '/private/var/db/aru-trusted-merge/request.py' in remote[-1]
    assert 'aru-merge-authority-run' not in remote[-1]
    assert client.command(['check', '--pr', '828', '--expected-head', HEAD, '--local'])[-1] == '--check'
    with pytest.raises(ValueError):
        client.command(['review', '--pr', '828; id', '--expected-head', HEAD])


def test_client_verifies_operation_pr_head_and_suppresses_private_output(modules):
    client = modules.client
    reviewed = {'status': 'reviewed', 'pr': 828, 'head': HEAD, 'decision': 'APPROVED', 'review_id': 55,
                'secret': 'private diagnostics'}
    assert 'secret' not in client.response(json.dumps(reviewed), 0, 'review', 828, HEAD)
    for operation, pr, head in [('merge', 828, HEAD), ('review', 829, HEAD), ('review', 828, BASE)]:
        assert client.response(json.dumps(reviewed), 0, operation, pr, head)['status'] == 'refused'
    merged = {'pr': 828, 'head': HEAD, 'merged': True, 'checked': False,
              'finalized': False, 'merge_commit': BASE}
    assert client.response(json.dumps(merged), 0, 'merge', 828, HEAD)['status'] == 'merged'
    merged.update(merged=False, checked=True, merge_commit=None)
    assert client.response(json.dumps(merged), 0, 'check', 828, HEAD)['status'] == 'checked'
    assert client.response('private stack trace', 1, 'review', 828, HEAD) == {
        'status': 'refused', 'reason': 'transport-unavailable'}
    assert client.response(json.dumps({'refused': True, 'gate': '/private/key'}), 1,
                           'merge', 828, HEAD)['reason'] == 'authority-refused'


def test_client_transport_failure_has_no_fallback(modules, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(subprocess, 'run', unavailable)
    assert modules.client.main(['review', '--pr', '828', '--expected-head', HEAD]) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'transport-unavailable'


def test_timeout_kills_entire_model_group(modules, monkeypatch, tmp_path):
    calls = []
    process = SimpleNamespace(pid=12345, returncode=-9, poll=lambda: None,
                              wait=lambda **kwargs: calls.append('wait'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(os, 'killpg', lambda *args: calls.append(args))
    user = SimpleNamespace(pw_uid=503, pw_gid=503)
    with pytest.raises(modules.security.Refusal, match='review-timeout'):
        modules.model.bounded_process([], tmp_path, {}, user, b'input', tmp_path, timeout=0)
    assert calls[0][0] == 12345 and calls[-1] == 'wait'


def test_model_profile_and_environment_do_not_expose_publisher_keys(modules):
    user = SimpleNamespace(pw_dir='/Users/aru-review-model')
    profile = modules.model.profile(Path('/private/var/db/aru-trusted-review'),
                                    Path('/private/var/db/aru-trusted-review/runs/nonce'), user)
    assert 'credentials' not in profile and 'gillella' not in profile
    assert '(allow process-exec (literal "/private/var/db/aru-trusted-review/agy"))' in profile
    assert '(deny default)' in profile


def test_prior_changes_require_specific_exact_head_disposition(modules):
    bundle = snapshot()
    bundle['previous_reviews'] = [{'id': 55, 'login': 'arureviewauthority[bot]', 'state': 'CHANGES_REQUESTED'}]
    value = events(bundle)
    with pytest.raises(ValueError, match='Prior findings'):
        modules.validation.validate(value, bundle, MODEL)
    value[-1]['result']['structured_output']['resolutions'] = [
        {'review_id': 55, 'evidence': 'Both input paths now reject the previously unchecked value.'}]
    assert modules.validation.validate(value, bundle, MODEL)[1]['decision'] == 'APPROVE'
    value[-1]['result']['structured_output']['resolutions'][0]['review_id'] = 99
    with pytest.raises(ValueError, match='Unbound disposition'):
        modules.validation.validate(value, bundle, MODEL)


def test_inline_findings_use_changed_source_side(modules):
    bundle = {'diff': [{'path': 'code.py', 'status': 'modified',
                       'patch': '@@ -1,2 +1,2 @@\n first\n-second\n+corrected'}]}
    finding = {'path': 'code.py', 'line': 2, 'severity': 'high', 'body': 'Input is unchecked'}
    assert modules.app.inline_findings(bundle, [finding])[0]['side'] == 'RIGHT'
    bundle['diff'][0]['status'] = 'removed'
    assert modules.app.inline_findings(bundle, [finding])[0]['side'] == 'LEFT'
    finding['line'] = 99
    assert modules.app.inline_findings(bundle, [finding]) == []
