"""One-shot private GitHub manifest registration. Never serve on the public Funnel."""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .private import PilotError, absolute, load_config, private_dir, request, write_new

PERMISSIONS = dict.fromkeys(('contents', 'pull_requests', 'checks', 'statuses', 'metadata'), 'read')


def public_origin(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment or value != f'https://{url.netloc}'):
        raise PilotError('Public origin must be an exact HTTPS origin without a path.')
    return value


def setup_address(config: dict, *, verify_host: bool = False) -> tuple[str, int]:
    origin = config['setup_origin']
    url = urlsplit(origin)
    address = ipaddress.ip_address(url.hostname or '')
    if (url.scheme != 'http' or url.username or url.password or url.path or url.query or url.fragment
            or address.version != 4 or not url.port or origin != f'http://{address}:{url.port}'
            or not (str(address) == '127.0.0.1' or address in ipaddress.ip_network('100.64.0.0/10'))):
        raise PilotError('Setup must bind exact IPv4 loopback or this host Tailscale address.')
    if verify_host and not address.is_loopback:
        result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, timeout=10, check=True)
        if result.stdout.decode().strip() != str(address):
            raise PilotError('Configured setup bind is not this host Tailscale address.')
    return str(address), url.port


def manifest(config: dict) -> dict:
    origin = public_origin(config['public_origin'])
    setup_address(config)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 -]{2,80}', config['app_name']):
        raise PilotError('Invalid App name.')
    return {
        'name': config['app_name'], 'url': origin,
        'redirect_url': config['setup_origin'] + '/manifest/callback',
        'callback_urls': [origin + '/auth/github/callback'],
        'setup_url': origin + '/auth/github/setup', 'public': True,
        'request_oauth_on_install': False,
        'hook_attributes': {'url': origin, 'active': False},
        'default_events': [], 'default_permissions': PERMISSIONS,
    }


def validate_app(data: dict, config: dict) -> dict:
    """Validate required reported identity/permissions and any reported settings.

    GitHub's conversion response omits some registration settings. Those are
    explicitly a browser verification step, never inferred as verified.
    """
    expected = manifest(config)
    if (not isinstance(data, dict) or not isinstance(data.get('owner'), dict)
            or data['owner'].get('login', '').lower() != config['app_owner'].lower()
            or data.get('name') != config['app_name'] or data.get('permissions') != PERMISSIONS
            or data.get('events') != [] or not isinstance(data.get('id'), int)
            or not re.fullmatch(r'[a-z0-9-]+', data.get('slug', ''))
            or data.get('html_url') != 'https://github.com/apps/' + data.get('slug', '')
            or data.get('external_url') != config['public_origin']):
        raise PilotError('Converted App identity or read-only policy does not match.')
    for key in ('public', 'request_oauth_on_install', 'callback_urls', 'setup_url', 'redirect_url'):
        if key in data and data[key] != expected[key]:
            raise PilotError('Reported App settings do not match.')
    hook = data.get('hook_attributes')
    if hook is not None and hook != expected['hook_attributes']:
        raise PilotError('Reported webhook settings do not match.')
    return data


def converted_credentials(data: dict, config: dict) -> dict:
    validate_app(data, config)
    if (not re.fullmatch(r'[A-Za-z0-9_.-]{6,100}', data.get('client_id', ''))
            or not re.fullmatch(r'[A-Za-z0-9_\-]{20,200}', data.get('client_secret', ''))
            or not isinstance(data.get('pem'), str) or 'PRIVATE KEY-----' not in data['pem']):
        raise PilotError('Conversion did not return usable credentials.')
    return {key: data[key] for key in ('id', 'slug', 'client_id', 'client_secret', 'pem', 'owner')}


def convert(code: str) -> dict:
    _, _, body = request('https://api.github.com/app-manifests/' + code + '/conversions',
                         data=b'', headers={'Accept': 'application/vnd.github+json',
                                            'X-GitHub-Api-Version': '2022-11-28'})
    return json.loads(body)


class SetupServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, config: dict, *, converter=convert, verify_host: bool = True):
        address = setup_address(config, verify_host=verify_host)
        self.manifest = manifest(config)
        self.config = config
        self.converter = converter
        self.capability = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(32)
        self.browser = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.used = False
        ttl = config.get('setup_ttl_seconds', 600)
        if type(ttl) is not int or not 30 <= ttl <= 900:
            raise PilotError('Setup lifetime must be 30–900 seconds.')
        self.deadline = time.monotonic() + ttl
        self.destination = absolute(config['credentials_dir'])
        if self.destination.exists():
            raise PilotError('Credential destination already exists; refusing overwrite.')
        private_dir(self.destination.parent)
        super().__init__(address, SetupHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def handle_error(self, request, client_address):
        pass  # Tracebacks could contain a callback code or credential response.


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, body: str, *, cookie=False):
        payload = body.encode()
        self.send_response(status)
        for key, value in {
            'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(payload)),
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; form-action https://github.com; base-uri 'none'; frame-ancestors 'none'",
        }.items():
            self.send_header(key, value)
        if cookie:
            self.send_header('Set-Cookie', f'chopin_setup={self.server.browser}; HttpOnly; SameSite=Lax; Path=/manifest/callback; Max-Age=900')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            self.route()
        except Exception:
            self.reply(500, 'Setup failed. Details suppressed; this attempt cannot be retried after conversion begins.')

    def route(self):
        server = self.server
        if self.headers.get_all('Host') != [urlsplit(server.config['setup_origin']).netloc]:
            return self.reply(403, 'Host refused.')
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment or not self.path.startswith('/'):
            return self.reply(400, 'Invalid target.')
        if time.monotonic() >= server.deadline:
            return self.reply(410, 'Setup expired.')
        if target.path == '/setup/' + server.capability and not target.query:
            if server.used:
                return self.reply(409, 'Setup consumed.')
            action = 'https://github.com/settings/apps/new?' + urlencode({'state': server.state})
            return self.reply(200, '<form method="post" action="' + html.escape(action, quote=True)
                              + '"><input type="hidden" name="manifest" value="'
                              + html.escape(json.dumps(server.manifest), quote=True)
                              + '"><button>Create Chopin Catalog App</button></form>', cookie=True)
        if target.path != '/manifest/callback':
            return self.reply(404, 'Not found.')
        self.callback(target.query)

    def callback(self, query: str):
        server = self.server
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        if set(fields) != {'code', 'state'} or any(len(v) != 1 for v in fields.values()):
            return self.reply(400, 'Invalid callback.')
        if (not secrets.compare_digest(fields['state'][0], server.state)
                or self.headers.get('Cookie') != 'chopin_setup=' + server.browser):
            return self.reply(403, 'Invalid callback.')
        code = fields['code'][0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,512}', code):
            return self.reply(400, 'Invalid callback.')
        with server.lock:
            if server.used or time.monotonic() >= server.deadline:
                return self.reply(409, 'Setup consumed or expired.')
            server.used = True  # Even ambiguous conversion failure is terminal.
            server.destination.mkdir(mode=0o700)  # Exclusive reservation, never overwrite.
            credentials = converted_credentials(server.converter(code), server.config)
            write_new(server.destination / 'credentials.json', json.dumps(credentials).encode())
        self.reply(200, 'Credentials saved privately. Verify expiring user tokens ON, device flow OFF, public installation, no webhooks, and exact callback/setup URLs in GitHub App settings before installation. No service was started.')

    def do_POST(self):
        self.reply(405, 'Method refused.')

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_HEAD = do_POST
    do_OPTIONS = do_POST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        with SetupServer(config) as server:
            # Terminal-only one-time capability, never log it in a shared service.
            print(config['setup_origin'] + '/setup/' + server.capability, flush=True)
            server.timeout = 1
            while time.monotonic() < server.deadline and not server.used:
                server.handle_request()
            # A worker may still be converting. Join it by taking its lock.
            with server.lock:
                pass
    except Exception:
        parser.exit(1, 'Setup refused or failed; sensitive details suppressed.\n')


if __name__ == '__main__':
    main()
