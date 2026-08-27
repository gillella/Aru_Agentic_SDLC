from __future__ import annotations

import pytest

import init_project


def test_scaffold_creates_only_minimal_governance(tmp_path):
    target = tmp_path / "consumer"
    written = init_project.scaffold("consumer", target)
    assert set(written) == {
        "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/workflows/ci.yml",
        ".gitignore",
        ".aru/hooks/pre-push",
        ".aru/hooks/enforce_touches.py",
    }
    assert (target / ".git").is_dir()
    assert not (target / "skills").exists()
    assert not (target / "scripts").exists()


def test_scaffold_refuses_to_overwrite_user_content(tmp_path):
    target = tmp_path / "consumer"
    target.mkdir()
    (target / "AGENTS.md").write_text("mine", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="refusing"):
        init_project.scaffold("consumer", target)


@pytest.mark.parametrize("name", ["x", "../bad", "contains space", "-leading"])
def test_project_name_is_contained(name):
    with pytest.raises(init_project.BootstrapError):
        init_project.safe_name(name)


def test_github_setup_marks_project_graphql_authority(monkeypatch, tmp_path):
    calls = []

    def fake_command(argv, *, cwd, json_output=False, auth=None):
        calls.append((argv, auth))
        if argv[:3] == ["gh", "repo", "view"]:
            return {"nameWithOwner": "owner/consumer"}
        if argv[:3] == ["gh", "project", "create"]:
            return {"number": 5, "url": "https://example.test/project/5"}
        if argv[:3] == ["gh", "project", "field-list"]:
            return {"fields": [{"name": "Status", "id": "PVTSSF_1"}]}
        return ""

    monkeypatch.setattr(init_project, "command", fake_command)

    result = init_project.github_setup("consumer", tmp_path, private=True)

    graphql_calls = [call for call in calls if call[0][:3] == ["gh", "api", "graphql"]]
    assert result["repository"] == "owner/consumer"
    assert len(graphql_calls) == 1
    assert graphql_calls[0][1] == init_project.PROJECT_AUTH
