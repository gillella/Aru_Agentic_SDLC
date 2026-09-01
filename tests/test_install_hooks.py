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
