from __future__ import annotations

import subprocess

import pytest

import common


def record(body: str, labels: list[str] | None = None, state: str = "OPEN") -> dict:
    return {
        "number": 7,
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in labels or []],
    }


def test_issue_contract_accepts_one_safe_budget_and_unchecked_criterion():
    body = """
## Acceptance Criteria

- [ ] behavior is observable

touches: scripts/a.py, docs/**
"""
    assert common.contract_errors(record(body)) == []
    assert common.parse_touches(body) == ["scripts/a.py", "docs/**"]
    assert common.acceptance_items(body) == [(False, "behavior is observable")]


@pytest.mark.parametrize(
    "declaration",
    [
        "touches: ../secret",
        "touches: /tmp/file",
        "touches: -rf",
        "touches: ~user/file",
        "touches:",
    ],
)
def test_issue_contract_rejects_unsafe_budgets(declaration):
    body = f"## Acceptance Criteria\n\n- [ ] done\n\n{declaration}\n"
    assert common.contract_errors(record(body))


def test_path_budget_is_exact_or_recursive():
    declared = ["scripts/a.py", "docs/**"]
    assert common.path_allowed("scripts/a.py", declared)
    assert common.path_allowed("docs/guide/one.md", declared)
    assert not common.path_allowed("scripts/b.py", declared)
    assert not common.path_allowed("../docs/guide.md", declared)


def test_status_fails_closed_on_contradiction():
    issue = record("", ["status:ready", "status:in-progress"])
    with pytest.raises(common.KernelError, match="contradictory"):
        common.status_of(issue)


def test_dependencies_are_unique_and_ordered():
    body = "depends-on: #9\ndepends-on: #2\ndepends-on: #9\n"
    assert common.dependencies(body) == [2, 9]


def test_repository_command_uses_configured_app_runner(monkeypatch, tmp_path):
    runner = tmp_path / "app-run"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    runner.chmod(0o755)
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(runner))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    common.run(["gh", "issue", "view", "7"])

    assert calls[0][0] == [str(runner), "--", "gh", "issue", "view", "7"]


def test_repository_command_uses_portable_gh_when_runner_is_unset(monkeypatch):
    monkeypatch.delenv("ARU_GITHUB_APP_RUNNER", raising=False)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    common.run(["gh", "pr", "view", "7"])

    assert calls[0][0] == ["gh", "pr", "view", "7"]


def test_configured_app_runner_must_be_executable(monkeypatch, tmp_path):
    runner = tmp_path / "missing-app-run"
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(runner))

    with pytest.raises(common.KernelError, match="not executable"):
        common.run(["gh", "issue", "view", "7"])


def test_project_command_uses_stored_pat_without_token_overrides(monkeypatch):
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", "/not/used/for/projects")
    monkeypatch.setenv("GH_TOKEN", "app-token-must-not-reach-projects")
    monkeypatch.setenv("GITHUB_TOKEN", "also-not-for-projects")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    common.run(["gh", "project", "item-list", "5"])

    argv, kwargs = calls[0]
    assert argv == ["gh", "project", "item-list", "5"]
    assert "GH_TOKEN" not in kwargs["env"]
    assert "GITHUB_TOKEN" not in kwargs["env"]


def test_graphql_requires_an_explicit_authority():
    with pytest.raises(common.KernelError, match="GraphQL authority is ambiguous"):
        common.gh_json(["api", "graphql", "-f", "query=query { viewer { login } }"])


def test_mixed_graphql_fails_closed_even_with_explicit_authority():
    query = "query { repository(owner: \"o\", name: \"r\") { pullRequest(number: 1) { id } projectsV2(first: 1) { nodes { id } } } }"
    with pytest.raises(common.KernelError, match="mixes repository and Project V2"):
        common.gh_json(
            ["api", "graphql", "-f", f"query={query}"],
            auth=common.PROJECT_AUTH,
        )


def test_linked_project_explicitly_uses_project_authority(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "projectsV2": {
                        "nodes": [{"id": "PVT_1", "number": 5, "title": "Delivery"}],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        }

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    assert common.linked_project()["number"] == 5
    assert calls[0][1] == common.PROJECT_AUTH


def test_subprocess_error_redacts_token_values(monkeypatch):
    secret = "ghs_this-must-never-appear"
    monkeypatch.setenv("GH_TOKEN", secret)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"failed with {secret}")

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    with pytest.raises(common.KernelError) as exc_info:
        common.run(["git", "status"])
    assert secret not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_unchecked_subprocess_error_also_redacts_token_values(monkeypatch):
    secret = "github_pat_this-must-never-appear"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"failed with {secret}")

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    result = common.run(["git", "status"], check=False)
    assert secret not in result.stderr
    assert "[REDACTED]" in result.stderr


def test_successful_subprocess_also_redacts_token_values(monkeypatch):
    secret = "ghs_success-output-must-never-appear"
    monkeypatch.setenv("GH_TOKEN", secret)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f'{{"token":"{secret}"}}',
            stderr=f"warning includes {secret}",
        )

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    result = common.run(["gh", "api", "repos/owner/repo"])
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "[REDACTED]" in result.stdout
    assert "[REDACTED]" in result.stderr
