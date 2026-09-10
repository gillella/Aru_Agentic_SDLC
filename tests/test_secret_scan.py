from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

VERIFY = Path(__file__).resolve().parents[1] / "templates/verify.sh"
GIT = shutil.which("git")


def git(repo, *args):
    return subprocess.run(
        [GIT, "-c", "core.fsmonitor=false", *args], cwd=repo,
        capture_output=True, check=True,
    )


def repository(tmp_path, mode, content=b"safe content\n"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "core.filemode", "true")
    sample = repo / "sample.txt"
    sample.write_bytes(b"base content\n")
    git(repo, "add", "sample.txt")
    git(repo, "commit", "-m", "base")
    if mode == "diff":
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sample.write_bytes(content)
    git(repo, "add", "sample.txt")
    git(repo, "commit", "-m", "candidate")
    return repo


def scan(repo, env=None):
    # These fixtures isolate scanner behavior; consumer execution is covered
    # separately with an actual failing and passing application check.
    consumer = repo / ".aru/verify-project.sh"
    consumer.parent.mkdir(exist_ok=True)
    consumer.write_text("#!/usr/bin/env bash\nexit 0\n")
    consumer.chmod(0o755)
    return subprocess.run(
        ["bash", str(VERIFY)], cwd=repo, env=env,
        capture_output=True, text=True, check=False,
    )


def fault_environment(tmp_path, fault, status=2):
    """Fault only scan content/grep calls; scope discovery still uses real Git."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "fault-seen"
    real = {"git": GIT, "grep": shutil.which("grep")}
    script = fr"""#!{sys.executable}
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
fault = {fault!r}
content = name == 'git' and ('diff' in args and '-a' in args or 'grep' in args)
filtering = name == 'grep' and args[:3] == ['-a', '-E', r'^\+']
matching = name == 'grep' and '-Eq' in args
if (fault == 'git' and content or fault == 'filter' and filtering
        or fault in ('match', 'read') and matching):
    Path({str(seen)!r}).write_text('fault exercised')
    if fault == 'read':
        Path(args[-1]).unlink()  # Force a real grep input-read error.
    else:
        print('synthetic scan failure', file=sys.stderr)
        print('safe partial output')
        sys.exit({status})
real = {real!r}[name]
os.execv(real, [real, *args])
"""
    for name in real:
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)
    return {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}, seen


@pytest.mark.parametrize("mode", ["diff", "tree"])
def test_content_collection_failure_cannot_hide_a_credential(tmp_path, mode):
    secret = ("API_SECRET_KEY=" + "A" * 32 + "\n").encode()
    repo = repository(tmp_path, mode, secret)
    control = scan(repo)
    assert control.returncode == 1
    assert "credential-shaped literal found" in control.stderr

    env, seen = fault_environment(tmp_path, "git")
    result = scan(repo, env)
    assert seen.exists()
    assert result.returncode != 0
    assert "secret-scan content is unavailable" in result.stderr
    assert "no credential-shaped literal found" not in result.stdout


@pytest.mark.parametrize("mode,fault,status", [
    ("diff", "filter", 2),
    ("diff", "match", 2),
    ("tree", "match", 2),
    ("diff", "match", 127),
    ("tree", "match", 127),
    ("diff", "read", 2),
    ("tree", "read", 2),
])
def test_scanner_errors_are_not_clean_results(tmp_path, mode, fault, status):
    repo = repository(tmp_path, mode)
    env, seen = fault_environment(tmp_path, fault, status)
    result = scan(repo, env)
    assert seen.exists()
    assert result.returncode != 0
    assert "secret-scan" in result.stderr
    assert "no credential-shaped literal found" not in result.stdout


@pytest.mark.parametrize("mode", ["diff", "tree"])
def test_clean_content_passes(tmp_path, mode):
    result = scan(repository(tmp_path, mode))
    assert result.returncode == 0, result.stderr
    assert "no credential-shaped literal found" in result.stdout


def test_empty_tracked_content_is_a_valid_no_match(tmp_path):
    result = scan(repository(tmp_path, "tree", b""))
    assert result.returncode == 0, result.stderr
    assert "no credential-shaped literal found" in result.stdout


@pytest.mark.parametrize("local_change", ["replace", "delete"])
def test_tree_scan_reads_committed_content_despite_local_changes(tmp_path, local_change):
    secret = ("API_SECRET_KEY=" + "A" * 32 + "\n").encode()
    repo = repository(tmp_path, "tree", secret)
    sample = repo / "sample.txt"
    if local_change == "replace":
        sample.write_text("safe local content\n")
        git(repo, "add", "sample.txt")
    else:
        sample.unlink()
    result = scan(repo)
    assert result.returncode == 1
    assert "credential-shaped literal found" in result.stderr
    assert "no credential-shaped literal found" not in result.stdout


@pytest.mark.parametrize("change", ["mode", "deletion"])
def test_diff_without_added_content_passes(tmp_path, change):
    repo = repository(tmp_path, "diff")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sample = repo / "sample.txt"
    if change == "mode":
        sample.chmod(0o755)
    else:
        sample.unlink()
    git(repo, "add", "sample.txt")
    git(repo, "commit", "-m", change)
    result = scan(repo)
    assert result.returncode == 0, result.stderr
    assert "no credential-shaped literal found" in result.stdout
