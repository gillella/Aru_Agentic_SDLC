"""One-repository App connection, used only by the protected request."""
import base64
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from security import Refusal, parse_json, private_directory, read_file

APP_ID = 5268645
APP_SLUG = 'arureviewauthority'
PERMISSIONS = {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'}


class Connection:
    def __init__(self, root, config, pr):
        self.root, self.config, self.pr = root, config, pr
        self.repository = config['repository']
        self.token = None
        self.issue = None

    def allowed(self, path, method):
        split = urllib.parse.urlsplit(path)
        if split.scheme or split.netloc or split.fragment:
            return False
        prefix = '/repos/' + self.repository
        value = split.path
        if method == 'GET' and value == prefix + f'/pulls/{self.pr}':
            return not split.query
        if method == 'GET' and value in (prefix + f'/pulls/{self.pr}/files',
                                         prefix + f'/pulls/{self.pr}/reviews'):
            query = urllib.parse.parse_qs(split.query, strict_parsing=True)
            return (set(query) == {'per_page', 'page'} and query['per_page'] == ['100']
                    and len(query['page']) == 1 and re.fullmatch(r'[1-9]|10', query['page'][0]))
        if method == 'GET' and value.startswith(prefix + '/contents/'):
            query = urllib.parse.parse_qs(split.query, strict_parsing=True)
            decoded = urllib.parse.unquote(value[len(prefix + '/contents/'):])
            return (safe_path(decoded) and set(query) == {'ref'} and len(query['ref']) == 1
                    and re.fullmatch('[0-9a-f]{40}', query['ref'][0]))
        if split.query:
            return False
        if value == prefix + f'/pulls/{self.pr}/reviews':
            return method == 'POST'
        if method == 'GET' and self.issue is not None and value == prefix + f'/issues/{self.issue}':
            return True
        if re.fullmatch(r'/app/installations/[1-9][0-9]*/access_tokens', value):
            return method == 'POST'
        if value == '/installation/token':
            return method == 'DELETE'
        return method == 'GET' and value in ('/app', '/installation/repositories', prefix + '/installation')

    def api(self, path, token, method='GET', payload=None):
        if not self.allowed(path, method):
            raise Refusal('outside-review-scope')
        request = urllib.request.Request('https://api.github.com' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'AruReviewAuthority'}, method=method)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise Refusal('authority-unavailable')

        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise Refusal('oversized-github-evidence')
                return parse_json(raw) if raw else None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise Refusal('authority-unavailable') from error

    def authenticate(self):
        private_directory(self.root / 'credentials')
        # Descriptor validation precedes openssl; only root can replace this chain.
        read_file(self.root / 'credentials/reviewer.pem', private=True, limit=20000)
        def encode(value):
            return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
        now = int(time.time())
        unsigned = encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()) + '.' + encode(
            json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': APP_ID}).encode())
        result = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign',
                                 str(self.root / 'credentials/reviewer.pem')], input=unsigned.encode(),
                                capture_output=True, check=True, timeout=15)
        jwt = unsigned + '.' + encode(result.stdout)
        app = self.api('/app', jwt)
        if app.get('id') != APP_ID or app.get('slug') != APP_SLUG or permissions(app.get('permissions')) != PERMISSIONS:
            raise Refusal('wrong-review-app')
        install = self.api('/repos/' + self.repository + '/installation', jwt)
        if (install.get('app_id') != APP_ID or install.get('account', {}).get('login') != 'gillella'
                or permissions(install.get('permissions')) != PERMISSIONS or install.get('repository_selection') != 'selected'):
            raise Refusal('wrong-review-installation')
        issued = self.api('/app/installations/' + str(install['id']) + '/access_tokens', jwt, 'POST',
                          {'repositories': ['Aru_Agentic_SDLC'], 'permissions': PERMISSIONS})
        token = issued.get('token')
        if not isinstance(token, str) or not token:
            raise Refusal('authority-unavailable')
        self.token = token
        if permissions(issued.get('permissions')) != PERMISSIONS:
            raise Refusal('wrong-review-token-scope')
        repos = self.api('/installation/repositories', token)
        if (repos.get('total_count') != 1 or len(repos.get('repositories', [])) != 1
                or repos['repositories'][0].get('full_name') != self.repository
                or repos['repositories'][0].get('permissions', {}).get('push') is not True):
            raise Refusal('wrong-review-token-scope')

    def revoke(self):
        if self.token:
            try:
                self.api('/installation/token', self.token, 'DELETE')
            except Exception:
                pass
            finally:
                self.token = None

    def pr_record(self, head, base=None):
        value = self.api(f'/repos/{self.repository}/pulls/{self.pr}', self.token)
        if (not isinstance(value, dict) or value.get('number') != self.pr or value.get('state') != 'open'
                or value.get('head', {}).get('sha') != head or value.get('draft') is not False
                or (base is not None and value.get('base', {}).get('sha') != base)):
            raise Refusal('stale-or-ineligible-pr')
        for ref in ('head', 'base'):
            if (value.get(ref, {}).get('repo', {}).get('full_name') != self.repository
                    or not re.fullmatch('[0-9a-f]{40}', value.get(ref, {}).get('sha', ''))):
                raise Refusal('outside-review-scope')
        if value.get('user', {}).get('login', '').lower() in (APP_SLUG + '[bot]', 'app/' + APP_SLUG):
            raise Refusal('self-review')
        body = value.get('body') or ''
        targets = re.findall(r'^Closes #([1-9][0-9]*)\s*$', body, re.MULTILINE)
        if len(targets) != 1 or int(targets[0]) not in self.config['issues']:
            raise Refusal('outside-issue-scope')
        current_issue = int(targets[0])
        if self.issue is not None and self.issue != current_issue:
            raise Refusal('issue-scope-changed')
        self.issue = current_issue
        return value

    def pages(self, suffix):
        result = []
        for page in range(1, 11):
            part = self.api(f'/repos/{self.repository}/pulls/{self.pr}/{suffix}?per_page=100&page={page}', self.token)
            if not isinstance(part, list) or any(not isinstance(item, dict) for item in part):
                raise Refusal('malformed-github-evidence')
            result.extend(part)
            if len(part) < 100:
                return result
        raise Refusal('truncated-github-evidence')

    def source(self, path, revision):
        if not safe_path(path):
            raise Refusal('unsafe-source-path')
        value = self.api('/repos/' + self.repository + '/contents/' + urllib.parse.quote(path, safe='/') +
                         '?ref=' + revision, self.token)
        if value.get('type') != 'file' or value.get('encoding') != 'base64' or value.get('path') != path:
            raise Refusal('unavailable-source')
        data = base64.b64decode(value['content'], validate=False)
        if len(data) > 1024 * 1024:
            raise Refusal('oversized-source')
        return data.decode('utf-8')

    def publish(self, bundle, receipt):
        self.pr_record(bundle['head'], bundle['base'])
        result = receipt['result']
        decision = result['decision']
        if decision not in ('APPROVE', 'REQUEST_CHANGES') or result['limitations']:
            raise Refusal('incomplete-review')
        if decision == 'APPROVE' and result['findings']:
            raise Refusal('contradictory-review')
        lines = ['Independent Antigravity review / ' + self.config['model'],
                 'Exact head: `' + bundle['head'] + '`', 'Base: `' + bundle['base'] + '`',
                 'Session: `' + receipt['conversation_id'] + '`',
                 'Transcript SHA-256: `' + receipt['transcript_sha256'] + '`', '', result['summary']]
        for finding in result['findings']:
            priority = {'high': 'P1', 'medium': 'P2', 'low': 'P3'}[finding['severity']]
            lines.append(f"\n[{priority}] `{finding['path']}:{finding['line']}` — {finding['body']}")
        for item in result['resolutions']:
            lines.extend(['', 'Resolves review: ' + str(item['review_id']), item['evidence']])
        lines.append('\nThe reviewer executed no tests or PR code. This review does not authorize merge.')
        comments = inline_findings(bundle, result['findings'])
        posted = self.api(f'/repos/{self.repository}/pulls/{self.pr}/reviews', self.token, 'POST',
                          {'commit_id': bundle['head'], 'event': decision, 'body': '\n'.join(lines),
                           'comments': comments})
        expected = {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED'}[decision]
        if (posted.get('commit_id') != bundle['head'] or posted.get('state') != expected
                or posted.get('user', {}).get('login', '').lower() != APP_SLUG + '[bot]'
                or type(posted.get('id')) is not int or posted['id'] <= 0):
            raise Refusal('uncertain-publication')
        self.pr_record(bundle['head'], bundle['base'])
        return {'status': 'reviewed', 'pr': self.pr, 'head': bundle['head'],
                'decision': expected, 'review_id': posted['id']}


def safe_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 1000 and not path.startswith('/')
            and all(part not in ('', '.', '..', '.git') for part in path.split('/'))
            and not any(ord(c) < 32 or c in '\\?#' for c in path))


def permissions(value):
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if item != 'none'}


def inline_findings(bundle, findings):
    comments = []
    for finding in findings:
        patch = next((value for value in bundle.get('diff', []) if value['path'] == finding['path']), None)
        if patch is None:
            continue  # Context findings remain explicit in the submitted summary.
        side = 'LEFT' if patch['status'] == 'removed' else 'RIGHT'
        left, right, positions = 0, 0, set()
        for line in patch['patch'].splitlines():
            hunk = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            if hunk:
                left, right = map(int, hunk.groups())
            elif line.startswith(' '):
                positions.add(left if side == 'LEFT' else right)
                left, right = left + 1, right + 1
            elif line.startswith('-'):
                if side == 'LEFT':
                    positions.add(left)
                left += 1
            elif line.startswith('+'):
                if side == 'RIGHT':
                    positions.add(right)
                right += 1
        if finding['line'] in positions:
            priority = {'high': 'P1', 'medium': 'P2', 'low': 'P3'}[finding['severity']]
            comments.append({'path': finding['path'], 'line': finding['line'], 'side': side,
                             'body': '[' + priority + '] ' + finding['body']})
    return comments
