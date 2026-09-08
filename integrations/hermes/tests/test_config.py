"""Consumer runner-profile declaration and account policy in Driver config."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver.config import Config, DriverError  # noqa: E402
from aru_project_driver.kernel import (  # noqa: E402
    ACCOUNT_RUNNER_PROFILES, RUNNER_PROFILES,
)

ROOT = Path(__file__).resolve().parents[3]


def kernel_init_project():
    """Load the kernel's bootstrap module without importing the package."""
    spec = importlib.util.spec_from_file_location(
        "_driver_test_init_project", ROOT / "scripts" / "init_project.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return module


def write_config(tmp_path, repo="gillella/repo", **project):
    home = tmp_path / "home"
    raw = {
        "version": 1, "hermes_home": str(home), "hermes_repo": str(tmp_path / "runtime"),
        "state_dir": str(home / "state" / "aru_project_driver"),
        "kernel_root": str(tmp_path / "kernel"),
        "projects": {repo: {"repo_dir": str(tmp_path / "repo"), "lanes": ["agent-one"], **project}},
        "lanes": {"agent-one": {
            "family": "openai-codex", "capacity_key": "account-one", "projects": [repo],
            "command": [sys.executable, "-c", "pass", "{prompt}"],
            "capacity_command": [sys.executable, "-c", "pass"],
            "probe_command": [sys.executable, "-c", "pass"],
        }},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return path


def test_kernel_and_driver_publish_the_same_account_runner_policy():
    kernel = kernel_init_project()
    assert kernel.ACCOUNT_RUNNER_PROFILES == ACCOUNT_RUNNER_PROFILES
    assert tuple(sorted(kernel.RUNNER_PROFILES)) == tuple(sorted(RUNNER_PROFILES))
    assert ACCOUNT_RUNNER_PROFILES == {
        "gillella": "self-hosted-mac",
        "unum-inc": "github-hosted",
    }


@pytest.mark.parametrize(
    ("repo", "expected"),
    [("gillella/repo", "self-hosted-mac"), ("Unum-Inc/repo", "github-hosted")],
)
def test_account_assignment_is_used_when_no_profile_is_declared(tmp_path, repo, expected):
    config = Config(write_config(tmp_path, repo=repo))
    assert config.runner_profile(repo) == expected


@pytest.mark.parametrize(
    ("repo", "declared"),
    [("gillella/repo", "self-hosted-mac"), ("Unum-Inc/repo", "github-hosted")],
)
def test_an_agreeing_declaration_is_accepted(tmp_path, repo, declared):
    config = Config(write_config(tmp_path, repo=repo, runner_profile=declared))
    assert config.runner_profile(repo) == declared


@pytest.mark.parametrize(
    ("repo", "declared", "message"),
    [
        ("gillella/repo", "github-hosted", "contradicts"),
        ("Unum-Inc/repo", "self-hosted-mac", "contradicts"),
        ("gillella/repo", "self-hosted-linux", "unknown runner_profile"),
        ("gillella/repo", "", "unknown runner_profile"),
        ("gillella/repo", 1, "unknown runner_profile"),
    ],
)
def test_unknown_or_contradictory_declarations_are_refused(tmp_path, repo, declared, message):
    with pytest.raises(DriverError, match=message):
        Config(write_config(tmp_path, repo=repo, runner_profile=declared))


def test_unassigned_account_must_declare_a_profile(tmp_path):
    repo = "someone-else/repo"
    config = Config(write_config(tmp_path, repo=repo))
    with pytest.raises(DriverError, match="no runner profile is declared or assigned"):
        config.runner_profile(repo)


def test_unassigned_account_may_declare_a_known_profile(tmp_path):
    repo = "someone-else/repo"
    config = Config(write_config(tmp_path, repo=repo, runner_profile="github-hosted"))
    assert config.runner_profile(repo) == "github-hosted"


def test_an_unconfigured_repository_has_no_profile(tmp_path):
    config = Config(write_config(tmp_path))
    with pytest.raises(DriverError, match="not configured for this Driver"):
        config.runner_profile("gillella/other")
