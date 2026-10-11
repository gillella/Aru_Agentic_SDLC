#!/usr/bin/env python3
"""Build deterministic public-source package; never reads credentials or installs."""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

NAMES = ('request.py', 'security.py', 'app.py', 'model.py', 'validation.py', 'client.py', 'install.py')


def build(repository, head, client, client_hash, destination):
    if not re.fullmatch('[0-9a-f]{40}', head) or not re.fullmatch('[0-9a-f]{64}', client_hash):
        raise ValueError('Full immutable source/client hashes required')
    payload = {}
    for name in NAMES:
        payload[name] = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(repository),
            'show', head + ':integrations/review-authority/' + name], capture_output=True,
            check=True, timeout=30).stdout
    raw = Path(client).read_bytes()
    if len(raw) > 256 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != client_hash:
        raise ValueError('Model client differs from explicitly selected public binary')
    payload['agy'] = raw
    manifest = {'version': 1, 'source_head': head,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload['manifest.json'] = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    # Exclusive output preserves earlier candidate packages.
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(payload):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name == 'agy' else 0o100644) << 16
            archive.writestr(info, payload[name])
    return hashlib.sha256(Path(destination).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--source-head', required=True)
    parser.add_argument('--client-binary', type=Path, required=True)
    parser.add_argument('--client-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({'package_sha256': build(args.repository, args.source_head, args.client_binary,
                                              args.client_sha256, args.output),
                      'source_head': args.source_head, 'installed': False, 'enabled': False}))


if __name__ == '__main__':
    main()
