from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_agent_integration.sh"
SKILLS = {"address-pr-feedback", "create-github-issue", "implement-next-issue",
          "init-agent-project", "remediate-ci-failure", "triage-backlog"}
TARGETS = (".codex/AGENTS.md", ".claude/CLAUDE.md")


def install(home: Path, *, check=True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(INSTALLER)], cwd=ROOT,
                          env={**os.environ, "HOME": str(home)}, text=True,
                          capture_output=True, check=check)


def assert_current(guidance):
    assert "fetch_next_work.py" in guidance
    assert "GitHub account other than the author: a person, CodeRabbit" in guidance
    assert "Each repository must use its declared runner profile" in guidance
    assert "`.aru/verify-project.sh`" in guidance
    assert "preserve existing consumer verification on updates" in guidance
    assert not any(old in guidance for old in ("run-aru-factory", "code-review",
                                               "fetch_next_issue.py", "__ARU_"))
    assert guidance.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->") == 1
    assert guidance.count("<!-- END ARU_SDLC_GOVERNANCE -->") == 1


def test_installer_links_only_current_skills_and_installs_global_guidance(tmp_path):
    obsolete = tmp_path / ".codex/skills/run-aru-factory"
    obsolete.parent.mkdir(parents=True)
    obsolete.symlink_to(ROOT / "skills/run-aru-factory")
    install(tmp_path)
    for relative in (".agents/skills", ".codex/skills", ".cursor/skills", ".claude/skills"):
        installed = tmp_path / relative
        assert {path.name for path in installed.iterdir()} == SKILLS
        assert all(path.is_symlink() for path in installed.iterdir())
    for relative in TARGETS:
        assert_current((tmp_path / relative).read_text())


@pytest.mark.parametrize("relative", TARGETS)
def test_installer_migrates_known_legacy_global_guidance_with_backup(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    personal = "# Personal preferences\n\nPreserve these exactly.\n\n"
    suffix = "\n\n# Other instructions\nKeep this too.\n"
    legacy = "# Global Software Development Governance: Aru_Agentic_SDLC\nrun-aru-factory\n"
    if relative.startswith(".claude"):
        legacy = (personal + "# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC\n"
                  "run-aru-factory\n<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n"
                  "code-review\n<!-- END ARU_SDLC_GOVERNANCE -->" + suffix)
    target.write_text(legacy)
    install(tmp_path)
    current = target.read_text()
    assert_current(current)
    if relative.startswith(".claude"):
        assert current.startswith(personal) and current.endswith(suffix)
    backups = list(target.parent.glob(target.name + ".pre-aru-*"))
    assert len(backups) == 1 and backups[0].read_text() == legacy
    install(tmp_path)
    assert target.read_text() == current
    assert len(list(target.parent.glob(target.name + ".pre-aru-*"))) == 1


@pytest.mark.parametrize("relative", TARGETS)
def test_installer_preserves_unmanaged_global_guidance(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("# My instructions\n\nKeep this.\n")
    install(tmp_path)
    current = target.read_text()
    assert current.startswith("# My instructions\n\nKeep this.\n")
    assert_current(current)


@pytest.mark.parametrize("relative", TARGETS)
def test_installer_refuses_symlinked_global_guidance(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not overwrite\n")
    target.symlink_to(outside)
    result = install(tmp_path, check=False)
    assert result.returncode != 0 and "refusing symlinked" in result.stderr
    assert outside.read_text() == "do not overwrite\n" and target.is_symlink()


@pytest.mark.parametrize("ending", ["", "<!-- END ARU_SDLC_GOVERNANCE -->\n" * 2,
    "<!-- END ARU_SDLC_GOVERNANCE --> # Personal suffix\n",
    "<!-- END ARU_SDLC_GOVERNANCE -->" * 2])
def test_ambiguous_claude_legacy_is_preserved(tmp_path, ending):
    target = tmp_path / ".claude/CLAUDE.md"
    target.parent.mkdir()
    original = ("# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC\n"
                "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n" + ending)
    target.write_text(original)
    assert install(tmp_path, check=False).returncode != 0
    assert target.read_text() == original


def test_installer_skips_claude_when_plugin_installed(tmp_path):
    installed_plugins = tmp_path / ".claude/plugins/installed_plugins.json"
    installed_plugins.parent.mkdir(parents=True)
    installed_plugins.write_text('{"plugins": {"aru-codefactory@2.2.1": {}}}', encoding="utf-8")

    # Stale skill that should be pruned from .claude/skills
    stale_claude = tmp_path / ".claude/skills/run-aru-factory"
    stale_claude.parent.mkdir(parents=True)
    stale_claude.symlink_to(ROOT / "skills/run-aru-factory")

    result = install(tmp_path)
    assert (
        "skipped ~/.claude/skills and ~/.claude/CLAUDE.md: the aru-codefactory "
        "plugin supplies the six skills and the governance block" in result.stdout
    )
    # .claude/skills has no newly linked skills
    assert not (tmp_path / ".claude/skills/implement-next-issue").exists()
    # stale symlink was pruned
    assert not stale_claude.exists()
    # .claude/CLAUDE.md was skipped
    assert not (tmp_path / ".claude/CLAUDE.md").exists()

    # Codex, Cursor, and .agents remain installed and identical
    for relative in (".agents/skills", ".codex/skills", ".cursor/skills"):
        installed = tmp_path / relative
        assert {path.name for path in installed.iterdir()} == SKILLS
        assert all(path.is_symlink() for path in installed.iterdir())
    assert_current((tmp_path / ".codex/AGENTS.md").read_text())
