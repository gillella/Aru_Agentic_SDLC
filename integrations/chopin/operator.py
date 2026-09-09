"""Explicit operator preparation/install actions. Importing this module does nothing."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import socket
import subprocess
import time
from urllib.parse import quote

from .manifest_setup import public_origin, validate_app
from .private import PilotError, absolute, load_config, private_dir, read_private, request, write_new

UPSTREAM = '9882eb3a13814ca0f198b6b580858bea86171226'
LABEL = 'local.chopin.catalog-pilot'


def run(argv: list[str], *, data=None, env=None, cwd=None) -> bytes:
    result = subprocess.run(argv, input=data, capture_output=True, timeout=300, cwd=cwd,
                            env=env or {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'HOME': str(Path.home())},
                            check=False)
    if result.returncode:
        raise PilotError('Operator command failed; output suppressed to protect credentials.')
    return result.stdout


def validate_source(config: dict) -> Path:
    source = absolute(config['source_dir'])
    if (run(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip() != UPSTREAM
            or run(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no']).strip()):
        raise PilotError('Upstream must be at the pinned commit with no tracked changes.')
    # Bun loads dotenv automatically. Refuse ambient runtime dotenv without reading secrets.
    for directory in (source, source / 'apps/server', source / 'apps/web'):
        if any((directory / name).exists() for name in ('.env', '.env.local', '.env.production', '.env.production.local', '.env.development', '.env.development.local')):
            raise PilotError('Ambient upstream dotenv exists; use an isolated clean deployment checkout.')
    return source


def credentials(config: dict) -> dict:
    data = json.loads(read_private(absolute(config['credentials_dir']) / 'credentials.json'))
    required = ('id', 'slug', 'client_id', 'client_secret', 'pem', 'owner')
    if set(data) != set(required) or not all(data.get(key) for key in required):
        raise PilotError('Real manifest credentials are required.')
    return data


def verify_credentials(config: dict, creds: dict):
    """Prove the App private key works against GitHub; compare reported App identity.

    The conversion's OAuth secret is retained, but only a real browser token
    exchange can prove the complete OAuth flow. No such claim is made here.
    """
    if config.get('app_browser_settings_verified') is not True:
        raise PilotError('Verify unreported App settings in the browser before preparation.')
    state = private_dir(absolute(config['state_dir']))
    key = state / ('jwt-key-' + secrets.token_hex(8))
    write_new(key, creds['pem'].encode())
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')
    now = int(time.time())
    signing = encode(b'{"alg":"RS256","typ":"JWT"}') + b'.' + encode(json.dumps({'iat': now - 60, 'exp': now + 120, 'iss': creds['client_id']}).encode())
    try:
        signature = run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', str(key)], data=signing)
    finally:
        key.unlink()
    jwt = (signing + b'.' + encode(signature)).decode()
    _, _, body = request('https://api.github.com/app', headers={'Authorization': 'Bearer ' + jwt,
                         'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    data = validate_app(json.loads(body), config)
    if any(data.get(field) != creds[field] for field in ('id', 'slug', 'client_id')):
        raise PilotError('Authenticated App identity differs from private credentials.')


def environment(config: dict, creds: dict) -> dict:
    origin = public_origin(config['public_origin'])
    db = config['database']
    if (db.get('host') != '127.0.0.1' or db.get('port') != 5432
            or any(not re.fullmatch(r'chopin_[a-z0-9_]{1,40}', db.get(k, '')) for k in ('name', 'role'))
            or not re.fullmatch(r'[A-Za-z0-9_-]+', db.get('admin_user', ''))):
        raise PilotError('Use dedicated chopin_ role/database names on loopback PostgreSQL 5432.')
    users = config['allowed_users']
    if (not isinstance(users, list) or not users or config['app_owner'] not in users
            or any(not re.fullmatch(r'[A-Za-z0-9-]{1,39}', user) for user in users)):
        raise PilotError('Explicit nonempty GitHub user admission is required.')
    password = secrets.token_urlsafe(32)
    return {'STORAGE_DRIVER': 'postgres',
            'DATABASE_URL': f"postgresql://{quote(db['role'], safe='')}:{password}@127.0.0.1:5432/{quote(db['name'], safe='')}",
            'APP_ORIGIN': origin, 'GITHUB_APP_SLUG': creds['slug'],
            'GITHUB_APP_CLIENT_ID': creds['client_id'], 'GITHUB_APP_CLIENT_SECRET': creds['client_secret'],
            'GITHUB_ALLOWED_USERS': ','.join(users), 'GITHUB_ALLOWED_ORGANIZATIONS': '',
            'SESSION_ENCRYPTION_KEY': secrets.token_hex(32), 'SERVER_HOST': '127.0.0.1', 'PORT': '3050',
            'AGENT': 'off', 'BACKGROUND_JOBS': 'off', 'WEB_RESEARCH': 'off', 'NODE_ENV': 'production',
            'CHOPIN_DATABASE_MARKER': 'chopin-pilot/v1:' + secrets.token_hex(24)}


def env_bytes(values: dict) -> bytes:
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str)
           or any(c in value for c in '\r\n\x00$`"\'\\') for key, value in values.items()):
        raise PilotError('Environment file contains unsupported syntax.')
    return ''.join(key + '=' + value + '\n' for key, value in values.items()).encode()


def read_env(config: dict) -> dict:
    raw = read_private(absolute(config['state_dir']) / 'runtime.env')
    lines = raw.decode().splitlines()
    pairs = [line.split('=', 1) for line in lines]
    if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
        raise PilotError('Malformed environment file.')
    value = dict(pairs)
    if env_bytes(value) != raw:
        raise PilotError('Environment file is not canonical.')
    creds = credentials(config)
    expected = environment(config, creds)
    dynamic = {'DATABASE_URL', 'SESSION_ENCRYPTION_KEY', 'CHOPIN_DATABASE_MARKER'}
    if set(value) != set(expected) or any(value[key] != expected[key] for key in set(expected) - dynamic):
        raise PilotError('Runtime environment differs from explicit configuration.')
    from urllib.parse import urlsplit
    url = urlsplit(value['DATABASE_URL'])
    db = config['database']
    if (url.scheme != 'postgresql' or url.hostname != db['host'] or url.port != db['port']
            or url.username != db['role'] or url.path != '/' + db['name'] or url.query or url.fragment
            or not re.fullmatch(r'[A-Za-z0-9_-]{40,60}', url.password or '')
            or not re.fullmatch(r'[a-f0-9]{64}', value['SESSION_ENCRYPTION_KEY'])
            or not re.fullmatch(r'chopin-pilot/v1:[a-f0-9]{48}', value['CHOPIN_DATABASE_MARKER'])):
        raise PilotError('Invalid runtime database or encryption configuration.')
    return value


def prepare(config: dict):
    validate_source(config)
    creds = credentials(config)
    verify_credentials(config, creds)
    state = private_dir(absolute(config['state_dir']))
    if (state / 'runtime.env').exists():
        read_env(config)
        pinned_bun(config)
        return  # No rotation or overwrite on repeat preparation.
    values = environment(config, creds)
    located = run(['npm', 'exec', '--yes', '--package=bun@1.3.2', '--', 'bun', '-p', 'process.execPath']).decode().strip()
    binary = absolute(located)
    if run([str(binary), '--version']).strip() != b'1.3.2':
        raise PilotError('Bun version mismatch.')
    bindir = private_dir(state / 'bin')
    target = bindir / 'bun'
    if not target.exists():
        write_new(target, binary.read_bytes())
        target.chmod(0o700)
    pinned_bun(config)
    write_new(state / 'runtime.env', env_bytes(values))


def pinned_bun(config: dict) -> str:
    binary = absolute(config['state_dir']) / 'bin/bun'
    absolute(str(binary))
    if run([str(binary), '--version']).strip() != b'1.3.2':
        raise PilotError('Prepared Bun version mismatch.')
    return str(binary)


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def database_sql(config: dict, values: dict) -> str:
    from urllib.parse import urlsplit
    db = config['database']
    role, name = literal(db['role']), literal(db['name'])
    marker = literal(values['CHOPIN_DATABASE_MARKER'])
    password = literal(urlsplit(values['DATABASE_URL']).password)
    # One psql session holds a cooperative advisory lock. A foreign object is
    # refused, never adopted or ALTERed. CREATE uses server-side %I/%L quoting.
    return f"""\\set ON_ERROR_STOP on
SET standard_conforming_strings = on;
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SELECT pg_advisory_lock(hashtext('chopin-pilot-database-preparation'));
DO $$ BEGIN
IF current_setting('server_version_num')::int / 10000 <> 17 THEN RAISE EXCEPTION 'PostgreSQL 17 required'; END IF;
IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname={role} AND
 (shobj_description(oid, 'pg_authid') IS DISTINCT FROM {marker} OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR NOT rolcanlogin))
 OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname={role})
THEN RAISE EXCEPTION 'Dedicated role collision'; END IF;
IF EXISTS (SELECT 1 FROM pg_database WHERE datname={name} AND
 (shobj_description(oid, 'pg_database') IS DISTINCT FROM {marker} OR pg_get_userbyid(datdba)<>{role}))
THEN RAISE EXCEPTION 'Dedicated database collision'; END IF;
END $$;
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', {role}, {password}) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={role})
\\gexec
SELECT format('COMMENT ON ROLE %I IS %L', {role}, {marker})
\\gexec
SELECT format('CREATE DATABASE %I OWNER %I', {name}, {role}) WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname={name})
\\gexec
SELECT format('COMMENT ON DATABASE %I IS %L', {name}, {marker})
\\gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', {name})
\\gexec
"""


def pg_env(config: dict, values: dict, *, admin=False) -> dict:
    from urllib.parse import urlsplit
    db = config['database']
    env = {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'PGHOST': '127.0.0.1', 'PGPORT': '5432',
           'PGDATABASE': 'postgres' if admin else db['name'],
           'PGUSER': db['admin_user'] if admin else db['role'], 'PGCONNECT_TIMEOUT': '10',
           'PGPASSFILE': '/dev/null', 'PGSSLMODE': 'disable'}
    if admin and db.get('admin_password_file'):
        env['PGPASSWORD'] = read_private(absolute(db['admin_password_file'])).decode().strip()
    elif not admin:
        env['PGPASSWORD'] = urlsplit(values['DATABASE_URL']).password
    return env


def database(config: dict):
    values = read_env(config)
    run([config['psql'], '-X', '-w', '-q'], data=database_sql(config, values).encode(), env=pg_env(config, values, admin=True))
    run([config['psql'], '-X', '-w', '-Atc', 'SELECT current_user'], env=pg_env(config, values))


def app_env(config: dict) -> dict:
    return {**read_env(config), 'PATH': str(absolute(config['state_dir']) / 'bin') + ':/opt/homebrew/bin:/usr/bin:/bin',
            'HOME': str(Path.home())}


def build(config: dict):
    source, bun = validate_source(config), pinned_bun(config)
    env = app_env(config)
    run([bun, 'install', '--frozen-lockfile'], cwd=source, env=env)
    run([bun, 'run', 'build'], cwd=source, env=env)
    receipt = absolute(config['state_dir']) / 'build.done'
    if receipt.exists():
        if read_private(receipt) != UPSTREAM.encode():
            raise PilotError('Build receipt source mismatch.')
    else:
        write_new(receipt, UPSTREAM.encode())


def migration_fingerprint(config: dict) -> bytes:
    return hashlib.sha256((UPSTREAM + json.dumps(read_env(config), sort_keys=True)).encode()).hexdigest().encode()


def migrate(config: dict):
    source, bun = validate_source(config), pinned_bun(config)
    state = absolute(config['state_dir'])
    fingerprint = migration_fingerprint(config)
    fd = os.open(state / 'migration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = state / 'migration.done'
        if receipt.exists():
            if read_private(receipt) != fingerprint:
                raise PilotError('Migration receipt configuration mismatch.')
            return
        run([bun, str(source / 'apps/server/src/storage/migrate.ts')], cwd=source, env=app_env(config))
        write_new(receipt, fingerprint)


def launch_plist(config: dict) -> bytes:
    state, source = absolute(config['state_dir']), validate_source(config)
    if not (source / 'apps/web/dist/index.html').is_file() or not (state / 'migration.done').is_file():
        raise PilotError('Build and explicit migration must complete first.')
    if (read_private(state / 'migration.done') != migration_fingerprint(config)
            or read_private(state / 'build.done') != UPSTREAM.encode()):
        raise PilotError('Build or migration receipt does not match this deployment.')
    return plistlib.dumps({'Label': LABEL, 'ProgramArguments': [pinned_bun(config),
                           '--env-file=' + str(state / 'runtime.env'), str(source / 'apps/server/src/main.ts')],
                          'WorkingDirectory': str(source), 'EnvironmentVariables': app_env(config),
                          'KeepAlive': True, 'RunAtLoad': True, 'Umask': 0o077,
                          'StandardOutPath': str(state / 'logs/stdout.log'),
                          'StandardErrorPath': str(state / 'logs/stderr.log')})


def install(config: dict):
    values = read_env(config)
    verify_credentials(config, credentials(config))
    database(config)  # Validates ownership before any service activation.
    state = absolute(config['state_dir'])
    logs = private_dir(state / 'logs')
    for name in ('stdout.log', 'stderr.log'):
        if not (logs / name).exists():
            write_new(logs / name, b'')
        read_private(logs / name)
    plist = state / (LABEL + '.plist')
    payload = launch_plist(config)
    if plist.exists():
        if read_private(plist) != payload:
            raise PilotError('Existing launch configuration differs; refusing overwrite.')
    else:
        write_new(plist, payload)
    probe = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'], capture_output=True, check=False)
    if probe.returncode == 0:
        raise PilotError('Launch label already exists; inspect it before reinstalling.')
    with socket.socket() as sock:
        sock.bind((values['SERVER_HOST'], int(values['PORT'])))  # Fail if another service owns 3050.
    agents = absolute(str(Path.home() / 'Library/LaunchAgents'))
    agents.mkdir(parents=True, exist_ok=True)
    info = agents.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PilotError('LaunchAgents directory ownership or permissions are unsafe.')
    installed = agents / plist.name
    # Exclusive hard link keeps credentials private and persists after login.
    # Never replace another job, even one with the same label.
    os.link(plist, installed, follow_symlinks=False)
    run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(installed)])


def health(config: dict):
    read_env(config)
    code, _, _ = request('http://127.0.0.1:3050/api/session')
    if code != 200:
        raise PilotError('HTTP session liveness failed.')
    print('HTTP session liveness only. OAuth, repository admission, MCP, canvas and multi-user meeting remain unverified.')


def backup(config: dict):
    values = read_env(config)
    directory = private_dir(absolute(config['state_dir']) / 'backups')
    path = directory / (time.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(4) + '.dump')
    # Output streams straight to an exclusive private file, never terminal/logs.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        result = subprocess.run([config['pg_dump'], '-w', '-Fc'], stdout=stream, stderr=subprocess.PIPE,
                                timeout=300, env=pg_env(config, values), check=False)
    if result.returncode:
        path.unlink()
        raise PilotError('Backup failed; no usable backup produced.')
    print('Private full database backup saved; restore rehearsal remains required.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=['prepare', 'database', 'build', 'migrate', 'install', 'health', 'backup'])
    args = parser.parse_args()
    try:
        os.umask(0o077)
        config = load_config(args.config)
        globals()[args.action](config)
        print(args.action + ' completed. This does not establish meeting readiness.')
    except PilotError as exc:
        parser.exit(1, str(exc) + '\n')
    except Exception:
        parser.exit(1, 'Operator action refused or failed; sensitive details suppressed.\n')


if __name__ == '__main__':
    main()
