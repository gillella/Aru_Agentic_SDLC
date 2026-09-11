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


@pytest.mark.parametrize("declared", ["github-hosted", "self-hosted-mac"])
def test_unassigned_account_cannot_declare_a_profile(tmp_path, declared):
    # Admission would block this account anyway; loading must refuse it the same way.
    repo = "someone-else/repo"
    with pytest.raises(DriverError, match="no assigned profile"):
        Config(write_config(tmp_path, repo=repo, runner_profile=declared))


def test_an_unconfigured_repository_has_no_profile(tmp_path):
    config = Config(write_config(tmp_path))
    with pytest.raises(DriverError, match="not configured for this Driver"):
        config.runner_profile("gillella/other")


@pytest.mark.parametrize("epoch", [None, True, "", "a" * 65, "unsafe revision"])
def test_worker_retry_epoch_requires_explicit_bounded_revision(tmp_path, epoch):
    with pytest.raises(DriverError, match="worker_retry_epoch"):
        Config(write_config(tmp_path, worker_retry_epoch=epoch))


@pytest.mark.parametrize("value,ok", [(1, True), (8, True), (0, False), (9, False), ("2", False), (True, False), (2.0, False)])
def test_max_sessions_is_a_bounded_integer(tmp_path, value, ok):
    config_path = write_config(tmp_path, repo="gillella/repo")
    raw = json.loads(config_path.read_text())
    for lane in raw["lanes"].values():
        lane["max_sessions"] = value
    config_path.write_text(json.dumps(raw))
    if ok:
        assert all(lane["max_sessions"] == value for lane in Config(config_path).lanes.values())
    else:
        with pytest.raises(DriverError, match="max_sessions must be between 1 and 8"):
            Config(config_path)


def test_lanes_sharing_a_subscription_must_agree_on_max_sessions(tmp_path):
    config_path = write_config(tmp_path, repo="gillella/repo")
    raw = json.loads(config_path.read_text())
    first, second = list(raw["lanes"])[:1] * 2 if len(raw["lanes"]) == 1 else list(raw["lanes"])[:2]
    if first == second:  # duplicate the single lane onto the same subscription
        raw["lanes"]["alias"] = dict(raw["lanes"][first])
        second = "alias"
        raw["projects"]["gillella/repo"]["lanes"].append("alias")
    raw["lanes"][second]["capacity_key"] = raw["lanes"][first]["capacity_key"]
    raw["lanes"][first]["max_sessions"], raw["lanes"][second]["max_sessions"] = 2, 1
    config_path.write_text(json.dumps(raw))
    with pytest.raises(DriverError, match="declare different max_sessions"):
        Config(config_path)


@pytest.mark.parametrize("value", [None, [], ["agent-one"], "old-inventory"])
def test_retired_reviewer_config_requires_operator_removal(tmp_path, value):
    path = write_config(tmp_path, coding_reviewers=value)
    with pytest.raises(DriverError, match="delete coding_reviewers from the Driver config"):
        Config(path)
