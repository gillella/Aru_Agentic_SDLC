"""Protected-package continuity and denial tests without privileged installation."""
import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_review_request import HEAD, MODEL, ROOT, load_modules


@pytest.fixture
def modules(monkeypatch):
    return load_modules(monkeypatch)


def test_writable_linked_and_nonroot_files_are_denied(modules, monkeypatch, tmp_path):
    target = tmp_path / 'source.py'
    target.write_text('source')
    real_stat = os.fstat
    metadata = {'st_uid': 0, 'st_mode': stat.S_IFREG | 0o644, 'st_nlink': 1}
    def fake_stat(fd):
        return SimpleNamespace(**metadata, st_size=real_stat(fd).st_size)
    monkeypatch.setattr(os, 'fstat', fake_stat)
    assert modules.security.read_file(target) == b'source'
    for change in ({'st_uid': 501}, {'st_mode': stat.S_IFREG | 0o664}, {'st_nlink': 2},
                   {'st_mode': stat.S_IFLNK | 0o644}):
        saved = metadata.copy()
        metadata.update(change)
        with pytest.raises(modules.security.Refusal):
            modules.security.read_file(target)
        metadata.update(saved)
    link = tmp_path / 'symlink'
    link.symlink_to(target)
    with pytest.raises(OSError):
        modules.security.read_file(link)
    with pytest.raises(modules.security.Refusal):
        modules.security.read_file(target, private=True)


def test_writable_or_linked_ancestor_refuses(modules, monkeypatch, tmp_path):
    original = Path.lstat
    def metadata(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
    monkeypatch.setattr(Path, 'lstat', metadata)
    parent = tmp_path / 'protected'
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    # A writable installation ancestor cannot be privileged staging.
    with pytest.raises(modules.security.Refusal, match='unprotected-directory'):
        modules.security.protected_chain(parent)
    with pytest.raises(ValueError, match='Root-controlled'):
        modules.install.protected_chain(parent)
    parent.chmod(0o755)
    linked = tmp_path / 'linked'
    linked.symlink_to(parent, target_is_directory=True)
    with pytest.raises(modules.security.Refusal, match='unprotected-directory'):
        modules.security.protected_chain(linked)


def configuration(modules):
    security = modules.security
    return {'version': 1, 'repository': security.REPOSITORY, 'issues': [826],
            'model_user': 'aru-review-model', 'model_uid': os.getuid(), 'model_gid': os.getgid(),
            'model_home': '/Users/aru-review-model', 'model': MODEL, 'source_head': HEAD,
            'files': {name: security.digest(name.encode()) for name in security.FILES},
            'client_sha256': security.digest(b'client'), 'interpreter_sha256': security.digest(b'python')}


def test_code_client_interpreter_and_account_seals(modules, monkeypatch, tmp_path):
    security = modules.security
    config = configuration(modules)
    home = tmp_path / 'model-home'
    home.mkdir(mode=0o700)
    user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=config['model_home'],
                           pw_shell='/usr/bin/false', pw_name='aru-review-model')
    contents = {ROOT / name: name.encode() for name in security.FILES}
    contents.update({ROOT / 'agy': b'client', Path('/usr/bin/python3'): b'python'})
    contents[ROOT / 'config.json'] = json.dumps(config).encode()
    monkeypatch.setattr(security, 'read_file', lambda path, **kwargs: contents[path])
    monkeypatch.setattr(security, 'protected_chain', lambda path: None)
    monkeypatch.setattr(security.pwd, 'getpwnam', lambda name: user)
    monkeypatch.setattr(os, 'getgrouplist', lambda *args: [os.getgid()])
    monkeypatch.setattr(security, 'Path', lambda path: home if path == config['model_home'] else Path(path))
    assert security.installation(ROOT)[0] == config
    for path in (ROOT / 'request.py', ROOT / 'agy', Path('/usr/bin/python3')):
        saved = contents[path]
        contents[path] += b'tampered'
        with pytest.raises(security.Refusal, match='changed-'):
            security.installation(ROOT)
        contents[path] = saved
    monkeypatch.setattr(os, 'getgrouplist', lambda *args: [80])
    with pytest.raises(security.Refusal, match='changed-model-principal'):
        security.installation(ROOT)


@pytest.mark.parametrize('change', [{'repository': 'attacker/fork'}, {'issues': []},
    {'issues': [826, 826]}, {'issues': [True]}, {'model_user': 'gillella'},
    {'files': {}}, {'interpreter_sha256': 'x'}, {'source_head': 'branch-name'}])
def test_scope_or_seal_configuration_is_not_extensible(modules, change):
    config = configuration(modules)
    config.update(change)
    with pytest.raises(modules.security.Refusal):
        modules.security.validate_config(config)


def test_sudo_policy_has_only_digest_pinned_fixed_script(modules):
    policy = modules.install.policy('c' * 64)
    assert 'sha256:' + 'c' * 64 + ' /usr/bin/python3 -I /private/var/db/aru-trusted-review/request.py' in policy
    assert 'NOPASSWD: ARU_REVIEW_REQUEST' in policy
    assert '!setenv' in policy and 'timestamp_timeout=0' in policy
    assert '/bin/sh' not in policy and 'aru-merge-authority-run' not in policy
    with pytest.raises(ValueError):
        modules.install.policy('caller-supplied-interpreter')


def installer_fixture(modules, monkeypatch, tmp_path):
    install = modules.install
    root = tmp_path / 'installed'
    policy = tmp_path / 'sudoers.d/review'
    policy.parent.mkdir()
    monkeypatch.setattr(install, 'ROOT', root)
    monkeypatch.setattr(install, 'Path', lambda path:
                        policy if str(path) == '/private/etc/sudoers.d/aru-trusted-review' else Path(path))
    monkeypatch.setattr(install, 'protected_chain', lambda path: None)
    payload = {name: name.encode() for name in install.PAYLOAD}
    monkeypatch.setattr(install, 'package', lambda *args: payload)
    monkeypatch.setattr(install, 'model_user', lambda: SimpleNamespace(
        pw_name='aru-review-model', pw_uid=503, pw_gid=503, pw_dir='/Users/aru-review-model'))
    monkeypatch.setattr(install, 'read_regular', lambda path, **kwargs:
                        b'-----BEGIN PRIVATE KEY-----' + b'x' * 600 if str(path) == '/human/key.pem' else b'python')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    # Installer changes umask only in its real operator process; isolate this test process.
    monkeypatch.setattr(os, 'umask', lambda value: 0o022)
    return root, policy


def test_installation_is_disabled_and_preserves_existing_state(modules, monkeypatch, tmp_path, capsys):
    root, policy = installer_fixture(modules, monkeypatch, tmp_path)
    modules.install.install(tmp_path, HEAD, [826], Path('/human/key.pem'))
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    assert policy.is_file() and not (root / 'enabled.json').exists()
    assert stat.S_IMODE((root / 'credentials/reviewer.pem').stat().st_mode) == 0o600
    before = (root / 'config.json').read_bytes()
    with pytest.raises(ValueError, match='Existing installation preserved'):
        modules.install.install(tmp_path, HEAD, [827], Path('/human/key.pem'))
    assert (root / 'config.json').read_bytes() == before


def test_failure_before_policy_rolls_back_only_new_installation(modules, monkeypatch, tmp_path):
    root, policy = installer_fixture(modules, monkeypatch, tmp_path)
    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, 'run', failure)
    with pytest.raises(subprocess.CalledProcessError):
        modules.install.install(tmp_path, HEAD, [826], Path('/human/key.pem'))
    assert not root.exists() and not policy.exists()


def test_failure_during_policy_write_removes_created_policy(modules, monkeypatch, tmp_path):
    root, policy = installer_fixture(modules, monkeypatch, tmp_path)
    real_fdopen = os.fdopen
    class BrokenWriter:
        def __init__(self, fd):
            self.fd = fd
        def __enter__(self):
            return self
        def __exit__(self, *args):
            os.close(self.fd)
        def write(self, text):
            raise OSError('interrupted policy installation')
    monkeypatch.setattr(os, 'fdopen', lambda fd, mode: BrokenWriter(fd) if mode == 'w' else real_fdopen(fd, mode))
    with pytest.raises(OSError, match='interrupted policy'):
        modules.install.install(tmp_path, HEAD, [826], Path('/human/key.pem'))
    assert not root.exists() and not policy.exists()


def test_existing_policy_is_never_removed(modules, monkeypatch, tmp_path):
    root, policy = installer_fixture(modules, monkeypatch, tmp_path)
    policy.write_text('existing operator policy')
    with pytest.raises(ValueError, match='Existing sudo policy preserved'):
        modules.install.install(tmp_path, HEAD, [826], Path('/human/key.pem'))
    assert policy.read_text() == 'existing operator policy' and not root.exists()


def test_package_manifest_tamper_and_wrong_source_are_rejected(modules, monkeypatch, tmp_path):
    install = modules.install
    monkeypatch.setattr(install, 'protected_chain', lambda path: None)
    monkeypatch.setattr(install, 'read_regular', lambda path, **kwargs: path.read_bytes())
    manifest = {'version': 1, 'source_head': HEAD, 'files': {}}
    for name in install.PAYLOAD:
        raw = name.encode()
        (tmp_path / name).write_bytes(raw)
        manifest['files'][name] = install.digest(raw)
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    assert set(install.package(tmp_path, HEAD)) == set(install.PAYLOAD)
    (tmp_path / 'request.py').write_text('replaced')
    with pytest.raises(ValueError, match='Changed reviewed package'):
        install.package(tmp_path, HEAD)
    with pytest.raises(ValueError, match='Wrong reviewed package'):
        install.package(tmp_path, 'b' * 40)


def test_deterministic_public_package_never_packages_working_tree_or_keys(modules, monkeypatch, tmp_path):
    calls = []
    def git_object(argv, **kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(stdout=argv[-1].encode())
    monkeypatch.setattr(subprocess, 'run', git_object)
    client = tmp_path / 'public-client'
    client.write_bytes(b'explicitly selected public binary')
    hash_value = modules.security.digest(client.read_bytes())
    first, second = tmp_path / 'one.zip', tmp_path / 'two.zip'
    assert modules.bundle.build(tmp_path, HEAD, client, hash_value, first) == modules.bundle.build(
        tmp_path, HEAD, client, hash_value, second)
    assert first.read_bytes() == second.read_bytes()
    assert all(value.startswith(HEAD + ':integrations/review-authority/') for value in calls)
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == set(modules.install.PAYLOAD) | {'manifest.json'}
        assert 'credentials/reviewer.pem' not in archive.namelist()
        assert json.loads(archive.read('manifest.json'))['source_head'] == HEAD
    with pytest.raises(FileExistsError):
        modules.bundle.build(tmp_path, HEAD, client, hash_value, first)
    with pytest.raises(ValueError, match='differs'):
        modules.bundle.build(tmp_path, HEAD, client, '0' * 64, tmp_path / 'wrong.zip')
