from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_installer_replaces_legacy_aru_hook_without_chaining_it(tmp_path):
    target, hooks = repository(tmp_path)
    legacy = hooks / "pre-push"
    legacy.write_text(
        "#!/usr/bin/env bash\n# Aru_Agentic_SDLC pre-push hook: legacy\n",
        encoding="utf-8",
    )
    legacy.chmod(0o755)

    install(target)

    assert "direct pushes to main/master" in legacy.read_text(encoding="utf-8")
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
