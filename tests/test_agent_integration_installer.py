from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_agent_integration.sh"
SKILLS = {
    "address-pr-feedback",
    "create-github-issue",
    "implement-next-issue",
    "init-agent-project",
    "remediate-ci-failure",
    "triage-backlog",
}


def install(home: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )


def test_installer_links_only_current_skills_and_installs_global_guidance(tmp_path):
    obsolete = tmp_path / ".codex" / "skills" / "run-aru-factory"
    obsolete.parent.mkdir(parents=True)
    obsolete.symlink_to(ROOT / "skills" / "run-aru-factory")

    install(tmp_path)

    for relative in (".agents/skills", ".codex/skills", ".cursor/skills"):
        installed = tmp_path / relative
        assert {path.name for path in installed.iterdir()} == SKILLS
        assert all(path.is_symlink() for path in installed.iterdir())

    guidance = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert guidance == (ROOT / "templates" / "AGENTS.md").read_text(encoding="utf-8")
    assert "fetch_next_work.py" in guidance
    assert "run-aru-factory" not in guidance
    assert "code-review" not in guidance
    assert "fetch_next_issue.py" not in guidance


def test_installer_migrates_known_legacy_global_guidance_with_backup(tmp_path):
    target = tmp_path / ".codex" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    legacy = """# Global Software Development Governance: Aru_Agentic_SDLC

Route work to run-aru-factory, code-review, and fetch_next_issue.py.
"""
    target.write_text(legacy, encoding="utf-8")

    install(tmp_path)

    assert target.read_text(encoding="utf-8") == (
        ROOT / "templates" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    backups = list(target.parent.glob("AGENTS.md.pre-aru-v0.2.8.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == legacy

    install(tmp_path)
    current = target.read_text(encoding="utf-8")
    assert current.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->") == 1
    assert current.count("<!-- END ARU_SDLC_GOVERNANCE -->") == 1


def test_installer_preserves_unmanaged_global_guidance(tmp_path):
    target = tmp_path / ".codex" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("# My instructions\n\nKeep this.\n", encoding="utf-8")

    install(tmp_path)

    current = target.read_text(encoding="utf-8")
    assert current.startswith("# My instructions\n\nKeep this.\n")
    assert current.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->") == 1


def test_installer_refuses_symlinked_global_guidance(tmp_path):
    target = tmp_path / ".codex" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not overwrite\n", encoding="utf-8")
    target.symlink_to(outside)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing symlinked global guidance path" in result.stderr
    assert outside.read_text(encoding="utf-8") == "do not overwrite\n"
    assert target.is_symlink()
