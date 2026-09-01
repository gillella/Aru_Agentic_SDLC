from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

import common
import create_pr


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _checkout(tmp_path: Path, remotes: dict[str, str], name: str = "checkout") -> Path:
    repository = tmp_path / name
    repository.mkdir()
    _git(repository, "init", "-q")
    for remote, url in remotes.items():
        _git(repository, "remote", "add", remote, url)
    return repository


def _stub_runner(tmp_path: Path, record: Path, payload: str = "{}", config: Path | None = None) -> Path:
    """Write a stub App runner that records its arguments instead of minting a token."""
    observe_config = (
        f"printf '%s' \"${{GH_CONFIG_DIR-unset}}\" > {shlex.quote(str(config))}\n" if config else ""
    )
    runner = tmp_path / "app-run"
    runner.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(record))}\n"
        f"{observe_config}"
        f"printf '%s' {shlex.quote(payload)}\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    return runner


def _recorded(record: Path) -> list[str]:
    return record.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:owner/consumer.git", "owner/consumer"),
        ("https://github.com/owner/consumer.git", "owner/consumer"),
        ("https://github.com/owner/consumer", "owner/consumer"),
        ("ssh://git@github.com/owner/consumer.git", "owner/consumer"),
        ("git@github.com:gillella/jaji-mission-control.git", "gillella/jaji-mission-control"),
        ("https://github.com/gillella/Aru_Agentic_SDLC.git", "gillella/Aru_Agentic_SDLC"),
    ],
)
def test_repository_commands_pass_the_checkout_repository_to_the_app_runner(
    monkeypatch, tmp_path, url, slug
):
    checkout = _checkout(tmp_path, {"origin": url})
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    common.run(["gh", "issue", "view", "7"], cwd=checkout)

    assert _recorded(record) == ["--repo", slug, "--", "gh", "issue", "view", "7"]


def test_factory_self_operations_still_target_the_factory_repository(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path, {"origin": "git@github.com:gillella/Aru_Agentic_SDLC.git"})
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    common.run(["gh", "pr", "view", "12"], cwd=checkout)

    assert _recorded(record) == ["--repo", "gillella/Aru_Agentic_SDLC", "--", "gh", "pr", "view", "12"]


def test_origin_is_authoritative_when_other_remotes_exist(monkeypatch, tmp_path):
    checkout = _checkout(
        tmp_path,
        {
            "origin": "git@github.com:gillella/jaji-mission-control.git",
            "upstream": "git@github.com:gillella/Aru_Agentic_SDLC.git",
        },
    )
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    common.run(["gh", "issue", "view", "7"], cwd=checkout)

    assert _recorded(record)[:2] == ["--repo", "gillella/jaji-mission-control"]


def test_single_named_remote_resolves_without_an_origin(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path, {"governed": "git@github.com:owner/consumer.git"})
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    common.run(["gh", "issue", "view", "7"], cwd=checkout)

    assert _recorded(record)[:2] == ["--repo", "owner/consumer"]


@pytest.mark.parametrize(
    ("remotes", "message"),
    [
        ({}, "unable to resolve the governed repository identity"),
        ({"origin": "https://gitlab.com/owner/consumer.git"}, "unable to resolve"),
        ({"origin": "https://github.com/owner"}, "unable to resolve"),
        ({"origin": "https://github.com/owner/consumer/extra.git"}, "unable to resolve"),
        ({"origin": "https://github.com.evil.example/owner/consumer.git"}, "unable to resolve"),
        ({"origin": "https://github.com/owner/..git"}, "unable to resolve"),
        (
            {"one": "git@github.com:owner/consumer.git", "two": "git@github.com:owner/other.git"},
            "governed repository identity is ambiguous",
        ),
    ],
)
def test_absent_or_ambiguous_identity_fails_closed_without_invoking_the_runner(
    monkeypatch, tmp_path, remotes, message
):
    checkout = _checkout(tmp_path, remotes)
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    with pytest.raises(common.KernelError, match=message):
        common.run(["gh", "issue", "view", "7"], cwd=checkout)

    assert not record.exists()


def test_repository_identity_is_not_derived_outside_a_checkout(monkeypatch, tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    with pytest.raises(common.KernelError, match="unable to resolve the governed repository identity"):
        common.run(["gh", "issue", "view", "7"], cwd=outside)

    assert not record.exists()


@pytest.mark.parametrize(
    ("command", "use_runner"),
    [(["gh", "issue", "view", "7"], True), (["gh", "issue", "view", "7"], False), (["gh", "pr", "view", "7"], False)],
)
def test_repository_command_runner_resolution(monkeypatch, tmp_path, command, use_runner):
    checkout = _checkout(tmp_path, {"origin": "git@github.com:owner/consumer.git"})
    calls = []
    if use_runner:
        runner = tmp_path / "app-run"
        runner.write_text("#!/bin/sh\n", encoding="utf-8")
        runner.chmod(0o755)
        monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(runner))
        expected_argv = [str(runner), "--repo", "owner/consumer", "--", *command]
    else:
        monkeypatch.delenv("ARU_GITHUB_APP_RUNNER", raising=False)
        expected_argv = list(command)
    real_run = common.subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "git":
            return real_run(argv, **kwargs)
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    common.run(command, cwd=checkout)
    assert calls[0][0] == expected_argv
    assert calls[0][1]["env"] is None


def test_configured_app_runner_must_be_executable(monkeypatch, tmp_path):
    runner = tmp_path / "missing-app-run"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(runner))
    with pytest.raises(common.KernelError, match="not executable"):
        common.run(["gh", "issue", "view", "7"])


def test_project_commands_never_receive_repository_identity(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path, {"origin": "git@github.com:owner/consumer.git"})
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))
    calls = []
    monkeypatch.setattr(
        common.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="{}", stderr=""),
    )

    common.run(["gh", "project", "item-list", "5"], cwd=checkout, auth=common.PROJECT_AUTH)

    assert calls[0] == ["gh", "project", "item-list", "5"]
    assert not record.exists()


def test_remote_credentials_never_reach_the_runner_arguments_or_errors(monkeypatch, tmp_path):
    secret = "ghp_remoteembeddedsecretvalue0000000000"
    checkout = _checkout(
        tmp_path, {"origin": f"https://x-access-token:{secret}@github.com/owner/consumer.git"}
    )
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record)))

    result = common.run(["gh", "issue", "view", "7"], cwd=checkout)

    recorded = _recorded(record)
    assert recorded[:2] == ["--repo", "owner/consumer"]
    assert all(secret not in part for part in recorded)
    assert secret not in result.stdout and secret not in result.stderr


def test_broken_remote_identity_error_excludes_credential_material(monkeypatch, tmp_path):
    secret = "ghp_brokenremotesecretvalue00000000000"
    checkout = _checkout(
        tmp_path, {"origin": f"https://x-access-token:{secret}@gitlab.example/owner/consumer.git"}
    )
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, tmp_path / "argv")))

    with pytest.raises(common.KernelError) as failure:
        common.run(["gh", "issue", "view", "7"], cwd=checkout)

    assert secret not in str(failure.value)


def test_checkout_repository_reads_the_governed_remote(tmp_path):
    checkout = _checkout(tmp_path, {"origin": "git@github.com:gillella/jaji-mission-control.git"})

    assert common.checkout_repository(checkout) == "gillella/jaji-mission-control"


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("git@github.com:gillella/jaji-mission-control.git", "gillella/jaji-mission-control"),
        ("git@github.com:gillella/Aru_Agentic_SDLC.git", "gillella/Aru_Agentic_SDLC"),
    ],
)
def test_pr_helpers_use_the_installation_of_the_checkout_repository(
    monkeypatch, tmp_path, remote, slug
):
    checkout = _checkout(tmp_path, {"origin": remote}, name="consumer")
    record = tmp_path / "argv"
    config = tmp_path / "observed-gh-config"
    payload = json.dumps([{"name": "reviewer-registered:coderabbit"}])
    monkeypatch.setenv(
        "ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record, payload, config))
    )
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "operator-gh"))
    monkeypatch.chdir(checkout)

    states = create_pr.registered_external_states()

    assert states["coderabbit"] == create_pr.AVAILABLE
    assert _recorded(record)[:4] == ["--repo", slug, "--", "gh"]
    assert config.read_text(encoding="utf-8") == str(tmp_path / "operator-gh")


def test_pr_helpers_fail_closed_when_the_checkout_has_no_repository_identity(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path, {}, name="consumer")
    record = tmp_path / "argv"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(_stub_runner(tmp_path, record, "[]")))
    monkeypatch.chdir(checkout)

    with pytest.raises(create_pr.KernelError, match="governed repository identity"):
        create_pr.registered_external_states()

    assert not record.exists()
