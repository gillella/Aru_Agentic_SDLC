"""Protected installation validation; no network or credential access."""
import hashlib
import json
import os
import pwd
import re
import stat
from pathlib import Path

ROOT = Path('/private/var/db/aru-trusted-review')
FILES = ('request.py', 'security.py', 'app.py', 'model.py', 'validation.py')
REPOSITORY = 'gillella/Aru_Agentic_SDLC'


class Refusal(Exception):
    """Only this public reason reaches the caller."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(path, private=False, limit=32 * 1024 * 1024):
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Refusal('invalid-protected-file')
        if info.st_uid != 0 or info.st_mode & 0o022 or (private and info.st_mode & 0o077):
            raise Refusal('unprotected-file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise Refusal('oversized-file')
        return data
    finally:
        os.close(descriptor)


def protected_chain(path):
    for item in reversed([Path(path), *Path(path).parents]):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal('unprotected-directory')


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refusal('duplicate-json-key')
        value[key] = item
    return value


def parse_json(data):
    return json.loads(data, object_pairs_hook=unique_object)


def parse_request(argv):
    if (len(argv) not in (4, 5) or argv[:1] != ['--pr']
            or argv[2:3] != ['--expected-head'] or (len(argv) == 5 and argv[4] != '--check')
            or not re.fullmatch(r'[1-9][0-9]{0,8}', argv[1])
            or not re.fullmatch(r'[0-9a-f]{40}', argv[3]) or argv[3] == '0' * 40):
        raise Refusal('invalid-request')
    return int(argv[1]), argv[3], len(argv) == 5


def validate_config(value):
    fields = {'version', 'repository', 'issues', 'model_user', 'model_uid', 'model_gid',
              'model_home', 'model', 'client_sha256', 'interpreter_sha256', 'files', 'source_head'}
    if not isinstance(value, dict) or set(value) != fields or value['version'] != 1:
        raise Refusal('invalid-configuration')
    if value['repository'] != REPOSITORY or value['model'] != 'gemini-3.8-flash-high':
        raise Refusal('unsupported-scope')
    issues = value['issues']
    if (not isinstance(issues, list) or not issues or len(issues) > 100
            or any(type(n) is not int or not 0 < n < 10**9 for n in issues)
            or len(set(issues)) != len(issues)):
        raise Refusal('invalid-issue-scope')
    if value['model_user'] != 'aru-review-model' or value['model_home'] != '/Users/aru-review-model':
        raise Refusal('invalid-model-principal')
    if (type(value['model_uid']) is not int or value['model_uid'] < 500
            or type(value['model_gid']) is not int or value['model_gid'] <= 0):
        raise Refusal('invalid-model-principal')
    if (not isinstance(value['files'], dict) or set(value['files']) != set(FILES)
            or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v)
                   for v in [*value['files'].values(), value['client_sha256'], value['interpreter_sha256']])
            or not isinstance(value['source_head'], str)
            or not re.fullmatch('[0-9a-f]{40}', value['source_head'])):
        raise Refusal('invalid-code-seal')
    return value


def installation(root=ROOT):
    protected_chain(root)
    raw = read_file(root / 'config.json', limit=65536)
    config = validate_config(parse_json(raw))
    for name, expected in config['files'].items():
        if digest(read_file(root / name)) != expected:
            raise Refusal('changed-installed-code')
    if digest(read_file(root / 'agy', limit=256 * 1024 * 1024)) != config['client_sha256']:
        raise Refusal('changed-model-client')
    if digest(read_file(Path('/usr/bin/python3'), limit=256 * 1024 * 1024)) != config['interpreter_sha256']:
        raise Refusal('changed-interpreter')
    user = pwd.getpwnam(config['model_user'])
    if ((user.pw_uid, user.pw_gid, user.pw_dir, user.pw_shell) !=
            (config['model_uid'], config['model_gid'], config['model_home'], '/usr/bin/false')
            or 80 in os.getgrouplist(user.pw_name, user.pw_gid)):
        raise Refusal('changed-model-principal')
    home = Path(user.pw_dir)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
        raise Refusal('unprotected-model-home')
    protected_chain(home.parent)
    return config, raw, user


def enabled(root, raw):
    value = parse_json(read_file(root / 'enabled.json', limit=4096))
    if value != {'version': 1, 'config_sha256': digest(raw)}:
        raise Refusal('reviews-disabled')


def private_directory(path):
    protected_chain(path)
    if path.lstat().st_mode & 0o077:
        raise Refusal('unprotected-private-directory')
