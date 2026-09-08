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
    job = yaml.safe_load(raw)["jobs"]["governed-pr"]
    assert (job["runs-on"], job["name"]) == (runs_on, "aru-governed-pr")
    assert f"# aru-runner-profile: {profile}" in raw and absent not in raw
    # Profile-independent guarantees survive in both renderings.
    assert yaml.safe_load(raw)["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read"}
    assert "bash .aru/verify.sh" in raw and '--expected-head "$ARU_EXPECTED_HEAD"' in raw
    assert profile in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("owner", "message"),
    [
        # gh created the repository in Unum-Inc: stop before labels, Project or ruleset.
        ("gillella", "created outside the requested account gillella"),
        # A matching owner still cannot provision a workflow bound to the other profile.
        ("Unum-Inc", "not assigned the self-hosted-mac runner profile"),
        ("", "unsafe GitHub owner"), ("bad/owner", "unsafe GitHub owner"), ("-dash", "unsafe GitHub owner"),
    ],
)
def test_repository_creation_refuses_a_cross_account_profile(monkeypatch, tmp_path, owner, message):
    calls = []

    def fake_command(argv, *, cwd, json_output=False, auth=None):
        calls.append(list(argv))
        return {"nameWithOwner": "Unum-Inc/consumer"} if argv[:3] == ["gh", "repo", "view"] else ""

    monkeypatch.setattr(init_project, "command", fake_command)
    with pytest.raises(BootstrapError, match=message):
        init_project.github_setup("consumer", tmp_path, private=True, owner=owner, runner_profile=MAC)
    assert not any(call[:3] == ["gh", "label", "create"] for call in calls)


def test_github_bootstrap_requires_an_owner(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_project, "scaffold", lambda *a, **k: pytest.fail("must fail before scaffold"))
    argv = ["init_project.py", "--name", "consumer", "--directory", str(tmp_path), "--github", "--runner-profile", MAC]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        init_project.main()
    assert "--github requires --owner" in capsys.readouterr().err


def _verify(tmp_path, profile, mutate=None):
    """Scaffold one profile, optionally corrupt the workflow, then verify."""
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    init_project.scaffold("consumer", tmp_path, runner_profile=profile)
    workflow = tmp_path / ".github/workflows/governed-pr.yml"
    if mutate is not None:
        workflow.write_text(mutate(workflow.read_text(encoding="utf-8")), encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")
    return subprocess.run(["bash", ".aru/verify.sh"], cwd=tmp_path, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("profile", [MAC, HOSTED])
def test_verification_accepts_the_workflow_of_its_declared_profile(tmp_path, profile):
    result = _verify(tmp_path, profile)
    assert result.returncode == 0, result.stderr
    assert f"governed workflow: {profile} profile" in result.stdout


@pytest.mark.parametrize(
    ("profile", "old", "new", "message"),
    [
        # The declared profile and the ACTIVE compute target must agree; commented copies,
        # generic self-hosted targets and additional runners are refused.
        (MAC, MAC_RUNS_ON, "runs-on: ubuntu-latest", "self-hosted-mac runner profile requires exactly"),
        (HOSTED, "runs-on: ubuntu-latest", MAC_RUNS_ON, "github-hosted runner profile requires exactly"),
        (MAC, "timeout-minutes: 20", "runs-on: ubuntu-latest\n    timeout-minutes: 20", "requires exactly one active"),
        (MAC, MAC_RUNS_ON, f"# {MAC_RUNS_ON}\n    runs-on: self-hosted", "requires exactly one active"),
        (MAC, MAC_RUNS_ON, f"{MAC_RUNS_ON} # {MAC_RUNS_ON}\n    runs-on: [self-hosted, linux]", "requires exactly one active"),
        (HOSTED, "runs-on: ubuntu-latest", "# runs-on: ubuntu-latest\n    runs-on: ubuntu-22.04", "requires exactly one active"),
        # A hosted repository must never reach a personal machine, even in a comment.
        (HOSTED, "timeout-minutes: 20", "timeout-minutes: 20 # self-hosted", "must not contain: self-hosted"),
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
