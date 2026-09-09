"""Small owner-only file and no-redirect HTTP primitives for the optional pilot."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import urllib.request


class PilotError(Exception):
    """Only static, nonsecret messages may cross the operator/chat boundary."""


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise PilotError('An absolute normalized path is required.')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PilotError('Symlink paths are refused.')
    return path


def private_dir(path: Path) -> Path:
    path = absolute(str(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PilotError('Directory must be owned by this user with mode 0700.')
    return path


def read_private(path: Path) -> bytes:
    absolute(str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PilotError('Private file must be owned by this user with mode 0600.')
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise PilotError('Private file exceeds size limit.')
    return data


def write_new(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_config(path: str) -> dict:
    value = json.loads(read_private(absolute(path)))
    if not isinstance(value, dict) or value.get('schema') != 'chopin-pilot/v1':
        raise PilotError('Unsupported pilot configuration.')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PilotError('HTTP redirects are refused.')


def request(url: str, *, data: bytes | None = None, headers: dict | None = None,
            method: str | None = None) -> tuple[int, dict, bytes]:
    """No proxies, redirects, response/error-body logging, or unbounded reads."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, data=data, headers=headers or {}, method=method), timeout=25) as reply:
            body = reply.read(2_000_001)
            if len(body) > 2_000_000:
                raise PilotError('HTTP response exceeds size limit.')
            return reply.status, {k.lower(): v for k, v in reply.headers.items()}, body
    except Exception:
        raise PilotError('HTTP request failed; remote details suppressed.') from None
