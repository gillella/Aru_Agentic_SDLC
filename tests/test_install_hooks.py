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


@pytest.mark.parametrize("revision", [
    "7a3dc46", "2b55cfc", "d3a0588", "4393d3c", "2dfe4ce", "a1559da",
])
@pytest.mark.parametrize("location", ["pre-push", "pre-push.pre-aru", "both"])
def test_installer_upgrades_authentic_historical_hooks(push_repo, revision, location):
    target, hooks, remote = push_repo
    # CI already fetches full history; these immutable source versions are the fixtures.
    historical = subprocess.check_output(["git", "show", f"{revision}:hooks/pre-push"], cwd=ROOT)
    install(target)
    names = ["pre-push", "pre-push.pre-aru"] if location == "both" else [location]
    for name in names:
        (hooks / name).write_bytes(historical)
        (hooks / name).chmod(0o755)
    for _ in range(2):
        install(target)
        assert (hooks / "pre-push").read_bytes() == (ROOT / "hooks/pre-push").read_bytes()
        assert not (hooks / "pre-push.pre-aru").exists()
        result = invoke_hook(target, hooks, remote, "0" * 40)
        assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("old_backup", [False, True])
def test_installer_preserves_and_chains_a_user_owned_hook(push_repo, old_backup):
    target, hooks, remote = push_repo
    custom = hooks / "pre-push"
    contents = "#!/usr/bin/env bash\n# Aru_Agentic_SDLC pre-push hook: local note\n"
    contents += "# Aru managed pre-push hook; installed by scripts/install_hooks.sh\n"
    contents += "cat > prior-input\necho user-hook\nexit 7\n"
    custom.write_text(contents)
    custom.chmod(0o755)
    if old_backup:
        (hooks / "pre-push.pre-aru").write_bytes(subprocess.check_output(
            ["git", "show", "4393d3c:hooks/pre-push"], cwd=ROOT,
        ))
    for _ in range(2):
        install(target)
        assert (hooks / "pre-push.pre-aru").read_text() == contents
        result = invoke_hook(target, hooks, remote, "0" * 40)
        assert result.returncode == 7 and "user-hook" in result.stdout
        assert "0" * 40 in (target / "prior-input").read_text()


@pytest.mark.parametrize("modified_history", [False, True])
def test_installer_refuses_ambiguous_custom_hooks_without_changes(tmp_path, modified_history):
    target, hooks = repository(tmp_path)
    current = b"#!/usr/bin/env bash\necho custom\n"
    if modified_history:
        current += subprocess.check_output(["git", "show", "4393d3c:hooks/pre-push"], cwd=ROOT)
    (hooks / "pre-push").write_bytes(current)
    (hooks / "pre-push.pre-aru").write_text("other user hook\n")
    result = install_result(target)
    assert result.returncode != 0 and "operator review" in result.stderr
    assert (hooks / "pre-push").read_bytes() == current
    assert (hooks / "pre-push.pre-aru").read_text() == "other user hook\n"
    assert not (hooks / "enforce_touches.py").exists()


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
    (target / "README.md").write_text("bootstrap\n", encoding="utf-8")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "bootstrap")
    remote = tmp_path / "remote.git"
    git(target, "init", "--bare", str(remote))
    install(target)
    head = git(target, "rev-parse", "HEAD")
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
