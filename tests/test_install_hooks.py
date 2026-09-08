from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_hooks.sh"


def repository(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "consumer"
    target.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True, capture_output=True)
    hooks = target / ".git" / "hooks"
    return target, hooks


def install(target: Path) -> None:
    subprocess.run([str(INSTALLER)], cwd=target, check=True, capture_output=True, text=True)


def install_result(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALLER)],
        cwd=target,
        check=False,
        capture_output=True,
        text=True,
    )


def test_installer_replaces_legacy_aru_hook_without_chaining_it(tmp_path):
    target, hooks = repository(tmp_path)
    legacy = hooks / "pre-push"
    legacy.write_text(
        "#!/usr/bin/env bash\n# Aru_Agentic_SDLC pre-push hook: legacy\n",
        encoding="utf-8",
    )
    legacy.chmod(0o755)

    install(target)

    assert "direct pushes to the default branch" in legacy.read_text(encoding="utf-8")
    assert (hooks / "touches.py").is_file()
    assert 'enforcer="${hook_dir}/enforce_touches.py"' in legacy.read_text(
        encoding="utf-8"
    )
    assert not (hooks / "pre-push.pre-aru").exists()


def test_installer_preserves_and_chains_a_user_owned_hook(tmp_path):
    target, hooks = repository(tmp_path)
    custom = hooks / "pre-push"
    custom.write_text("#!/usr/bin/env bash\necho user-hook\n", encoding="utf-8")
    custom.chmod(0o755)

    install(target)

    preserved = hooks / "pre-push.pre-aru"
    assert preserved.read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho user-hook\n"
    assert "pre-push.pre-aru" in custom.read_text(encoding="utf-8")


def test_installer_refuses_external_core_hooks_path(tmp_path):
    target, _hooks = repository(tmp_path)
    outside = tmp_path / "outside-hooks"
    subprocess.run(
        ["git", "config", "core.hooksPath", str(outside)],
        cwd=target,
        check=True,
    )

    result = install_result(target)

    assert result.returncode != 0
    assert "non-canonical core.hooksPath" in result.stderr
    assert not outside.exists()


def test_installer_refuses_symlink_hooks_directory(tmp_path):
    target, hooks = repository(tmp_path)
    outside = tmp_path / "outside-hooks"
    outside.mkdir()
    shutil.rmtree(hooks)
    hooks.symlink_to(outside, target_is_directory=True)

    result = install_result(target)

    assert result.returncode != 0
    assert "symbolic-link hooks directory" in result.stderr
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "hook_name",
    ["pre-push", "pre-push.pre-aru", "enforce_touches.py", "touches.py"],
)
def test_installer_refuses_symlink_hook_targets(tmp_path, hook_name):
    target, hooks = repository(tmp_path)
    outside = tmp_path / f"outside-{hook_name}"
    outside.write_text("operator-owned\n", encoding="utf-8")
    (hooks / hook_name).symlink_to(outside)

    result = install_result(target)

    assert result.returncode != 0
    assert "symbolic-link hook target" in result.stderr
    assert outside.read_text(encoding="utf-8") == "operator-owned\n"


def test_hook_allows_one_initial_branch_on_a_verified_empty_remote(tmp_path):
    target, hooks = repository(tmp_path)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=target, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=target, check=True
    )
    (target / "README.md").write_text("bootstrap\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "bootstrap"], cwd=target, check=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    install(target)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    update = (
        f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"
    )

    result = subprocess.run(
        [str(hooks / "pre-push"), str(remote), str(remote)],
        cwd=target,
        input=update,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def git(target: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "user.name=Test",
         "-c", "user.email=test@example.com", *arguments],
        cwd=target, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def push_repo(tmp_path, monkeypatch):
    import os

    target, hooks = repository(tmp_path)
    (target / "README.md").write_text("base\n", encoding="utf-8")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "base")
    remote = tmp_path / "remote.git"
    git(target, "init", "--bare", "-b", "main", str(remote))
    git(target, "remote", "add", "origin", str(remote))
    git(target, "push", "origin", "main")
    git(target, "switch", "-c", "fix/issue-591-scope")
    (target / "allowed.txt").write_text("feature\n", encoding="utf-8")
    git(target, "add", "allowed.txt")
    git(target, "commit", "-m", "feature")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "assert sys.argv[1:4] == ['issue', 'view', '591']\n"
        "print(json.dumps({'state':'OPEN', 'body':os.environ['ARU_TEST_SCOPE'], "
        "'labels':[{'name':'status:in-progress'}, {'name':'agent:fixture'}]}))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ARU_TEST_SCOPE", "touches: allowed.txt")
    return target, hooks, remote


def invoke_hook(target, hooks, remote, previous, *, proposed=None, destination=None):
    branch = "refs/heads/fix/issue-591-scope"
    update = f"{branch} {proposed or git(target, 'rev-parse', 'HEAD')} "
    update += f"{destination or branch} {previous}\n"
    return subprocess.run(
        [str(hooks / "pre-push"), str(remote), str(remote)], cwd=target,
        input=update, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("subsequent", [False, True])
def test_push_scope_excludes_integrated_remote_default_work(push_repo, subsequent):
    target, hooks, remote = push_repo
    previous = git(target, "rev-parse", "HEAD") if subsequent else "0" * 40
    if subsequent:
        git(target, "push", "origin", "fix/issue-591-scope")
    old_default = git(target, "rev-parse", "main")
    git(target, "switch", "main")
    (target / "peer.txt").write_text("accepted peer work\n", encoding="utf-8")
    git(target, "add", "peer.txt")
    git(target, "commit", "-m", "peer work")
    git(target, "push", "origin", "main")
    git(target, "update-ref", "refs/remotes/origin/main", old_default)
    git(target, "switch", "fix/issue-591-scope")
    git(target, "merge", "--no-edit", "main")
    install(target)
    prior = hooks / "pre-push.pre-aru"
    prior.write_text('#!/usr/bin/env bash\ncat > prior-input\necho "$@" > prior-args\n')
    prior.chmod(0o755)

    result = invoke_hook(target, hooks, remote, previous)

    assert result.returncode == 0, result.stderr + result.stdout
    assert previous in (target / "prior-input").read_text()
    assert (target / "prior-args").read_text().strip() == f"{remote} {remote}"
    assert git(target, "rev-parse", "refs/remotes/origin/main") == old_default


def test_later_push_still_rejects_an_earlier_out_of_scope_change(push_repo):
    target, hooks, remote = push_repo
    (target / "outside.txt").write_text("earlier forbidden change\n", encoding="utf-8")
    git(target, "add", "outside.txt")
    git(target, "commit", "-m", "earlier change")
    git(target, "push", "origin", "fix/issue-591-scope")
    previous = git(target, "rev-parse", "HEAD")
    (target / "allowed.txt").write_text("latest in-scope update\n", encoding="utf-8")
    git(target, "commit", "-am", "latest change")
    install(target)

    result = invoke_hook(target, hooks, remote, previous)

    assert result.returncode != 0
    assert "outside.txt is outside touches:" in result.stdout


def test_existing_branch_refuses_unfetched_remote_default_commit(push_repo, tmp_path):
    target, hooks, remote = push_repo
    git(target, "push", "origin", "fix/issue-591-scope")
    previous = git(target, "rev-parse", "HEAD")
    peer = tmp_path / "peer"
    git(tmp_path, "clone", str(remote), str(peer))
    (peer / "new-default.txt").write_text("new remote commit\n", encoding="utf-8")
    git(peer, "add", "new-default.txt")
    git(peer, "commit", "-m", "unfetched main")
    git(peer, "push", "origin", "main")
    install(target)

    result = invoke_hook(target, hooks, remote, previous)

    assert result.returncode != 0
    assert "remote default-branch commit is unavailable locally" in result.stderr


@pytest.mark.parametrize("failure", [
    "remote", "default", "protected", "local-sha", "remote-sha", "remote-commit", "scope",
])
def test_push_refuses_unavailable_or_unsafe_evidence(push_repo, monkeypatch, failure):
    target, hooks, remote = push_repo
    previous, proposed, destination = "0" * 40, None, None
    if failure == "remote":
        remote = remote.parent / "missing.git"
    elif failure == "default":
        git(target, "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/missing")
    elif failure == "protected":
        destination = "refs/heads/main"
    elif failure == "local-sha":
        proposed = "HEAD"
    elif failure == "remote-sha":
        previous = "malformed"
    elif failure == "remote-commit":
        previous = "f" * 40
    else:
        monkeypatch.setenv("ARU_TEST_SCOPE", "touches: ../unsafe")
    install(target)
    prior = hooks / "pre-push.pre-aru"
    prior.write_text("#!/usr/bin/env bash\ntouch prior-ran\n")
    prior.chmod(0o755)
    result = invoke_hook(target, hooks, remote, previous, proposed=proposed, destination=destination)
    assert result.returncode != 0
    assert not (target / "prior-ran").exists()
