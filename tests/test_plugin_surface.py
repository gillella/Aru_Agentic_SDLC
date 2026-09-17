from __future__ import annotations

import pytest

import json
import pathlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import policy
from test_surface import supported_command_paths, tracked_paths

AGENT_PLUGINS_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PERMITTED_MANIFEST_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
PLUGIN_NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def test_root_plugin_manifest_conforms_to_agent_plugins_standard():
    manifest_path = ROOT / "plugin.json"
    assert manifest_path.is_file(), "root plugin.json must exist"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert data.get("$schema") == AGENT_PLUGINS_SCHEMA_ID
    name = data.get("name")
    assert isinstance(name, str) and PLUGIN_NAME_PATTERN.match(name)
    assert name == "aru-codefactory"

    assert data.get("version") == policy.version()
    assert set(data.keys()).issubset(PERMITTED_MANIFEST_FIELDS)
    assert not any(k in data for k in ("agents", "hooks", "skills", "mcpServers"))


def test_claude_adapter_agrees_with_root_manifest():
    root_data = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    adapter_path = ROOT / ".claude-plugin" / "plugin.json"
    assert adapter_path.is_file(), ".claude-plugin/plugin.json must exist"
    adapter = json.loads(adapter_path.read_text(encoding="utf-8"))

    for key in ("name", "version", "description"):
        assert adapter.get(key) == root_data.get(key), f"adapter {key} must agree"

    # Not a literal: every manifest tracks the release the Factory actually declares,
    # so a version bump that forgets an adapter fails here instead of shipping.
    assert adapter.get("version") == policy.version()

    # Not a style preference: `claude plugin validate .` rejects a directory string here
    # ("agents: Invalid input"), unlike the Cursor adapter, so the Claude adapter must
    # name every persona file and a new persona has to be added in both places.
    agents = adapter.get("agents")
    assert isinstance(agents, list), "claude adapter agents must be a list of file paths"
    for agent_rel in agents:
        assert (ROOT / agent_rel).is_file(), f"agent path {agent_rel} must be a file"
    listed = {pathlib.PurePosixPath(a).name for a in agents}
    available = {q.name for q in (ROOT / "plugin" / "agents").glob("*.md")}
    assert listed == available, "claude adapter must list every persona in plugin/agents/"

    hooks_path = ROOT / adapter.get("hooks", "")
    assert hooks_path.is_file(), f"adapter hooks path {hooks_path} must exist"


def test_cursor_adapter_agrees_with_root_manifest():
    root_data = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    adapter_path = ROOT / ".cursor-plugin" / "plugin.json"
    assert adapter_path.is_file(), ".cursor-plugin/plugin.json must exist"
    adapter = json.loads(adapter_path.read_text(encoding="utf-8"))

    for key in ("name", "version", "description"):
        assert adapter.get(key) == root_data.get(key), f"cursor adapter {key} must agree"

    # Not a literal: the adapter tracks the release the Factory declares in policy.toml.
    assert adapter.get("version") == policy.version()

    agents = adapter.get("agents")
    assert isinstance(agents, str), "cursor adapter agents must be a directory path"
    agents_dir = ROOT / agents
    assert agents_dir.is_dir(), f"cursor adapter agents path {agents} must be a directory"
    agent_names = {p.name for p in agents_dir.glob("*.md")}
    assert {"aru-implementer.md", "aru-reviewer.md", "aru-triager.md"}.issubset(agent_names)

    skills = adapter.get("skills")
    assert isinstance(skills, str), "cursor adapter skills must be a directory path"
    skills_dir = ROOT / skills
    assert skills_dir.is_dir(), f"cursor adapter skills path {skills} must be a directory"
    skill_names = [p.parent.name for p in skills_dir.glob("*/SKILL.md")]
    assert len(skill_names) == 6


def test_marketplace_manifest_is_valid():
    market_path = ROOT / ".claude-plugin" / "marketplace.json"
    assert market_path.is_file(), "marketplace.json must exist"
    market = json.loads(market_path.read_text(encoding="utf-8"))

    if "metadata" in market and "version" in market["metadata"]:
        assert market["metadata"]["version"] == policy.version()

    plugins = market.get("plugins", [])
    assert len(plugins) == 1
    assert plugins[0].get("name") == "aru-codefactory"
    assert plugins[0].get("source") == "./"


def test_subagent_personas_have_frontmatter_and_contracts():
    agents_dir = ROOT / "plugin" / "agents"
    agent_files = sorted(agents_dir.glob("*.md"))
    names = {p.name for p in agent_files}
    assert names == {
        "aru-docs.md",
        "aru-implementer.md",
        "aru-reviewer.md",
        "aru-tester.md",
        "aru-triager.md",
    }

    for p in agent_files:
        text = p.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{p.name} must start with frontmatter"
        end_idx = text.find("\n---\n", 4)
        assert end_idx != -1, f"{p.name} must have closing frontmatter"
        frontmatter = text[4:end_idx]
        assert "name:" in frontmatter and "description:" in frontmatter

    reviewer_text = (agents_dir / "aru-reviewer.md").read_text(encoding="utf-8")
    assert "refuses to review" in reviewer_text
    assert "authored" in reviewer_text

    tester_text = (agents_dir / "aru-tester.md").read_text(encoding="utf-8")
    assert "fails before the implementation change and passes after it" in tester_text
    assert "Never deletes or loosens an existing assertion" in tester_text
    assert "declared `touches:`" in tester_text

    docs_text = (agents_dir / "aru-docs.md").read_text(encoding="utf-8")
    assert "Never changes code" in docs_text
    assert "scripts/policy.toml" in docs_text


def test_persona_prompts_require_identity_announcement():
    # Iterate every prompt file so a later persona that omits the rule fails here,
    # even if the exact-five name set above is updated to admit it.
    required = (
        "Before its first action on a claimed issue",
        "this persona's role",
        "the model it is running as",
        "fleet-launched",
        "self-reported",
        "launcher recorded",
        "does not know",
        "rather than guessing from context",
    )
    for path in sorted((ROOT / "plugin" / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for phrase in required:
            assert phrase in text, f"{path.name} must contain {phrase!r}"
        lowered = text.lower()
        assert "subscription account" not in lowered
        assert "name a subscription" not in lowered


def test_hooks_json_configuration():
    hooks_file = ROOT / "plugin" / "hooks" / "hooks.json"
    hooks_config = json.loads(hooks_file.read_text(encoding="utf-8"))
    hooks = hooks_config.get("hooks", {})

    assert "SessionStart" in hooks
    assert "PreToolUse" in hooks

    pre_tool = hooks["PreToolUse"]
    assert len(pre_tool) == 1
    assert pre_tool[0].get("matcher") == "Write|Edit|NotebookEdit"

    for event_hooks in hooks.values():
        for group in event_hooks:
            for hook in group.get("hooks", []):
                assert "${CLAUDE_PLUGIN_ROOT}" in hook.get("command", "")


def test_no_blocking_constructs_under_plugin_hooks():
    hooks_dir = ROOT / "plugin" / "hooks"
    forbidden = ["permissiondecision", "deny", "block", "exit 2", "sys.exit(2)", "systemexit(2)"]
    for path in hooks_dir.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".sh", ".json"):
            content = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert token not in content, f"forbidden construct {token!r} in {path.relative_to(ROOT)}"


def test_session_start_hook_execution(tmp_path):
    script = ROOT / "plugin" / "hooks" / "session_start.sh"
    env_file = tmp_path / "env.sh"
    env_file.touch()

    # Case 1: ARU_SDLC_HOME unset
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "CLAUDE_ENV_FILE": str(env_file),
    }
    env.pop("ARU_SDLC_HOME", None)
    res = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=True)
    assert res.returncode == 0
    exported = env_file.read_text(encoding="utf-8")
    assert f"export ARU_SDLC_HOME={ROOT}" in exported
    data = json.loads(res.stdout)
    assert data.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"
    assert "additionalContext" in data.get("hookSpecificOutput", {})

    # Case 2: ARU_SDLC_HOME already set
    env["ARU_SDLC_HOME"] = "/custom/path"
    env_file.write_text("", encoding="utf-8")
    res2 = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=True)
    assert res2.returncode == 0
    assert env_file.read_text(encoding="utf-8") == ""


def test_touches_advisory_hook(monkeypatch):
    import importlib.util
    hook_path = ROOT / "plugin" / "hooks" / "touches_advisory.py"
    spec = importlib.util.spec_from_file_location("touches_advisory", hook_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake_run(cmd, cwd=None):
        if "rev-parse" in cmd and "--show-toplevel" in cmd:
            return str(ROOT)
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return "feat/issue-42-test"
        if "issue" in cmd and "view" in cmd:
            return json.dumps({"body": "touches: allowed/path/**\n"})
        return ""

    monkeypatch.setattr(mod, "_run", fake_run)

    # In-scope edit
    payload_in = json.dumps({
        "cwd": str(ROOT),
        "tool_input": {"file_path": "allowed/path/file.py"},
    })
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload_in))
    captured = []
    monkeypatch.setattr("builtins.print", lambda x: captured.append(x))
    with pytest.raises(SystemExit) as exc1:
        mod.main()
    assert exc1.value.code == 0
    assert not captured

    # Out-of-scope edit
    payload_out = json.dumps({
        "cwd": str(ROOT),
        "tool_input": {"file_path": "outside/path/file.py"},
    })
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload_out))
    captured.clear()
    with pytest.raises(SystemExit) as exc2:
        mod.main()
    assert exc2.value.code == 0
    assert len(captured) == 1
    out_json = json.loads(captured[0])
    assert out_json.get("hookSpecificOutput", {}).get("hookEventName") == "PreToolUse"
    assert "Aru advisory (not enforcement):" in out_json["hookSpecificOutput"]["additionalContext"]


def test_genericized_agent_guidance_equality():
    guidance = policy.genericized_agent_guidance()
    assert "Each repository must use its declared runner profile" in guidance
    assert "__ARU_" not in guidance


def test_surface_budgets_and_vendoring_refusals_remain_intact():
    assert len(supported_command_paths()) == 14
    skills = [
        path for path in tracked_paths()
        if path.name == "SKILL.md" and path.parent.parent.name == "skills"
    ]
    assert len(skills) == 6
    verify_sh = (ROOT / "scripts" / "verify_consumer.sh").read_text(encoding="utf-8")
    assert "Factory lifecycle scripts must not be vendored" in verify_sh
    assert "Factory skills must not be vendored under .aru/skills" in verify_sh
