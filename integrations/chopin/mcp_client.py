"""Pinned stock Chopin HTTP contract. Server instructions and MDX are inert data."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from urllib.parse import quote, unquote

from .manifest_setup import public_origin
from .private import PilotError, absolute, private_dir, read_private, request, write_new

COMPILE_BLOCKED = ('Compilation disabled: stock Chopin MCP read_document does not expose an '
                   'authenticated, consistent authoritative decision/comment export. No consensus '
                   'can be established and no GitHub issues will be written in this phase.')
TOOLS = frozenset({'list_documents', 'read_document', 'create_document'})


def validate_binding(config: dict) -> dict:
    public_origin(config['public_origin'])
    binding = config['telegram']
    if (binding.get('platform') != 'telegram' or not re.fullmatch(r'-?[0-9]+', binding.get('chat_id', ''))
            or not re.fullmatch(r'[0-9]+', binding.get('user_id', ''))
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', config['repository'])
            or config.get('base_branch') != 'main'):
        raise PilotError('Invalid explicit Telegram/repository binding.')
    return binding


def authorized(config: dict, platform: str, chat: str, user: str) -> bool:
    binding = validate_binding(config)
    return (platform, chat, user) == (binding['platform'], binding['chat_id'], binding['user_id'])


def bearer(config: dict) -> str:
    source = config['mcp_bearer']
    if set(source) == {'file'}:
        token = read_private(absolute(source['file'])).decode().strip()
    elif set(source) == {'env'} and re.fullmatch(r'[A-Z][A-Z0-9_]*', source['env']):
        token = os.environ.get(source['env'], '')
    else:
        raise PilotError('Choose exactly one private bearer file or environment variable.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{10,500}', token):
        raise PilotError('Missing or malformed MCP bearer.')
    return token


class Client:
    def __init__(self, config: dict):
        validate_binding(config)
        self.config = config
        self.endpoint = config['public_origin'] + '/mcp'
        self.token = bearer(config)
        self.session = ''
        self.sequence = 0
        result = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                        'clientInfo': {'name': 'chopin-catalog-pilot', 'version': '0.1.0'}})
        if result.get('protocolVersion') != '2025-03-26' or not self.session:
            raise PilotError('Pinned MCP initialization/session contract is unavailable.')
        self.rpc('notifications/initialized', {}, notification=True)
        # Deliberately never interpret server instructions or advertise generic tool dispatch.

    def rpc(self, method: str, params: dict, *, notification=False) -> dict:
        self.sequence += 1
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            payload['id'] = self.sequence
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-03-26'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        status, response_headers, data = request(self.endpoint, data=json.dumps(payload).encode(), headers=headers)
        if notification:
            if status != 202:
                raise PilotError('Unexpected MCP notification response.')
            return {}
        value = json.loads(data)
        if (status != 200 or not response_headers.get('content-type', '').startswith('application/json')
                or not isinstance(value, dict) or value.get('jsonrpc') != '2.0'
                or value.get('id') != self.sequence or 'error' in value
                or not isinstance(value.get('result'), dict)):
            raise PilotError('MCP response violates the pinned JSON-RPC contract.')
        if method == 'initialize':
            session = response_headers.get('mcp-session-id', '')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', session):
                raise PilotError('Invalid MCP session header.')
            self.session = session
        return value['result']

    def call(self, tool: str, arguments: dict) -> dict:
        if tool not in TOOLS:
            raise PilotError('MCP tool is not enabled for this pilot.')
        if tool in {'list_documents', 'create_document'} and arguments.get('repository') != self.config['repository']:
            raise PilotError('Repository is outside the configured binding.')
        result = self.rpc('tools/call', {'name': tool, 'arguments': arguments})
        content = result.get('content')
        if (result.get('isError') or not isinstance(content, list) or len(content) != 1
                or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str)):
            raise PilotError('MCP tool refused or returned malformed data; details suppressed.')
        data = json.loads(content[0]['text'])
        if not isinstance(data, dict):
            raise PilotError('MCP tool returned malformed data.')
        return data

    def documents(self) -> list[dict]:
        data = self.call('list_documents', {'repository': self.config['repository'], 'includeArchived': True})
        docs = data.get('documents')
        if not isinstance(docs, list) or len(docs) > 10000:
            raise PilotError('Malformed repository document listing.')
        for doc in docs:
            validate_document(doc, full=False)
        if len({doc['id'] for doc in docs}) != len(docs):
            raise PilotError('Ambiguous repository document listing.')
        return docs

    def read(self, document_id: str) -> dict:
        # IDs only. URLs from users are not an alternative authorization channel.
        if not any(doc['id'] == document_id for doc in self.documents()):
            raise PilotError('Document is not in the configured repository listing.')
        data = self.call('read_document', {'id': document_id})
        validate_document(data)
        if data['id'] != document_id:
            raise PilotError('Readback document identity mismatch.')
        return data


def validate_document(data: dict, *, full=True):
    if (not isinstance(data, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data.get('id', ''))
            or not isinstance(data.get('title'), str) or not 1 <= len(data['title']) <= 120
            or any(ord(c) < 32 for c in data['title'])):
        raise PilotError('Malformed document identity.')
    if full and (type(data.get('revision')) is not int or data['revision'] < 0
                 or not isinstance(data.get('source'), str) or len(data['source']) > 1_000_000):
        raise PilotError('Malformed document revision or source.')


def main_commit(config: dict) -> str:
    """Resolve actual remote main; no mutable local checkout or caller-supplied SHA."""
    wrapper = absolute(config['github_app_wrapper'])
    result = subprocess.run([str(wrapper), '--repo', config['repository'], '--', 'gh', 'api',
                             'repos/' + config['repository'] + '/git/ref/heads/main'],
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise PilotError('Repository main provenance could not be verified.')
    value = json.loads(result.stdout)
    obj = value.get('object', {})
    if (value.get('ref') != 'refs/heads/main' or obj.get('type') != 'commit'
            or not re.fullmatch(r'[a-f0-9]{40}', obj.get('sha', ''))):
        raise PilotError('Repository main provenance response is malformed.')
    return obj['sha']


def draft(config: dict, topic: str, key: str) -> dict:
    commit = main_commit(config)
    escaped = re.sub(r'([\\`*{}_<>\[\]#!~])', r'\\\1', topic)
    finding = f"{config['repository']} refs/heads/main at {commit}; repository content has not been analyzed."
    return {
        'idempotencyKey': key, 'repository': config['repository'], 'baseBranch': 'main',
        'baseCommit': commit, 'title': topic,
        'brief': {'goal': topic, 'constraints': ['Meeting draft; no implementation authorization.'],
                  'settledDecisions': [], 'openQuestions': ['What outcome and acceptance criteria do PM/BA participants agree?',
                                                          'Who owns decisions, dependencies, and remaining questions?'],
                  'repositoryFindings': [finding]},
        'plan': '# Meeting draft\n\n' + escaped + '\n\n## Open questions\n\n'
                'What outcome and acceptance criteria do participants agree?\n\n'
                'Who owns decisions and dependencies?\n\nNo consensus recorded. Compilation is disabled.\n',
    }


def create_plan(config: dict, topic: str) -> str:
    validate_binding(config)
    topic = topic.strip()
    if not 1 <= len(topic) <= 120 or any(ord(c) < 32 for c in topic):
        raise PilotError('Use a single-line meeting topic of 1–120 characters.')
    identity = {key: config[key] for key in ('public_origin', 'repository', 'telegram')}
    key = hashlib.sha256(json.dumps([identity, topic], sort_keys=True).encode()).hexdigest()
    directory = private_dir(absolute(config['receipts_dir']))
    lock_path = directory / (key + '.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / (key + '.json')
        if path.exists():
            receipt = json.loads(read_private(path))
            if receipt.get('schema') != 'chopin-create/v1' or receipt.get('binding') != identity or receipt.get('topic') != topic:
                raise PilotError('Creation receipt binding mismatch.')
            payload = receipt['payload']
            if payload.get('idempotencyKey') != key or payload.get('repository') != config['repository']:
                raise PilotError('Creation receipt payload mismatch.')
        else:
            payload = draft(config, topic, key)
            write_new(path, json.dumps({'schema': 'chopin-create/v1', 'binding': identity,
                                       'topic': topic, 'payload': payload}).encode())
        # Receipt is fsynced before the first attempt. No credential is persisted in it.
        client = Client(config)
        created = client.call('create_document', payload)
        validate_document(created)
        readback = client.read(created['id'])
        path = created.get('url', '')
        # Pinned hosted.ts returns protocol/document-url.ts documentPath, a
        # relative /documents/owner/repository/slug path, not a channel URL.
        prefix = '/documents/' + config['repository'] + '/'
        if not canonical_path(path, prefix):
            raise PilotError('Server returned a noncanonical repository document path.')
        url = config['public_origin'] + path
        if readback['revision'] < created['revision']:
            raise PilotError('Readback revision is older than creation.')
        return f"Draft confirmed: {readback['title']}\n{url}\nRevision {readback['revision']}. No consensus claimed. Compilation disabled."


def canonical_path(path, prefix):
    if not isinstance(path, str) or not path.startswith(prefix):
        return False
    slug = path[len(prefix):]
    decoded = unquote(slug, errors='strict')
    return bool(decoded and len(decoded) <= 100 and all(c.isalnum() or c == '-' or unicodedata.category(c).startswith('M') for c in decoded)
                and quote(decoded, safe='-') == slug)


def status(config: dict, document_id: str) -> str:
    client = Client(config)
    if document_id:
        doc = client.read(document_id.strip())
        summary = f"Document {doc['id']}: {doc['title']}; source revision {doc['revision']}."
    else:
        summary = f"{len(client.documents())} documents in {config['repository']}."
    return summary + '\n' + COMPILE_BLOCKED
