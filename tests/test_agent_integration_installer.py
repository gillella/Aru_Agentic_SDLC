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


def install(home: Path, *, project: Path | None = None, check=True) -> subprocess.CompletedProcess[str]:
    command = ["bash", str(INSTALLER)]
    if project is not None:
        command.extend(["--project", str(project)])
    return subprocess.run(command, cwd=ROOT,
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
    managed = guidance.split("<!-- BEGIN ARU_SDLC_GOVERNANCE -->", 1)[1].split(
        "<!-- END ARU_SDLC_GOVERNANCE -->", 1)[0]
    for skill in SKILLS:
        assert managed.count(f"`{skill}`") == 1
    assert "Every PR, documentation included" in managed
    assert "A push dismisses earlier approvals" in managed
    assert "agents sharing one GitHub account cannot approve each other" in managed


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


def test_plugin_precedence_removes_only_duplicate_claude_block(tmp_path):
    installed_plugins = tmp_path / ".claude/plugins/installed_plugins.json"
    installed_plugins.parent.mkdir(parents=True)
    installed_plugins.write_text('{"plugins": {"aru-codefactory@2.5.0": {}}}')
    claude = tmp_path / ".claude/CLAUDE.md"
    personal = "# Personal instructions\nKeep this.\n\n"
    consumer = "\n# Consumer controls\nRequire a human security signoff.\n"
    stale = ("<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n"
             "Use run-aru-factory and code-review. Tier 0 needs no approval; "
             "same-login agents may approve.\n"
             "<!-- END ARU_SDLC_GOVERNANCE -->")
    claude.write_text(personal + stale + consumer)
    install(tmp_path)
    assert claude.read_text() == personal + consumer
    assert len(list(claude.parent.glob("CLAUDE.md.pre-aru-*"))) == 1
    install(tmp_path)
    assert claude.read_text() == personal + consumer
    assert len(list(claude.parent.glob("CLAUDE.md.pre-aru-*"))) == 1


def test_plugin_precedence_refuses_unknown_claude_legacy_section(tmp_path):
    installed_plugins = tmp_path / ".claude/plugins/installed_plugins.json"
    installed_plugins.parent.mkdir(parents=True)
    installed_plugins.write_text('{"plugins": {"aru-codefactory@2.5.0": {}}}')
    claude = tmp_path / ".claude/CLAUDE.md"
    original = ("# MASTER OPERATING DIRECTIVE: Aru_Agentic_SDLC\n"
                "run-aru-factory\nConsumer text without a boundary.\n"
                "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nold\n"
                "<!-- END ARU_SDLC_GOVERNANCE -->\n")
    claude.write_text(original)
    assert install(tmp_path, check=False).returncode != 0
    assert claude.read_text() == original


def test_managed_global_update_replaces_obsolete_review_and_preserves_consumer_controls(tmp_path):
    target = tmp_path / ".codex/AGENTS.md"
    target.parent.mkdir()
    before = "# Personal preferences\nKeep this.\n\n"
    after = "\n# Consumer controls\nRequire two human approvals for payments.\n"
    target.write_text(before + "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n"
                      "Use run-aru-factory, code-review and fetch_next_issue.py.\n"
                      "Tier 0 skips review; same-login agents can approve.\n"
                      "<!-- END ARU_SDLC_GOVERNANCE -->" + after)
    install(tmp_path)
    current = target.read_text()
    assert current.startswith(before) and current.endswith(after)
    assert_current(current)
    assert "Tier 0 skips review" not in current
    install(tmp_path)
    assert target.read_text() == current


@pytest.mark.parametrize("body", [
    "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nold\n",
    "<!-- END ARU_SDLC_GOVERNANCE -->\n",
    "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nold\n<!-- END ARU_SDLC_GOVERNANCE --> # suffix\n",
    "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nold\n<!-- END ARU_SDLC_GOVERNANCE -->\n"
    "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\nold\n<!-- END ARU_SDLC_GOVERNANCE -->\n",
])
def test_malformed_global_boundary_is_refused_without_edit(tmp_path, body):
    target = tmp_path / ".codex/AGENTS.md"
    target.parent.mkdir()
    target.write_text("# My instructions\n" + body)
    result = install(tmp_path, check=False)
    assert result.returncode != 0
    assert target.read_text() == "# My instructions\n" + body


LEGACY_CURSOR_RULE = """---
description: Aru_Agentic_SDLC Issue-First governance for this repository
alwaysApply: true
---

# Aru Agentic SDLC (project rule)

This repository is governed by **Aru_Agentic_SDLC**.

## Before any code change

1. Confirm work originates from a tracked GitHub issue (Issue-First Law).
2. Read and follow the matching skill under `$ARU_SDLC_HOME/skills/`:
   - `run-aru-factory` — `aru code` (synonyms software/dev/sdlc), "please continue", work the board, loop
   - `implement-next-issue` — claim/implement/PR for a named or next issue
   - `create-github-issue` — file work
   - `code-review` — review only a preassigned `review:agent` emergency fallback; otherwise refuse and await external review
   - `remediate-ci-failure` — fix red CI
   - `address-pr-feedback` — resolve review threads
   - `init-agent-project` — bootstrap a new governed repo
3. Prefer `python3 "$ARU_SDLC_HOME/scripts/<tool>.py"` over ad-hoc GitHub/git glue.

## Hard constraints

- No direct pushes to `main` / `master`
- Feature and remediation work runs in `.worktrees/`
- Local tests must pass before commit/push
- Every PR body includes `Closes #<issue_number>`
- Resume in-flight issues for this agent id before claiming new work

## Issue body contract

```
depends-on: #12, #14
parallel-eligible: true
touches: src/**, tests/**
```
"""


def test_legacy_cursor_project_rule_migrates_with_consumer_controls(tmp_path):
    project = tmp_path / "project"
    rule = project / ".cursor/rules/aru-agentic-sdlc.mdc"
    rule.parent.mkdir(parents=True)
    consumer = "\n## Consumer controls\nRequire a human approval for production.\n"
    rule.write_text(LEGACY_CURSOR_RULE + consumer)
    install(tmp_path / "home", project=project)
    current = rule.read_text()
    assert_current(current)
    assert "## Hard constraints" in current
    assert current.endswith(consumer)
    assert "run-aru-factory" not in current and "code-review" not in current
    assert len(list(rule.parent.glob("aru-agentic-sdlc.mdc.pre-aru-*"))) == 1
    install(tmp_path / "home", project=project)
    assert rule.read_text() == current
    assert len(list(rule.parent.glob("aru-agentic-sdlc.mdc.pre-aru-*"))) == 1


def test_cursor_managed_rule_replaces_tiered_same_login_guidance(tmp_path):
    project = tmp_path / "project"
    rule = project / ".cursor/rules/aru-agentic-sdlc.mdc"
    rule.parent.mkdir(parents=True)
    header = LEGACY_CURSOR_RULE.split("## Before any code change", 1)[0]
    consumer = "\n## Consumer controls\nRequire two human approvals for production.\n"
    original = (header + "<!-- BEGIN ARU_SDLC_GOVERNANCE -->\n"
                "Use run-aru-factory, code-review and fetch_next_issue.py.\n"
                "Tier 0 has no review; same-login agents may approve.\n"
                "<!-- END ARU_SDLC_GOVERNANCE -->" + consumer)
    rule.write_text(original)
    install(tmp_path / "home", project=project)
    current = rule.read_text()
    assert_current(current)
    assert current.endswith(consumer)
    assert "Tier 0 has no review" not in current
    install(tmp_path / "home", project=project)
    assert rule.read_text() == current
    assert len(list(rule.parent.glob("aru-agentic-sdlc.mdc.pre-aru-*"))) == 1


@pytest.mark.parametrize("change", [
    lambda text: text.replace("## Hard constraints", "## Other section"),
    lambda text: text.replace("   - `code-review`", "   - `personal-review`"),
    lambda text: text.replace("## Hard constraints", "<!-- END ARU_SDLC_GOVERNANCE -->\n## Hard constraints"),
    lambda text: text + "\nUse code-review for payments.\n",
])
def test_unknown_or_malformed_cursor_rule_is_preserved(tmp_path, change):
    project = tmp_path / "project"
    rule = project / ".cursor/rules/aru-agentic-sdlc.mdc"
    rule.parent.mkdir(parents=True)
    original = change(LEGACY_CURSOR_RULE)
    rule.write_text(original)
    result = install(tmp_path / "home", project=project, check=False)
    assert result.returncode != 0
    assert rule.read_text() == original


# --- Hermes Agent: a client with no plugin mechanism, configured only if present ---

HERMES_SKILLS = ".hermes/skills/software-development"
HERMES_GUIDANCE = ".hermes/SOUL.md"


def test_installer_ignores_hermes_when_it_is_not_installed(tmp_path):
    """The installer configures agents that are present; it never creates a home
    for one that is not, because an empty ~/.hermes would make a Hermes host of a
    machine that has never run Hermes."""
    result = install(tmp_path)
    assert not (tmp_path / ".hermes").exists()
    assert "skipped Hermes Agent" in result.stdout


def test_installer_links_the_six_skills_into_hermes_category_directory(tmp_path):
    """Hermes discovers <category>/<skill>/SKILL.md, so the six skills are the
    children of one category directory rather than one bundled skill."""
    (tmp_path / ".hermes").mkdir()
    install(tmp_path)
    category = tmp_path / HERMES_SKILLS
    linked = sorted(path.name for path in category.iterdir())
    assert linked == sorted(SKILLS)
    for name in SKILLS:
        entry = category / name
        assert entry.is_symlink()
        assert (entry / "SKILL.md").is_file(), f"{name} must expose SKILL.md to Hermes"


def test_installer_installs_and_then_updates_the_soul_block_idempotently(tmp_path):
    (tmp_path / ".hermes").mkdir()
    install(tmp_path)
    guidance = tmp_path / HERMES_GUIDANCE
    assert_current(guidance.read_text(encoding="utf-8"))
    first = guidance.read_text(encoding="utf-8")
    install(tmp_path)
    assert guidance.read_text(encoding="utf-8") == first, "a second run must not drift"


def test_installer_preserves_a_hermes_soul_the_operator_already_wrote(tmp_path):
    (tmp_path / ".hermes").mkdir()
    guidance = tmp_path / HERMES_GUIDANCE
    guidance.write_text("# My own soul\n\nkeep this\n", encoding="utf-8")
    install(tmp_path)
    text = guidance.read_text(encoding="utf-8")
    assert "# My own soul" in text and "keep this" in text
    assert_current(text)
    install(tmp_path)
    again = guidance.read_text(encoding="utf-8")
    assert again.count("# My own soul") == 1
    assert again.count("<!-- BEGIN ARU_SDLC_GOVERNANCE -->") == 1


def test_installer_refuses_a_symlinked_hermes_home(tmp_path):
    """A linked ~/.hermes could place the block or the skills outside the home the
    caller named, so the installer treats it as absent rather than following it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".hermes").symlink_to(elsewhere)
    result = install(tmp_path)
    assert "skipped Hermes Agent" in result.stdout
    assert not (elsewhere / "SOUL.md").exists()
    assert not (elsewhere / "skills").exists()
