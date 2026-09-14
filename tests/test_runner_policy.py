"""Consumer runner profiles: account policy, scaffold and verification agree."""

from __future__ import annotations

import json
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
        # An unassigned account declaring nothing is still refused: no default.
        # It is no longer refused *by name* once it declares a profile -- see
        # test_an_unassigned_account_may_adopt_by_declaring_its_profile (#698).
        ("someone-else", None, "has no declared runner profile"),
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


def _scaffold(tmp_path, profile):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    init_project.scaffold("consumer", tmp_path, runner_profile=profile)
    (tmp_path / ".aru/verify-project.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    return git


def _run_verify(tmp_path):
    return subprocess.run(["bash", ".aru/verify.sh"], cwd=tmp_path,
                          capture_output=True, text=True, check=False)


def _verify(tmp_path, profile, mutate=None, rehash=None):
    """Scaffold one profile, optionally corrupt the workflow, then verify.

    The managed-file integrity section runs first and unconditionally, so a mutated
    workflow fails there unless its hash is updated. `rehash` does that, which is the
    documented residual and keeps the governance assertions below testing governance.
    """
    git = _scaffold(tmp_path, profile)
    workflow = tmp_path / ".github/workflows/governed-pr.yml"
    if mutate is not None:
        workflow.write_text(mutate(workflow.read_text(encoding="utf-8")), encoding="utf-8")
        if rehash is not None:
            rehash(tmp_path)
    git("add", ".")
    git("commit", "-m", "init")
    return _run_verify(tmp_path)


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
def test_verification_refuses_a_workflow_that_contradicts_its_profile(
    tmp_path, profile, old, new, message, rehash_manifest,
):
    result = _verify(tmp_path, profile, lambda raw: raw.replace(old, new), rehash_manifest)
    assert result.returncode == 1
    assert message in result.stderr


# --- managed-file integrity: the first section, and the only unconditional one ---

MANIFEST = ".aru/manifest.json"


@pytest.mark.parametrize("profile", [MAC, HOSTED])
def test_integrity_passes_on_an_untouched_scaffold(tmp_path, profile):
    result = _verify(tmp_path, profile)
    assert result.returncode == 0, result.stderr
    assert "managed files match .aru/manifest.json" in result.stdout
    assert f"profile {profile}" in result.stdout


def _managed_paths(tmp_path):
    return sorted(json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))["files"])


@pytest.mark.parametrize("profile", [MAC, HOSTED])
def test_integrity_refuses_each_tampered_managed_file(tmp_path, profile):
    git = _scaffold(tmp_path, profile)
    git("add", ".")
    git("commit", "-m", "init")
    managed = _managed_paths(tmp_path)
    assert managed, "the scaffold must write a manifest that lists files"
    for relative in managed:
        original = (tmp_path / relative).read_bytes()
        (tmp_path / relative).write_bytes(original + b"\n# tampered\n")
        result = _run_verify(tmp_path)
        (tmp_path / relative).write_bytes(original)
        assert result.returncode == 1, relative
        assert f"{relative}: content differs from the manifest" in result.stderr
        assert "init_project.py --sync" in result.stderr
        assert "cannot stop a head that rewrites .aru/verify.sh" in result.stderr


def test_integrity_refuses_a_missing_managed_file(tmp_path):
    git = _scaffold(tmp_path, MAC)
    git("add", ".")
    git("commit", "-m", "init")
    (tmp_path / ".aru/lib/touches.py").unlink()
    result = _run_verify(tmp_path)
    assert result.returncode == 1
    assert ".aru/lib/touches.py: missing, unreadable" in result.stderr


@pytest.mark.parametrize(
    ("document", "message"),
    [("{}\n", "malformed"), ("not json\n", "malformed"), (None, "is missing")],
)
def test_integrity_refuses_a_malformed_or_missing_manifest(tmp_path, document, message):
    git = _scaffold(tmp_path, MAC)
    git("add", ".")
    git("commit", "-m", "init")
    if document is None:
        (tmp_path / MANIFEST).unlink()
    else:
        (tmp_path / MANIFEST).write_text(document, encoding="utf-8")
    result = _run_verify(tmp_path)
    assert result.returncode == 1
    assert message in result.stderr
    assert "init_project.py --sync" in result.stderr


def test_integrity_runs_without_a_governance_change_in_scope(tmp_path):
    """The point of running first: the section that catches tampering is not itself
    gated on the pull request having touched a governance path."""
    git = _scaffold(tmp_path, MAC)
    (tmp_path / ".aru/lib/touches.py").write_text("# tampered\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "init")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("value = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "product change only")
    result = _run_verify(tmp_path)
    assert result.returncode == 1
    assert ".aru/lib/touches.py: content differs from the manifest" in result.stderr


def test_a_rehashed_manifest_is_the_documented_residual(tmp_path, rehash_manifest):
    """Stated in the contract, the register and the failure message: this section
    detects drift. It cannot stop an author who edits a managed file and regenerates
    the manifest in the same head; `aru-merge-policy` judges that from the base branch.
    """
    git = _scaffold(tmp_path, MAC)
    (tmp_path / ".aru/lib/touches.py").write_text("# tampered\n", encoding="utf-8")
    rehash_manifest(tmp_path)
    git("add", ".")
    git("commit", "-m", "init")
    result = _run_verify(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "managed files match .aru/manifest.json" in result.stdout


# --- account assignments are data, not a source patch (#698) ----------------

def test_known_accounts_resolve_exactly_as_before():
    assert init_project.resolve_runner_profile("gillella", None) == "self-hosted-mac"
    assert init_project.resolve_runner_profile("Unum-Inc", None) == "github-hosted"
    assert init_project.resolve_runner_profile("UNUM-INC", None) == "github-hosted"


def test_assignments_come_from_the_policy_declaration():
    import policy
    assert init_project.ACCOUNT_RUNNER_PROFILES == policy.account_runner_profiles()
    assert policy.account_runner_profiles()["gillella"] == "self-hosted-mac"


def test_an_unassigned_account_may_adopt_by_declaring_its_profile():
    # The portability fix: a third party is no longer refused by name.
    assert init_project.resolve_runner_profile("newco", "github-hosted") == "github-hosted"
    assert init_project.resolve_runner_profile("newco", "self-hosted-mac") == "self-hosted-mac"


def test_an_unassigned_account_declaring_nothing_is_still_refused():
    # There is no default; fail-closed is preserved.
    with pytest.raises(init_project.BootstrapError, match="pass --runner-profile"):
        init_project.resolve_runner_profile("newco", None)


def test_a_declaration_contradicting_an_assignment_is_still_refused():
    with pytest.raises(init_project.BootstrapError, match="is assigned the self-hosted-mac"):
        init_project.resolve_runner_profile("gillella", "github-hosted")


def test_an_unknown_profile_name_is_still_refused():
    with pytest.raises(init_project.BootstrapError, match="unknown runner profile"):
        init_project.resolve_runner_profile("newco", "some-other-profile")


def test_a_malformed_assignment_table_refuses():
    import policy
    base = policy.load()
    for table in ({}, {"gillella": 5}, {"gillella": ""}):
        broken = {**base, "runner_profiles": {"accounts": table}}
        with pytest.raises(policy.PolicyError):
            policy.account_runner_profiles(broken)
