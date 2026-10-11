#!/usr/bin/env python3
"""Human root-Terminal installation and enablement; never called by Kernel tools."""
import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path('/private/var/db/aru-trusted-review')
PAYLOAD = ('request.py', 'security.py', 'app.py', 'model.py', 'validation.py', 'client.py', 'install.py', 'agy')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def protected_chain(path):
    for item in reversed([Path(path), *Path(path).parents]):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Root-controlled staging/installation required')


def read_regular(path, limit=256 * 1024 * 1024, root_owned=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or (root_owned and (info.st_uid != 0 or info.st_mode & 0o022))):
            raise ValueError('Unprotected or invalid input')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            result = source.read(limit + 1)
        if len(result) > limit:
            raise ValueError('Oversized input')
        return result
    finally:
        os.close(descriptor)


def package(source, expected):
    protected_chain(source)
    manifest = json.loads(read_regular(source / 'manifest.json', limit=65536))
    if (set(manifest) != {'version', 'source_head', 'files'} or manifest['version'] != 1
            or manifest['source_head'] != expected or not re.fullmatch('[0-9a-f]{40}', expected)
            or set(manifest['files']) != set(PAYLOAD)):
        raise ValueError('Wrong reviewed package')
    result = {}
    for name in PAYLOAD:
        raw = read_regular(source / name)
        if digest(raw) != manifest['files'][name]:
            raise ValueError('Changed reviewed package')
        result[name] = raw
    return result


def model_user():
    user = pwd.getpwnam('aru-review-model')
    if (user.pw_uid < 500 or user.pw_gid <= 0 or user.pw_dir != '/Users/aru-review-model'
            or user.pw_shell != '/usr/bin/false' or 80 in os.getgrouplist(user.pw_name, user.pw_gid)):
        raise ValueError('Dedicated non-admin nonlogin model account required')
    home = Path(user.pw_dir)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
        raise ValueError('Private model home required')
    protected_chain(home.parent)
    return user


def policy(interpreter_hash):
    if not re.fullmatch('[0-9a-f]{64}', interpreter_hash):
        raise ValueError('Invalid interpreter digest')
    command = ('sha256:' + interpreter_hash + ' /usr/bin/python3 -I ' + str(ROOT / 'request.py') +
               ' --pr * --expected-head *')
    return ('# Separately reviewed Aru automatic-review request; no generic interpreter grant.\n'
            'Defaults!ARU_REVIEW_REQUEST env_reset, !setenv, !requiretty, timestamp_timeout=0\n'
            'Cmnd_Alias ARU_REVIEW_REQUEST = ' + command + '\n'
            'gillella ALL=(root) NOPASSWD: ARU_REVIEW_REQUEST\n')


def install(source, expected, issues, key_path):
    payload = package(source, expected)
    user = model_user()
    if ROOT.exists() or ROOT.is_symlink():
        raise ValueError('Existing installation preserved; no overwrite')
    protected_chain(ROOT.parent)
    policy_path = Path('/private/etc/sudoers.d/aru-trusted-review')
    protected_chain(policy_path.parent)
    if policy_path.exists() or policy_path.is_symlink():
        raise ValueError('Existing sudo policy preserved')
    if not issues or len(issues) > 100 or len(set(issues)) != len(issues) or any(not 0 < n < 10**9 for n in issues):
        raise ValueError('Explicit unique issue scope required')
    # This read is performed only by the human's privileged installer, never an agent/model.
    key = read_regular(key_path, limit=20000, root_owned=False)
    if b'PRIVATE KEY-----' not in key or len(key) < 500:
        raise ValueError('Fresh private App PEM required')
    interpreter_hash = digest(read_regular(Path('/usr/bin/python3')))
    os.umask(0o077)
    created = False
    policy_created = False
    try:
        ROOT.mkdir(mode=0o755)
        created = True
        os.chmod(ROOT, 0o755)
        for name, raw in payload.items():
            target = ROOT / name
            target.write_bytes(raw)
            os.chmod(target, 0o755 if name == 'agy' else 0o644)
        config = {'version': 1, 'repository': 'gillella/Aru_Agentic_SDLC', 'issues': sorted(issues),
                  'model_user': user.pw_name, 'model_uid': user.pw_uid, 'model_gid': user.pw_gid,
                  'model_home': user.pw_dir, 'model': 'gemini-3.8-flash-high',
                  'source_head': expected, 'client_sha256': digest(payload['agy']),
                  'interpreter_sha256': interpreter_hash,
                  'files': {name: digest(payload[name]) for name in ('request.py', 'security.py', 'app.py', 'model.py', 'validation.py')}}
        (ROOT / 'config.json').write_text(json.dumps(config, sort_keys=True, separators=(',', ':')))
        os.chmod(ROOT / 'config.json', 0o644)
        for name, mode in (('credentials', 0o700), ('runtime', 0o700), ('runs', 0o755)):
            (ROOT / name).mkdir(mode=mode)
            os.chmod(ROOT / name, mode)
        (ROOT / 'credentials/reviewer.pem').write_bytes(key)
        os.chmod(ROOT / 'credentials/reviewer.pem', 0o600)
        subprocess.run(['/usr/bin/openssl', 'rsa', '-in', str(ROOT / 'credentials/reviewer.pem'),
                        '-check', '-noout'], capture_output=True, check=True, timeout=15)
        candidate = ROOT / 'review-sudoers'
        candidate.write_text(policy(interpreter_hash))
        os.chmod(candidate, 0o440)
        subprocess.run(['/usr/sbin/visudo', '-cf', str(candidate)], capture_output=True, check=True, timeout=15)
        # Exclusive creation prevents replacing any concurrent policy installation.
        descriptor = os.open(policy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440)
        policy_created = True
        with os.fdopen(descriptor, 'w') as output:
            output.write(candidate.read_text())
            os.fchmod(output.fileno(), 0o440)
    except Exception:
        # This installation never enabled reviews. Existing installations/policies were refused.
        if policy_created:
            policy_path.unlink()
        if created:
            shutil.rmtree(ROOT)
        raise
    print(json.dumps({'installed': True, 'enabled': False, 'source_head': expected,
                      'repository': config['repository'], 'issues': config['issues']}))


def admission(action):
    protected_chain(ROOT)
    for name in ('security.py', 'install.py'):
        read_regular(ROOT / name)
    sys.path.insert(0, str(ROOT))
    from security import installation
    _config, raw, _user = installation()
    marker = ROOT / 'enabled.json'
    if action == 'disable':
        if input('Coordinate in-flight requests; type DISABLE: ') != 'DISABLE':
            raise ValueError('Not disabled')
        marker.unlink(missing_ok=True)
        print(json.dumps({'enabled': False, 'in_flight_cancelled': False}))
        return
    print(json.dumps({key: _config[key] for key in ('repository', 'issues', 'source_head',
                     'model_user', 'model', 'client_sha256', 'interpreter_sha256')}, sort_keys=True))
    print('Config SHA256:', digest(raw))
    print('Complete #827 host/credential/model/denial proof and explicit acceptance before enabling.')
    if input('Accepted this exact installation and scope? Type ENABLE: ') != 'ENABLE':
        raise ValueError('Not enabled')
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), 0o644)
        json.dump({'version': 1, 'config_sha256': digest(raw)}, output, sort_keys=True)
    print(json.dumps({'enabled': True, 'config_sha256': digest(raw)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'enable', 'disable'))
    parser.add_argument('--source-head')
    parser.add_argument('--issue', type=int, action='append')
    parser.add_argument('--key-file', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.stdin.isatty() or not sys.flags.isolated:
        raise ValueError('Human isolated root Terminal required')
    # The installer itself must be staged root-owned before its first privileged use.
    protected_chain(Path(__file__).absolute().parent)
    read_regular(Path(__file__).absolute())
    if args.action == 'install':
        if not args.source_head or not args.issue or not args.key_file or not args.key_file.is_absolute():
            parser.error('Reviewed source head, explicit issue scope and private local key path required')
        install(Path(__file__).absolute().parent, args.source_head, args.issue, args.key_file)
    elif args.source_head or args.issue or args.key_file:
        parser.error('Enable/disable accepts no package, scope or credential arguments')
    else:
        admission(args.action)


if __name__ == '__main__':
    main()
