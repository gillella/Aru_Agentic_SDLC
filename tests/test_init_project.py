from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess

import pytest
import yaml

import init_project

ROOT = init_project.Path(__file__).resolve().parents[1]
WORKFLOW_SOURCES = ["live", "self-hosted-mac", "github-hosted"]


def profiled_workflows() -> dict[str, str]:
    """Aru's own live workflow plus the template rendered for every profile."""
    template = (ROOT / "templates/governed-pr.yml").read_text(encoding="utf-8")
    return {
        "live": (ROOT / ".github/workflows/governed-pr.yml").read_text(encoding="utf-8"),
        **{name: init_project.render_profile(template, name) for name in init_project.RUNNER_PROFILES},
    }


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
    assert workflow["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read"}
    job = workflow["jobs"]["governed-pr"]
    assert job["name"] == "aru-governed-pr"
    assert job["runs-on"] == ["self-hosted", "macOS", "ARM64", "aru-ci"]
    preflight = job["steps"][0]
    assert preflight["name"] == "Validate self-hosted runner trust boundary"
    assert "ARU_HEAD_REPOSITORY" in preflight["env"]
    assert "command -v python3" in preflight["run"] and "command -v gh" in preflight["run"]
    checkout = job["steps"][1]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert checkout["uses"].startswith("actions/checkout@") and len(checkout["uses"].split("@", 1)[1]) == 40
    assert checkout["with"]["persist-credentials"] is False
    raw = path.read_text(encoding="utf-8")
    assert "merge_group:" not in raw and "github.event_name == 'pull_request'" in raw
    for absent in ("pull_request_target", "ubuntu-latest", "actions/upload-artifact", "actions/setup-python", "cache:"):
        assert absent not in raw


def test_scaffold_creates_only_minimal_governance(tmp_path):
    target = tmp_path / "consumer"
    written = init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    assert set(written) == {
        "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/workflows/governed-pr.yml",
        ".aru/verify.sh",
        ".aru/verify-project.sh",
        ".aru/lib/touches.py",
        ".gitignore",
        ".aru/hooks/pre-push",
        ".aru/hooks/enforce_touches.py",
    }
    assert (target / ".git").is_dir()
    assert not (target / "skills").exists()
    assert not (target / "scripts").exists()
    workflow = (target / ".github/workflows/governed-pr.yml").read_text(encoding="utf-8")
    assert "name: aru-governed-pr" in workflow
    assert "runs-on: [self-hosted, macOS, ARM64, aru-ci]" in workflow
    assert "hooks/enforce_touches.py --pr" in workflow
    assert (target / ".aru/verify.sh").stat().st_mode & 0o111
    assert "class TouchesError" in (target / ".aru/lib/touches.py").read_text(encoding="utf-8")
    assert (target / ".git/hooks/touches.py").is_file()


def test_scaffolded_hook_loads_its_vendored_canonical_parser(tmp_path, monkeypatch):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    monkeypatch.delenv("ARU_SDLC_HOME", raising=False)
    hook = target / ".aru/hooks/enforce_touches.py"
    spec = importlib.util.spec_from_file_location("consumer_touches_hook", hook)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.parse_touches("touches: src/**") == ["src/**"]


def test_scaffolded_pr_template_names_the_server_verification_authority(tmp_path):
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    template = (target / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert "required `aru-governed-pr` server check" in template
    assert "Optional local preflight" in template


def test_scaffold_refuses_to_overwrite_user_content(tmp_path):
    target = tmp_path / "consumer"
    target.mkdir()
    (target / "AGENTS.md").write_text("mine", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="refusing"):
        init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")


@pytest.mark.parametrize("linked_directory", [".aru", ".github"])
def test_scaffold_refuses_nested_symlink_directory_escape(tmp_path, linked_directory):
    target = tmp_path / "consumer"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / linked_directory).symlink_to(outside, target_is_directory=True)

    with pytest.raises(init_project.BootstrapError, match="symbolic-link"):
        init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")

    assert list(outside.iterdir()) == []


def test_scaffold_refuses_symlink_file_target_escape(tmp_path):
    target = tmp_path / "consumer"
    (target / ".aru").mkdir(parents=True)
    outside = tmp_path / "outside-verify.sh"
    outside.write_text("operator-owned\n", encoding="utf-8")
    (target / ".aru" / "verify.sh").symlink_to(outside)

    with pytest.raises(init_project.BootstrapError, match="symbolic-link"):
        init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")

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
        init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")

    assert not outside.exists()


@pytest.mark.parametrize("name", ["x", "../bad", "contains space", "-leading"])
def test_project_name_is_contained(name):
    with pytest.raises(init_project.BootstrapError):
        init_project.safe_name(name)


def test_github_setup_marks_project_graphql_authority(monkeypatch, tmp_path):
    calls, rulesets = [], []
    responses = {
        ("gh", "repo", "view"): {"nameWithOwner": "gillella/consumer"},
        ("gh", "project", "create"): {"number": 5, "url": "https://example.test/project/5"},
        ("gh", "project", "field-list"): {"fields": [{"name": "Status", "id": "PVTSSF_1"}]},
    }

    def fake_command(argv, *, cwd, json_output=False, auth=None):
        calls.append((argv, auth))
        if argv[:3] == ["gh", "api", "repos/gillella/consumer/rulesets"]:
            input_path = argv[argv.index("--input") + 1]
            rulesets.append(init_project.json.loads(init_project.Path(input_path).read_text()))
            return {"_links": {"html": {"href": "https://example.test/rules/1"}}}
        return responses.get(tuple(argv[:3]), "")

    def calls_with(*prefix):
        return [call for call in calls if call[0][: len(prefix)] == list(prefix)]

    monkeypatch.setattr(init_project, "command", fake_command)
    result = init_project.github_setup(
        "consumer", tmp_path, private=True, owner="gillella", runner_profile="self-hosted-mac"
    )
    assert result["repository"] == "gillella/consumer"
    assert [call[0][3] for call in calls_with("gh", "repo", "create")] == ["gillella/consumer"]
    assert [auth for _, auth in calls_with("gh", "api", "graphql")] == [init_project.PROJECT_AUTH]
    assert rulesets == [init_project.ruleset_payload()]
    ruleset_calls = calls_with("gh", "api", "repos/gillella/consumer/rulesets")
    assert [auth for _, auth in ruleset_calls] == [init_project.REPOSITORY_AUTH]
    assert result["ruleset"] == "https://example.test/rules/1"


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_governed_pr_workflow_provenance_and_python3(source):
    raw = profiled_workflows()[source]
    workflow = yaml.safe_load(raw)
    assert set(workflow.get("on", workflow.get(True))) == {"pull_request"}
    job = workflow["jobs"]["governed-pr"]
    assert job["if"] == "${{ github.event_name == 'pull_request' && !github.event.pull_request.draft }}"
    assert job["steps"][1]["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert "ARU_HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}" in raw
    assert "|| github.repository" not in raw
    assert (
        'if [[ "$ARU_EVENT_NAME" != "pull_request" || -z "$ARU_HEAD_REPOSITORY" || "$ARU_HEAD_REPOSITORY" != "$ARU_REPOSITORY" ]]; then'
        in raw
    )
    assert "Only verified pull_request events from this repository" in raw
    assert 'enforce_touches.py --pr "$ARU_PR_NUMBER"' in raw


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
@pytest.mark.parametrize(
    ("event_name", "head_repo", "repo", "expected_code"),
    [
        ("pull_request", "", "owner/repo", 1),
        ("pull_request", "fork/repo", "owner/repo", 1),
        ("pull_request", "owner/repo", "owner/repo", 0),
        ("merge_group", "", "owner/repo", 1),
        ("merge_group", "fork/repo", "owner/repo", 1),
        ("merge_group", "owner/repo", "owner/repo", 1),
        ("push", "owner/repo", "owner/repo", 1),
        ("workflow_dispatch", "owner/repo", "owner/repo", 1),
    ],
)
def test_trust_boundary_script_execution(source, event_name, head_repo, repo, expected_code):
    workflow = yaml.safe_load(profiled_workflows()[source])
    step = workflow["jobs"]["governed-pr"]["steps"][0]
    assert step["name"].startswith("Validate ") and step["name"].endswith("trust boundary")
    assert step["env"]["ARU_HEAD_REPOSITORY"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert step["env"]["ARU_REPOSITORY"] == "${{ github.repository }}"
    assert step["env"]["ARU_EVENT_NAME"] == "${{ github.event_name }}"
    env = {**os.environ, "ARU_EVENT_NAME": event_name,
           "ARU_HEAD_REPOSITORY": head_repo, "ARU_REPOSITORY": repo}
    result = subprocess.run(
        ["bash", "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == expected_code
    if expected_code == 1:
        assert (
            "::error::Only verified pull_request events from this repository may execute"
            in result.stdout
        )


def test_governed_pr_trust_boundary_no_drift():
    """The self-hosted rendering must still reproduce Aru's own live workflow."""
    workflows = profiled_workflows()
    rendered = yaml.safe_load(workflows["self-hosted-mac"])["jobs"]["governed-pr"]
    live = yaml.safe_load(workflows["live"])["jobs"]["governed-pr"]
    assert rendered["steps"][0] == live["steps"][0]
    assert rendered["runs-on"] == live["runs-on"] == ["self-hosted", "macOS", "ARM64", "aru-ci"]


def test_scaffold_consumer_drift_fixtures_and_permissions(tmp_path):
    target = tmp_path / "consumer"
    written = init_project.scaffold("consumer", target, runner_profile="self-hosted-mac")
    framework = init_project.Path(__file__).resolve().parents[1]

    expected_sources = {
        "AGENTS.md": framework / "templates" / "AGENTS.md",
        ".github/ISSUE_TEMPLATE/governed-task.yml": framework / "templates" / "issue.yml",
        ".github/PULL_REQUEST_TEMPLATE.md": framework / "templates" / "pull_request.md",
        ".github/workflows/governed-pr.yml": framework / "templates" / "governed-pr.yml",
        ".aru/verify.sh": framework / "templates" / "verify.sh",
        ".aru/verify-project.sh": framework / "templates" / "verify-project.sh",
        ".aru/lib/touches.py": framework / "scripts" / "touches.py",
        ".aru/hooks/pre-push": framework / "hooks" / "pre-push",
        ".aru/hooks/enforce_touches.py": framework / "hooks" / "enforce_touches.py",
    }
    # Profile-rendered outputs must match their template rendered for the same
    # profile; every other scaffolded file stays byte-identical to its source.
    rendered = {"AGENTS.md", ".github/workflows/governed-pr.yml"}

    for relative, source_path in expected_sources.items():
        assert relative in written
        dest_file = target / relative
        assert dest_file.is_file()
        source = source_path.read_text(encoding="utf-8")
        if relative in rendered:
            source = init_project.render_profile(source, "self-hosted-mac")
        expected_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        actual_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        assert actual_hash == expected_hash, f"Hash mismatch for {relative}"

    # Executable permissions binding
    executable_files = {".aru/verify.sh", ".aru/verify-project.sh", ".aru/hooks/pre-push", ".aru/hooks/enforce_touches.py"}
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
    secret_re = match.group(1).replace(r"\"", '"')

    positives = [
        "gh" + "p_123456789012345678901234567890123456",
        "github_pat_" + "123456789012345678901234567890123456789012345678901234567890",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "xox" + "b-123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
        "sk-" + "123456789012345678901234567890123456",
        "sk-proj-" + "abc123def456ghi789jkl012mno345pqr678stu901vwx_yz-123456",
        'API_SECRET_KEY="'
        + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        + '"',
        "API_SECRET_KEY=" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        'JMC_API_SECRET="'
        + "c4d9e32e4518ff6adffb23ba8cc224450e9ec6ffefd862451d449d85331480e2"
        + '"',
        "JMC_API_SECRET=" + "c4d9e32e4518ff6adffb23ba8cc224450e9ec6ffefd862451d449d85331480e2",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
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


def _init_git_repo(path: init_project.Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)


@pytest.mark.parametrize("operation", ["rename", "copy"])
def test_verify_template_classifies_nul_paths_end_to_end(tmp_path, operation):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    source = ".aru/old\tline\ncafé_🚀.txt"
    destination = "moved/new\tline\ncafé_🚀.txt"
    (tmp_path / source).write_text("governance-adjacent content\n", encoding="utf-8")
    (tmp_path / ".aru" / "verify.sh").chmod(0o644)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=tmp_path,
        check=True,
    )

    (tmp_path / "moved").mkdir()
    if operation == "rename":
        subprocess.run(["git", "mv", source, destination], cwd=tmp_path, check=True)
    else:
        (tmp_path / destination).write_bytes((tmp_path / source).read_bytes())
        subprocess.run(["git", "add", destination], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-am", operation],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert ".aru/verify.sh must be executable" in result.stderr
    assert '".aru/old\\tline\\ncaf\\u00e9_\\ud83d\\ude80.txt"' in result.stdout
    assert '"moved/new\\tline\\ncaf\\u00e9_\\ud83d\\ude80.txt"' in result.stdout


@pytest.mark.parametrize(
    "evidence",
    [
        b"M\x00unterminated.py",
        b"R100\x00only-one-side.py\x00",
        b"M\x00valid.py\x00extra\x00",
    ],
)
def test_verify_template_fails_closed_on_malformed_nul_evidence(tmp_path, evidence):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=tmp_path, check=True
    )
    (tmp_path / "ordinary.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "ordinary.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "change"], cwd=tmp_path, check=True, capture_output=True
    )

    evidence_file = tmp_path / "malformed.diff-z"
    evidence_file.write_bytes(evidence)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    git_shim = shim_dir / "git"
    git_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$3" = "diff" ]; then\n'
        '  cat "$ARU_TEST_EVIDENCE"\n'
        "  exit 0\n"
        "fi\n"
        'exec "$ARU_REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    git_shim.chmod(0o755)
    real_git = shutil.which("git")
    assert real_git is not None
    env = {
        **os.environ,
        "ARU_REAL_GIT": real_git,
        "ARU_TEST_EVIDENCE": str(evidence_file),
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", ".aru/verify.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 1
    assert "changed-path evidence is malformed" in result.stderr


def test_verify_template_secret_scan_catches_runtime_generated_diff_inputs():
    path = init_project.Path(__file__).resolve().parents[1] / "templates/verify.sh"
    content = path.read_text(encoding="utf-8")

    match = re.search(r'secret_re="(.*?)"\s*$', content, re.MULTILINE)
    assert match is not None
    secret_re = match.group(1).replace(r"\"", '"')

    # Positive test: Runtime-constructed secret added in diff is caught by fail-closed scanner pipeline
    token = "gh" + "p_" + "1234567890" * 4
    diff_with_token = f"+ {token}\n"
    cmd = "grep -a -E '^\\+' || true"
    res = subprocess.run(
        ["bash", "-c", cmd], input=diff_with_token, text=True, capture_output=True, check=True
    )
    scan_res = subprocess.run(
        ["grep", "-a", "-Eq", secret_re],
        input=res.stdout,
        text=True,
        capture_output=True,
        check=False,
    )
    assert scan_res.returncode == 0, "Expected generated secret in added diff line to be caught"

    # Positive test: Runtime-constructed binary secret with NUL bytes is caught by pipeline
    binary_diff = b"+ \x00\x01\x02" + token.encode("ascii") + b"\x00\x03\n"
    res_bin = subprocess.run(
        ["bash", "-c", cmd], input=binary_diff, capture_output=True, check=True
    )
    scan_bin = subprocess.run(
        ["grep", "-a", "-Eq", secret_re],
        input=res_bin.stdout,
        capture_output=True,
        check=False,
    )
    assert (
        scan_bin.returncode == 0
    ), "Expected generated binary secret in added diff line to be caught"

    # Positive test: Runtime-constructed project secret in code diff is caught
    proj_token = "sk-" + "proj-" + "abc123def456ghi789jkl012mno345pqr678stu901vwx_yz-" + "123456"
    test_file_diff = f"+ # in tests/test_auth.py\n+ TOKEN = '{proj_token}'\n"
    res = subprocess.run(
        ["bash", "-c", cmd], input=test_file_diff, text=True, capture_output=True, check=True
    )
    scan_res = subprocess.run(
        ["grep", "-a", "-Eq", secret_re],
        input=res.stdout,
        text=True,
        capture_output=True,
        check=False,
    )
    assert scan_res.returncode == 0, "Expected generated test file secret to be caught"

    # Negative test: Non-secret added line with placeholder passes
    clean_diff = '+ API_SECRET_KEY="your-long-random-secret-key-min-32-chars"\n+ python3 main.py\n'
    res = subprocess.run(
        ["bash", "-c", cmd], input=clean_diff, text=True, capture_output=True, check=True
    )
    scan_res = subprocess.run(
        ["grep", "-a", "-Eq", secret_re],
        input=res.stdout,
        text=True,
        capture_output=True,
        check=False,
    )
    assert scan_res.returncode != 0, "Expected clean placeholder diff to pass without detection"


def test_verify_template_secret_scan_catches_binary_credentials_end_to_end(tmp_path):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=tmp_path, check=True
    )

    secret = "gh" + "p_" + "1234567890" * 4
    binary_payload = b"\x00\x01\x02\xff" + secret.encode("ascii") + b"\x00\xfe\n"
    (tmp_path / "payload.bin").write_bytes(binary_payload)
    subprocess.run(["git", "add", "payload.bin"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add binary credential"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["bash", ".aru/verify.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "credential-shaped literal found in the verified content" in result.stderr


def test_verify_template_secret_scan_allows_safe_binary_control_end_to_end(tmp_path):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    (tmp_path / ".aru/verify-project.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=tmp_path, check=True
    )

    safe_binary = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
    (tmp_path / "image.png").write_bytes(safe_binary)
    subprocess.run(["git", "add", "image.png"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add safe binary"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result = subprocess.run(
        ["bash", ".aru/verify.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "no credential-shaped literal found" in result.stdout
    assert "proportional verification passed" in result.stdout


def test_verify_template_secret_scan_fallback_tree_mode_with_binary_content_end_to_end(tmp_path):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    (tmp_path / ".aru/verify-project.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    safe_binary = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))
    (tmp_path / "image.png").write_bytes(safe_binary)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init safe tree"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Safe binary in fallback mode (no origin/main comparison base)
    result = subprocess.run(
        ["bash", ".aru/verify.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "no credential-shaped literal found" in result.stdout
    assert "full tracked tree (no comparison base resolved)" in result.stdout

    # Add binary credential in fallback mode
    secret = "gh" + "p_" + "1234567890" * 4
    (tmp_path / "secret.bin").write_bytes(b"\x00\x01" + secret.encode("ascii") + b"\x00")
    subprocess.run(["git", "add", "secret.bin"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add secret binary"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    result_secret = subprocess.run(
        ["bash", ".aru/verify.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result_secret.returncode == 1
    assert "credential-shaped literal found in the verified content" in result_secret.stderr


def test_verify_template_executable_rejects_indented_write_permission_on_macos(tmp_path):
    _init_git_repo(tmp_path)
    init_project.scaffold("consumer", tmp_path, runner_profile="self-hosted-mac")
    (tmp_path / ".aru/verify-project.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    wf = tmp_path / ".github/workflows/governed-pr.yml"
    content = wf.read_text(encoding="utf-8")
    wf.write_text(
        content.replace("permissions:\n  contents: read", "permissions:\n  contents: write"),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "commit", "-a", "-m", "add write perm"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    res = subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert res.returncode == 1
    assert "governed workflow permissions must stay read-only" in res.stderr

    wf.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-a", "-m", "restore read perm"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    res_ok = subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert res_ok.returncode == 0
    assert "proportional verification passed" in res_ok.stdout


def test_verify_template_workflow_permissions_portable_gate():
    path = init_project.Path(__file__).resolve().parents[1] / "templates/verify.sh"
    content = path.read_text(encoding="utf-8")

    match = re.search(r"if grep -Eq '([^']+)' \"\$\{workflow\}\"; then", content)
    assert match is not None
    perm_re = match.group(1)

    # Indented write / admin permissions must be rejected
    rejected = [
        "permissions:\n  contents: write\n",
        "permissions:\n\tcontents: write\n",
        "permissions:\n    contents: write\n",
        "contents: write\n",
        "permissions:\n  issues: write\n",
        "permissions:\n  pull-requests: write\n",
        "permissions:\n  actions: write\n",
        "permissions:\n  checks: write\n",
        "permissions:\n  deployments: write\n",
        "permissions:\n  packages: write\n",
        "permissions:\n  id-token: write\n",
        "permissions:\n  contents: admin\n",
        "permissions:\n  actions: admin\n",
    ]
    for item in rejected:
        res = subprocess.run(
            ["grep", "-Eq", perm_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode == 0, f"Expected rejection for:\n{item}"

    # Read-only permissions must pass
    accepted = [
        "permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n",
        "permissions:\n  actions: read\n  checks: read\n",
        "permissions:\n  deployments: read\n  packages: read\n  id-token: read\n",
        "permissions: read-all\n",
        "permissions: {}\n",
    ]
    for item in accepted:
        res = subprocess.run(
            ["grep", "-Eq", perm_re],
            input=item,
            text=True,
            capture_output=True,
            check=False,
        )
        assert res.returncode != 0, f"Expected acceptance for:\n{item}"
