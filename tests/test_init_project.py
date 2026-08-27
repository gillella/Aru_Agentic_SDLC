from __future__ import annotations

import pytest

import init_project


def test_bootstrap_provisions_priority_labels():
    assert {
        label: metadata
        for label, metadata in init_project.LABELS.items()
        if label.startswith("priority:")
    } == {
        "priority:p0": ("b60205", "Blocking; drop everything"),
        "priority:p1": ("d93f0b", "Current phase critical path"),
        "priority:p2": ("fbca04", "Current phase, not critical path"),
        "priority:p3": ("c5def5", "Opportunistic"),
    }


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
