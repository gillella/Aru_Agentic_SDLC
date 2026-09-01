from __future__ import annotations

import importlib.util

import pytest
import yaml

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


def test_bootstrap_provisions_external_and_coding_review_authorities():
    assert {
        "review:coderabbit",
        "review:sourcery",
        "review:codeant",
        "review:claude-code",
        "review:openai-codex",
        "review:xai-cursor",
        "review:google-antigravity",
    }.issubset(init_project.LABELS)
    assert not any(label.startswith("reviewer-registered:") for label in init_project.LABELS)
    assert not any(label.startswith("reviewer-binding:") for label in init_project.LABELS)


def test_ruleset_requires_the_server_exact_head_check_and_no_bypass():
    payload = init_project.ruleset_payload()
    assert payload["bypass_actors"] == []
    rules = {rule["type"]: rule for rule in payload["rules"]}
    assert rules["pull_request"]["parameters"]["required_review_thread_resolution"] is True
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is False
    checks = rules["required_status_checks"]["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    assert checks["required_status_checks"] == [
        {
            "context": "aru-governed-pr",
            "integration_id": init_project.GITHUB_ACTIONS_APP_ID,
        }
    ]


def test_kernel_workflow_is_read_only_exact_head_and_immutable():
    path = init_project.Path(__file__).resolve().parents[1] / ".github/workflows/governed-pr.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    job = workflow["jobs"]["governed-pr"]
    assert job["name"] == "aru-governed-pr"
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert checkout["uses"].startswith("actions/checkout@")
    assert len(checkout["uses"].split("@", 1)[1]) == 40
    assert checkout["with"]["persist-credentials"] is False
    raw = path.read_text(encoding="utf-8")
    assert "merge_group:" in raw
    assert "github.event_name == 'pull_request'" in raw


def test_scaffold_creates_only_minimal_governance(tmp_path):
    target = tmp_path / "consumer"
    written = init_project.scaffold("consumer", target)
    assert set(written) == {
        "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/workflows/governed-pr.yml",
        ".aru/verify.sh",
        ".aru/lib/touches.py",
        ".gitignore",
        ".aru/hooks/pre-push",
        ".aru/hooks/enforce_touches.py",
    }
    assert (target / ".git").is_dir()
    assert not (target / "skills").exists()
    assert not (target / "scripts").exists()
    workflow = (target / ".github/workflows/governed-pr.yml").read_text(
        encoding="utf-8"
    )
    assert "name: aru-governed-pr" in workflow
    assert "hooks/enforce_touches.py --pr" in workflow
    assert (target / ".aru/verify.sh").stat().st_mode & 0o111
    assert "class TouchesError" in (target / ".aru/lib/touches.py").read_text(
        encoding="utf-8"
    )
    assert (target / ".git/hooks/touches.py").is_file()


def test_scaffolded_hook_loads_its_vendored_canonical_parser(tmp_path, monkeypatch):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target)
    monkeypatch.delenv("ARU_SDLC_HOME", raising=False)
    hook = target / ".aru/hooks/enforce_touches.py"
    spec = importlib.util.spec_from_file_location("consumer_touches_hook", hook)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_touches("touches: src/**") == ["src/**"]


def test_scaffolded_pr_template_names_the_server_verification_authority(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target)
    template = (target / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "required `aru-governed-pr` server check" in template
    assert "Optional local preflight" in template


def test_scaffold_refuses_to_overwrite_user_content(tmp_path):
    target = tmp_path / "consumer"
    target.mkdir()
    (target / "AGENTS.md").write_text("mine", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="refusing"):
        init_project.scaffold("consumer", target)


@pytest.mark.parametrize("linked_directory", [".aru", ".github"])
def test_scaffold_refuses_nested_symlink_directory_escape(tmp_path, linked_directory):
    target = tmp_path / "consumer"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / linked_directory).symlink_to(outside, target_is_directory=True)

    with pytest.raises(init_project.BootstrapError, match="symbolic-link"):
        init_project.scaffold("consumer", target)

    assert list(outside.iterdir()) == []


def test_scaffold_refuses_symlink_file_target_escape(tmp_path):
    target = tmp_path / "consumer"
    (target / ".aru").mkdir(parents=True)
    outside = tmp_path / "outside-verify.sh"
    outside.write_text("operator-owned\n", encoding="utf-8")
    (target / ".aru" / "verify.sh").symlink_to(outside)

    with pytest.raises(init_project.BootstrapError, match="symbolic-link"):
        init_project.scaffold("consumer", target)

    assert outside.read_text(encoding="utf-8") == "operator-owned\n"


def test_scaffold_refuses_external_core_hooks_path(tmp_path):
    target = tmp_path / "consumer"
    target.mkdir()
    init_project.run(["git", "init", "-b", "main"], cwd=target)
    outside = tmp_path / "outside-hooks"
    init_project.run(
        ["git", "config", "core.hooksPath", str(outside)],
        cwd=target,
    )

    with pytest.raises(init_project.BootstrapError, match="non-canonical"):
        init_project.scaffold("consumer", target)

    assert not outside.exists()


@pytest.mark.parametrize("name", ["x", "../bad", "contains space", "-leading"])
def test_project_name_is_contained(name):
    with pytest.raises(init_project.BootstrapError):
        init_project.safe_name(name)


def test_github_setup_marks_project_graphql_authority(monkeypatch, tmp_path):
    calls = []
    rulesets = []

    def fake_command(argv, *, cwd, json_output=False, auth=None):
        calls.append((argv, auth))
        if argv[:3] == ["gh", "repo", "view"]:
            return {"nameWithOwner": "owner/consumer"}
        if argv[:3] == ["gh", "project", "create"]:
            return {"number": 5, "url": "https://example.test/project/5"}
        if argv[:3] == ["gh", "project", "field-list"]:
            return {"fields": [{"name": "Status", "id": "PVTSSF_1"}]}
        if argv[:3] == ["gh", "api", "repos/owner/consumer/rulesets"]:
            input_path = argv[argv.index("--input") + 1]
            rulesets.append(init_project.json.loads(init_project.Path(input_path).read_text()))
            return {"_links": {"html": {"href": "https://example.test/rules/1"}}}
        return ""

    monkeypatch.setattr(init_project, "command", fake_command)

    result = init_project.github_setup("consumer", tmp_path, private=True)

    graphql_calls = [call for call in calls if call[0][:3] == ["gh", "api", "graphql"]]
    assert result["repository"] == "owner/consumer"
    assert len(graphql_calls) == 1
    assert graphql_calls[0][1] == init_project.PROJECT_AUTH
    assert rulesets == [init_project.ruleset_payload()]
    ruleset_calls = [call for call in calls if call[0][:3] == ["gh", "api", "repos/owner/consumer/rulesets"]]
    assert len(ruleset_calls) == 1
    assert ruleset_calls[0][1] == init_project.REPOSITORY_AUTH
    assert result["ruleset"] == "https://example.test/rules/1"
