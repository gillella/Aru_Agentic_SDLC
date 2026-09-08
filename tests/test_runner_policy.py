"""Consumer runner profiles: account policy, scaffold and verification agree."""

from __future__ import annotations

import subprocess

import pytest
import yaml

import init_project

BootstrapError = init_project.BootstrapError
ROOT = init_project.Path(__file__).resolve().parents[1]
MAC = "self-hosted-mac"
HOSTED = "github-hosted"
MAC_RUNS_ON = "runs-on: [self-hosted, macOS, ARM64, aru-ci]"
MARKER_MAC = f"# aru-runner-profile: {MAC}"
MARKER_HOSTED = f"# aru-runner-profile: {HOSTED}"


@pytest.mark.parametrize(
    ("owner", "declared", "expected"),
    [
        ("gillella", None, MAC), ("GILLELLA", None, MAC), ("gillella", MAC, MAC),
        ("Unum-Inc", None, HOSTED), ("unum-inc", None, HOSTED), ("Unum-Inc", HOSTED, HOSTED),
        (None, HOSTED, HOSTED), (None, MAC, MAC),
    ],
)
def test_account_policy_resolves_one_profile(owner, declared, expected):
    assert init_project.resolve_runner_profile(owner, declared) == expected


@pytest.mark.parametrize(
    ("owner", "declared", "message"),
    [
        ("gillella", HOSTED, "assigned the self-hosted-mac runner profile"),
        ("Unum-Inc", MAC, "assigned the github-hosted runner profile"),
        ("someone-else", None, "no runner profile is assigned to account"),
        ("someone-else", MAC, "no runner profile is assigned to account"),
        ("gillella", "ubuntu", "unknown runner profile"),
        (None, None, "must select a runner profile"),
    ],
)
def test_unknown_or_contradictory_profiles_are_refused(owner, declared, message):
    with pytest.raises(BootstrapError, match=message):
        init_project.resolve_runner_profile(owner, declared)


def test_rendering_refuses_unknown_profiles_and_leaves_no_token():
    template = (ROOT / "templates/AGENTS.md").read_text(encoding="utf-8")
    with pytest.raises(BootstrapError, match="unknown runner profile"):
        init_project.render_profile(template, "self-hosted-linux")
    with pytest.raises(BootstrapError, match="unresolved scaffold token"):
        init_project.render_profile("__ARU_RUNS_ON__ and __ARU_UNKNOWN_TOKEN__", MAC)
    for profile in init_project.RUNNER_PROFILES:
        assert "__ARU_" not in init_project.render_profile(template, profile)


@pytest.mark.parametrize(
    ("profile", "runs_on", "absent"),
    [(MAC, ["self-hosted", "macOS", "ARM64", "aru-ci"], "ubuntu"), (HOSTED, "ubuntu-latest", "self-hosted")],
)
def test_scaffold_binds_the_workflow_and_instructions_to_one_profile(tmp_path, profile, runs_on, absent):
    init_project.scaffold("consumer", tmp_path, runner_profile=profile)
    raw = (tmp_path / ".github/workflows/governed-pr.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)
    assert workflow["jobs"]["governed-pr"]["runs-on"] == runs_on
    assert f"# aru-runner-profile: {profile}" in raw
    assert absent not in raw
    # Profile-independent guarantees survive in both renderings.
    assert workflow["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read"}
    assert workflow["jobs"]["governed-pr"]["name"] == "aru-governed-pr"
    assert "bash .aru/verify.sh" in raw
    assert '--expected-head "$ARU_EXPECTED_HEAD"' in raw
    assert profile in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_repository_creation_refuses_a_cross_account_profile(monkeypatch, tmp_path):
    def fake_command(argv, *, cwd, json_output=False, auth=None):
        if argv[:3] == ["gh", "repo", "view"]:
            return {"nameWithOwner": "Unum-Inc/consumer"}
        return ""

    monkeypatch.setattr(init_project, "command", fake_command)
    with pytest.raises(BootstrapError, match="not assigned the self-hosted-mac runner profile"):
        init_project.github_setup("consumer", tmp_path, private=True, runner_profile=MAC)


def _verify(tmp_path, profile, mutate=None):
    """Scaffold one profile, optionally corrupt the workflow, then verify."""
    run = {"cwd": tmp_path, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-b", "main"], **run)
    subprocess.run(["git", "config", "user.name", "Test"], **run)
    subprocess.run(["git", "config", "user.email", "test@example.com"], **run)
    init_project.scaffold("consumer", tmp_path, runner_profile=profile)
    workflow = tmp_path / ".github/workflows/governed-pr.yml"
    if mutate is not None:
        workflow.write_text(mutate(workflow.read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return subprocess.run(
        ["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("profile", [MAC, HOSTED])
def test_verification_accepts_the_workflow_of_its_declared_profile(tmp_path, profile):
    result = _verify(tmp_path, profile)
    assert result.returncode == 0, result.stderr
    assert f"governed workflow: {profile} profile" in result.stdout


@pytest.mark.parametrize(
    ("profile", "old", "new", "message"),
    [
        # The declared profile and the actual compute target must agree.
        (MAC, MAC_RUNS_ON, "runs-on: ubuntu-latest", "self-hosted-mac runner profile requires exactly"),
        (HOSTED, "runs-on: ubuntu-latest", MAC_RUNS_ON, "github-hosted runner profile requires exactly"),
        # A hosted repository must never reach a personal machine.
        (HOSTED, "timeout-minutes: 20", "timeout-minutes: 20 # self-hosted", "must not contain: self-hosted"),
        (MAC, "timeout-minutes: 20", "runs-on: ubuntu-latest\n    timeout-minutes: 20", "must not contain: runs-on:"),
        # An unknown, absent or duplicated declaration fails closed.
        (MAC, MARKER_MAC, "# aru-runner-profile: ubuntu", "unknown runner profile: ubuntu"),
        (MAC, MARKER_MAC + "\n", "", "exactly one '# aru-runner-profile:' line"),
        (HOSTED, MARKER_HOSTED, MARKER_HOSTED + "\n" + MARKER_HOSTED, "exactly one '# aru-runner-profile:' line"),
    ],
)
def test_verification_refuses_a_workflow_that_contradicts_its_profile(tmp_path, profile, old, new, message):
    result = _verify(tmp_path, profile, lambda raw: raw.replace(old, new))
    assert result.returncode == 1
    assert message in result.stderr
