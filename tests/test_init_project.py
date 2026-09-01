from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess

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
    assert job["runs-on"] == ["self-hosted", "macOS", "ARM64", "aru-ci"]
    preflight = job["steps"][0]
    assert preflight["name"] == "Validate self-hosted runner trust boundary"
    assert "ARU_HEAD_REPOSITORY" in preflight["env"]
    assert "command -v python3" in preflight["run"]
    assert "command -v gh" in preflight["run"]
    checkout = job["steps"][1]
    assert checkout["with"]["ref"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert checkout["uses"].startswith("actions/checkout@")
    assert len(checkout["uses"].split("@", 1)[1]) == 40
    assert checkout["with"]["persist-credentials"] is False
    raw = path.read_text(encoding="utf-8")
    assert "merge_group:" in raw
    assert "github.event_name == 'pull_request'" in raw
    assert "pull_request_target" not in raw
    assert "ubuntu-latest" not in raw
    assert "actions/upload-artifact" not in raw
    assert "actions/setup-python" not in raw
    assert "cache:" not in raw


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
    assert "runs-on: [self-hosted, macOS, ARM64, aru-ci]" in workflow
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


def test_governed_pr_template_provenance_and_python3():
    path = init_project.Path(__file__).resolve().parents[1] / "templates/governed-pr.yml"
    raw = path.read_text(encoding="utf-8")
    assert "ARU_HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}" in raw
    assert "|| github.repository" not in raw
    assert 'if [[ "$ARU_EVENT_NAME" == "pull_request" && ( -z "$ARU_HEAD_REPOSITORY" || "$ARU_HEAD_REPOSITORY" != "$ARU_REPOSITORY" ) ]]; then' in raw
    assert 'python3 .aru/hooks/enforce_touches.py --pr "$ARU_PR_NUMBER"' in raw


@pytest.mark.parametrize(
    ("event_name", "head_repo", "repo", "expected_code"),
    [
        ("pull_request", "", "owner/repo", 1),
        ("pull_request", "fork/repo", "owner/repo", 1),
        ("pull_request", "owner/repo", "owner/repo", 0),
        ("merge_group", "", "owner/repo", 0),
        ("merge_group", "fork/repo", "owner/repo", 0),
    ],
)
def test_trust_boundary_script_execution(event_name, head_repo, repo, expected_code):
    script = """
if [[ "$ARU_EVENT_NAME" == "pull_request" && ( -z "$ARU_HEAD_REPOSITORY" || "$ARU_HEAD_REPOSITORY" != "$ARU_REPOSITORY" ) ]]; then
  echo "::error::Fork pull requests cannot execute on persistent self-hosted runners."
  exit 1
fi
"""
    env = {
        "ARU_EVENT_NAME": event_name,
        "ARU_HEAD_REPOSITORY": head_repo,
        "ARU_REPOSITORY": repo,
    }
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_code


def test_scaffold_consumer_drift_fixtures_and_permissions(tmp_path):
    target = tmp_path / "consumer"
    written = init_project.scaffold("consumer", target)
    framework = init_project.Path(__file__).resolve().parents[1]

    expected_sources = {
        "AGENTS.md": framework / "templates" / "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml": framework / "templates" / "issue.yml",
        ".github/PULL_REQUEST_TEMPLATE.md": framework / "templates" / "pull_request.md",
        ".github/workflows/governed-pr.yml": framework / "templates" / "governed-pr.yml",
        ".aru/verify.sh": framework / "templates" / "verify.sh",
        ".aru/lib/touches.py": framework / "scripts" / "touches.py",
        ".aru/hooks/pre-push": framework / "hooks" / "pre-push",
        ".aru/hooks/enforce_touches.py": framework / "hooks" / "enforce_touches.py",
    }

    for relative, source_path in expected_sources.items():
        assert relative in written
        dest_file = target / relative
        assert dest_file.is_file()
        expected_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        actual_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        assert actual_hash == expected_hash, f"Hash mismatch for {relative}"

    # Executable permissions binding
    executable_files = {
        ".aru/verify.sh",
        ".aru/hooks/pre-push",
        ".aru/hooks/enforce_touches.py",
    }
    for relative in written:
        dest_file = target / relative
        mode = dest_file.stat().st_mode
        if relative in executable_files:
            assert mode & 0o111 != 0, f"Expected {relative} to be executable"
        else:
            assert mode & 0o111 == 0, f"Expected {relative} to not be executable"


def test_verify_template_secret_scan_positives_and_negatives():
    path = init_project.Path(__file__).resolve().parents[1] / "templates/verify.sh"
    content = path.read_text(encoding="utf-8")

    match = re.search(r'secret_re="(.*?)"\s*$', content, re.MULTILINE)
    assert match is not None
    secret_re = match.group(1).replace(r'\"', '"')

    positives = [
        "ghp_123456789012345678901234567890123456",
        "github_pat_123456789012345678901234567890123456789012345678901234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
        "sk-123456789012345678901234567890123456",
        "sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx_yz-123456",
        'API_SECRET_KEY="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"',
        "API_SECRET_KEY=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        'JMC_API_SECRET="c4d9e32e4518ff6adffb23ba8cc224450e9ec6ffefd862451d449d85331480e2"',
        "JMC_API_SECRET=c4d9e32e4518ff6adffb23ba8cc224450e9ec6ffefd862451d449d85331480e2",
        "-----BEGIN RSA PRIVATE KEY-----",
    ]

    negatives = [
        content,
        "API_SECRET_KEY=your-long-random-secret-key-min-32-chars",
        "JMC_API_SECRET=your-long-random-secret-key-min-32-chars",
        "DEEPSEEK_API_KEY=sk-your-deepseek-api-key",
        "DATABASE_URL=postgresql://username:password@localhost:5432/jaji_mc",
        'DATABASE_URL="postgresql://verify:verify@127.0.0.1:5432/verify"',
        'NEXT_PUBLIC_APP_URL="http://localhost:3000"',
        "LINKEDIN_CLIENT_SECRET=your-linkedin-client-secret",
        'API_SECRET_KEY=""',
        'API_SECRET_KEY="placeholder"',
    ]

    for item in positives:
        res = subprocess.run(
            ["grep", "-Eq", secret_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode == 0, f"Expected positive match for {item}"

    for item in negatives:
        res = subprocess.run(
            ["grep", "-Eq", secret_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode != 0, f"Expected negative match (no match) for {item}"


def test_verify_template_changed_paths_deterministic_renames(tmp_path):
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "old.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "old.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "mv", "old.txt", "new.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "rename"], cwd=tmp_path, check=True, capture_output=True
    )

    cmd = 'git diff --name-status --find-renames --diff-filter=ACDMRT HEAD~1...HEAD -- | awk -F\'\t\' \'{for (i=2; i<=NF; i++) print $i}\' | sort -u'
    res = subprocess.run(
        ["bash", "-c", cmd],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip().splitlines() == ["new.txt", "old.txt"]
