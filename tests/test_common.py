from __future__ import annotations

import os
import subprocess

import pytest

import common


@pytest.fixture(autouse=True)
def clear_linked_project_cache():
    common._LINKED_PROJECT_CACHE.clear()
    yield
    common._LINKED_PROJECT_CACHE.clear()


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


def test_issue_contract_accepts_rendered_issue_form_markdown():
    body = """
### Outcome

The behavior is observable.

### Acceptance Criteria

- [ ] behavior is observable

### touches:

scripts/a.py, docs/**

### Dependencies

No response
"""
    assert common.contract_errors(record(body)) == []
    assert common.parse_touches(body) == ["scripts/a.py", "docs/**"]
    assert common.acceptance_items(body) == [(False, "behavior is observable")]


def test_issue_contract_rejects_mixed_inline_and_issue_form_touches():
    body = """
## Acceptance Criteria

- [ ] behavior is observable

touches: scripts/a.py

### touches:

docs/**
"""
    with pytest.raises(common.KernelError, match="exactly one touches"):
        common.parse_touches(body)


@pytest.mark.parametrize(
    ("value", "diagnostic"),
    [
        ("scripts/a.py\ndocs/**", "single line"),
        ("../private.py", "unsafe path"),
    ],
)
def test_issue_form_touches_rejects_multiline_or_unsafe_values(value, diagnostic):
    body = f"### touches:\n\n{value}\n"
    with pytest.raises(common.KernelError, match=diagnostic):
        common.parse_touches(body)


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


def _linked_project_payload(number: int = 5) -> dict:
    return {
        "data": {
            "repository": {
                "projectsV2": {
                    "nodes": [{"id": "PVT_1", "number": number, "title": "Delivery"}],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }
    }


def test_linked_project_does_not_requery_within_one_process(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return _linked_project_payload()

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    first = common.linked_project()
    second = common.linked_project()

    assert first == second == {"id": "PVT_1", "number": 5, "title": "Delivery"}
    assert len(calls) == 1
    assert calls[0][1] == common.PROJECT_AUTH


def test_linked_project_cache_is_scoped_to_project_number(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append(args)
        requested = os.environ.get("ARU_PROJECT_NUMBER")
        return _linked_project_payload(int(requested) if requested else 5)

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    monkeypatch.delenv("ARU_PROJECT_NUMBER", raising=False)
    assert common.linked_project()["number"] == 5
    monkeypatch.setenv("ARU_PROJECT_NUMBER", "9")
    assert common.linked_project()["number"] == 9
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: HTTP 429",
        "API rate limit exceeded for user",
        "You have exceeded a secondary rate limit",
        "resource-limits exceeded",
    ],
)
def test_github_quota_failure_raises_without_retry(monkeypatch, stderr):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    with pytest.raises(common.KernelError, match="quota exhausted"):
        common.run(
            ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"],
            auth=common.REPOSITORY_AUTH,
        )
    assert len(calls) == 1
    assert "stop and wait" in common.QUOTA_STOP_MESSAGE


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL resource 429 was not found",
        "issue #429 does not exist",
    ],
)
def test_unrelated_429_error_is_not_classified_as_quota(monkeypatch, stderr):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    with pytest.raises(common.KernelError, match=stderr):
        common.run(
            ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"],
            auth=common.REPOSITORY_AUTH,
        )


def test_graphql_rate_limited_payload_raises_without_retry(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}',
            stderr="",
        )

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    with pytest.raises(common.KernelError, match="quota exhausted"):
        common.gh_json(
            ["api", "graphql", "-f", "query=query { viewer { login } }"],
            auth=common.REPOSITORY_AUTH,
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("app/aru-code-factory-gillella", "aru-code-factory-gillella[bot]", True),
        ("app/aru-code-factory-gillella", "app/aru-code-factory-gillella", True),
        ("aru-code-factory-gillella[bot]", "aru-code-factory-gillella[bot]", True),
        ("octocat", "octocat", True),
        ("octocat", "OctoCat", True),
        ("octocat", "octocat[bot]", False),
        ("app/aru-code-factory-gillella", "app/other-app", False),
    ],
)
def test_canonical_github_actors_preserve_distinct_users(left, right, equal):
    assert (common.same_github_actor(left, right)) is equal


def board_payload(
    *,
    items: list[dict] | None = None,
    has_next_page: bool = False,
    field: dict | None = None,
) -> dict:
    return {
        "data": {
            "issueNode": {
                "projectItems": {
                    "nodes": items
                    if items is not None
                    else [{"id": "PVTI_7", "project": {"id": "PVT_1"}}],
                    "pageInfo": {"hasNextPage": has_next_page},
                }
            },
            "projectNode": {
                "field": field
                if field is not None
                else {
                    "id": "PVTSSF_status",
                    "name": "Status",
                    "options": [
                        {"id": "ready-option", "name": "Ready"},
                        {"id": "done-option", "name": "Done"},
                    ],
                }
            },
        }
    }


def test_board_edit_reads_only_target_issue_item_and_status_field(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        common,
        "linked_project",
        lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"},
    )
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        if args[:2] == ["api", "repos/owner/repo/issues/7"]:
            return {"number": 7, "node_id": "I_7"}
        return board_payload()

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    assert common.board_edit(7, "Done") == [
        "project",
        "item-edit",
        "--id",
        "PVTI_7",
        "--project-id",
        "PVT_1",
        "--field-id",
        "PVTSSF_status",
        "--single-select-option-id",
        "done-option",
    ]
    assert calls[0][1] is None
    assert calls[1][1] == common.PROJECT_AUTH
    query = " ".join(calls[1][0])
    assert "projectItems(first:20)" in query
    assert 'field(name:"Status")' in query
    assert "item-list" not in query
    assert "field-list" not in query


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (board_payload(has_next_page=True), "truncated"),
        (board_payload(items=[]), "ambiguous"),
        (
            board_payload(
                items=[
                    {"id": "PVTI_7a", "project": {"id": "PVT_1"}},
                    {"id": "PVTI_7b", "project": {"id": "PVT_1"}},
                ]
            ),
            "ambiguous",
        ),
        (board_payload(items=[{"id": "PVTI_7", "project": {}}]), "malformed"),
        (
            board_payload(
                field={
                    "id": "PVTSSF_status",
                    "name": "Status",
                    "options": [{"id": "ready-option", "name": "Ready"}],
                }
            ),
            "no unique 'Done' option",
        ),
        (
            board_payload(
                field={
                    "id": "PVTSSF_status",
                    "name": "Status",
                    "options": [{"id": "", "name": "Done"}],
                }
            ),
            "options are malformed",
        ),
    ],
)
def test_board_edit_fails_closed_on_incomplete_targeted_evidence(
    monkeypatch, payload, message
):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        common,
        "linked_project",
        lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"},
    )

    def fake_gh_json(args, *, cwd=None, auth=None):
        if args[:2] == ["api", "repos/owner/repo/issues/7"]:
            return {"number": 7, "node_id": "I_7"}
        return payload

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    with pytest.raises(common.KernelError, match=message):
        common.board_edit(7, "Done")


@pytest.mark.parametrize(
    "issue_record",
    [
        {},
        {"number": 8, "node_id": "I_7"},
        {"number": 7, "node_id": ""},
        {"number": 7, "node_id": "I_7", "pull_request": {}},
    ],
)
def test_board_edit_rejects_missing_or_non_issue_project_identity(
    monkeypatch, issue_record
):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        common,
        "linked_project",
        lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"},
    )
    monkeypatch.setattr(common, "gh_json", lambda *args, **kwargs: issue_record)

    with pytest.raises(common.KernelError, match="Project identity is unavailable"):
        common.board_edit(7, "Done")


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
