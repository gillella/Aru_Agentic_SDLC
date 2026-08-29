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


@pytest.mark.parametrize(
    "body",
    [
        "## Acceptance Criteria\n\n- [ ] behavior is observable\n\ntouches: scripts/a.py, docs/**\n",
        (
            "### Outcome\n\nThe behavior is observable.\n\n### Acceptance Criteria\n\n"
            "- [ ] behavior is observable\n\n### touches:\n\nscripts/a.py, docs/**\n\n"
            "### Dependencies\n\nNo response\n"
        ),
    ],
)
def test_issue_contract_accepts_valid_bodies(body):
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


@pytest.mark.parametrize("use_runner", [True, False])
def test_repository_command_runner_resolution(monkeypatch, tmp_path, use_runner):
    calls = []
    if use_runner:
        runner = tmp_path / "app-run"
        runner.write_text("#!/bin/sh\n", encoding="utf-8")
        runner.chmod(0o755)
        monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", str(runner))
        expected_argv = [str(runner), "--", "gh", "issue", "view", "7"]
    else:
        monkeypatch.delenv("ARU_GITHUB_APP_RUNNER", raising=False)
        expected_argv = ["gh", "issue", "view", "7"]

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    common.run(["gh", "issue", "view", "7"])
    assert calls[0][0] == expected_argv


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


def test_linked_project_rejects_graphql_errors_without_caching(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    common._LINKED_PROJECT_CACHE.clear()
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        if len(calls) == 1:
            return {
                "errors": [{"message": "partial project board failure"}],
                "data": {
                    "repository": {
                        "projectsV2": {
                            "nodes": [{"id": "PVT_BAD", "number": 5, "title": "Bad"}],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                },
            }
        return _linked_project_payload(5)

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    with pytest.raises(common.KernelError, match="GraphQL error"):
        common.linked_project()

    assert ("owner/repo", "") not in common._LINKED_PROJECT_CACHE

    project = common.linked_project()
    assert project["id"] == "PVT_1"
    assert project["number"] == 5
    assert len(calls) == 2
    assert common._LINKED_PROJECT_CACHE[("owner/repo", "")] == project


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


def project_status_payload(
    *,
    items: list[dict] | None = None,
    has_next_page: bool = False,
) -> dict:
    return {
        "data": {
            "issueNode": {
                "projectItems": {
                    "nodes": items
                    if items is not None
                    else [
                        {
                            "id": "PVTI_7",
                            "project": {"id": "PVT_1"},
                            "fieldValueByName": {"name": "Done"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": has_next_page},
                }
            }
        }
    }


def test_project_item_status_reads_back_the_settled_option(monkeypatch):
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
        return project_status_payload()

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    assert common.project_item_status(7) == "Done"
    assert calls[0][1] is None
    assert calls[1][1] == common.PROJECT_AUTH
    query = " ".join(calls[1][0])
    assert "projectItems(first:20)" in query
    assert 'fieldValueByName(name:"Status")' in query


def test_project_item_status_returns_none_without_a_status_value(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        common,
        "linked_project",
        lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"},
    )

    def fake_gh_json(args, *, cwd=None, auth=None):
        if args[:2] == ["api", "repos/owner/repo/issues/7"]:
            return {"number": 7, "node_id": "I_7"}
        return project_status_payload(
            items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": None}]
        )

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    assert common.project_item_status(7) is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (project_status_payload(has_next_page=True), "truncated"),
        (project_status_payload(items=[]), "ambiguous"),
        (
            project_status_payload(
                items=[
                    {"id": "PVTI_7a", "project": {"id": "PVT_1"}, "fieldValueByName": None},
                    {"id": "PVTI_7b", "project": {"id": "PVT_1"}, "fieldValueByName": None},
                ]
            ),
            "ambiguous",
        ),
        (
            project_status_payload(
                items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {}}]
            ),
            "malformed",
        ),
    ],
)
def test_project_item_status_fails_closed_on_incomplete_evidence(
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
        common.project_item_status(7)


def test_project_item_status_fails_closed_on_top_level_graphql_errors(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(
        common,
        "linked_project",
        lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"},
    )

    def fake_gh_json(args, *, cwd=None, auth=None):
        if args[:2] == ["api", "repos/owner/repo/issues/7"]:
            return {"number": 7, "node_id": "I_7"}
        # A partial response: an error is present alongside a data envelope,
        # which must not be trusted as complete evidence.
        return {"errors": [{"message": "boom"}], "data": {"issueNode": None}}

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    with pytest.raises(common.KernelError, match="GraphQL error"):
        common.project_item_status(7)


@pytest.mark.parametrize(
    ("env_var", "secret", "returncode", "check", "expect_raise"),
    [
        ("GH_TOKEN", "ghs_this-must-never-appear", 1, True, True),
        ("GITHUB_TOKEN", "github_pat_this-must-never-appear", 1, False, False),
        ("GH_TOKEN", "ghs_success-output-must-never-appear", 0, True, False),
    ],
)
def test_subprocess_redacts_token_values(
    monkeypatch, env_var, secret, returncode, check, expect_raise
):
    monkeypatch.setenv(env_var, secret)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            returncode,
            stdout=f'{{"token":"{secret}"}}',
            stderr=f"failed or warning with {secret}",
        )

    monkeypatch.setattr(common.subprocess, "run", fake_run)

    if expect_raise:
        with pytest.raises(common.KernelError) as exc_info:
            common.run(["git", "status"], check=check)
        assert secret not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value)
    else:
        result = common.run(["gh", "api", "repos/owner/repo"], check=check)
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert "[REDACTED]" in result.stdout or "[REDACTED]" in result.stderr


def test_child_issue_snapshots_use_one_repository_graphql_query(monkeypatch):
    import update_issue_status as uis

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "i91": {
                        "number": 91,
                        "state": "CLOSED",
                        "repository": {"nameWithOwner": "owner/repo"},
                        "labels": {
                            "nodes": [{"name": "status:done"}],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                    "i92": {
                        "number": 92,
                        "state": "CLOSED",
                        "repository": {"nameWithOwner": "owner/repo"},
                        "labels": {
                            "nodes": [{"name": "status:done"}],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                }
            }
        }

    monkeypatch.setattr(uis, "gh_json", fake_gh_json)
    snapshots = uis.child_issue_snapshots([91, 92])
    assert set(snapshots) == {91, 92}
    assert len(calls) == 1
    assert calls[0][1] == uis.REPOSITORY_AUTH
    query = " ".join(calls[0][0])
    assert "i91: issue(number: 91)" in query
    assert "i92: issue(number: 92)" in query


def _raw_child_node(**overrides) -> dict:
    node = {
        "number": 91,
        "state": "CLOSED",
        "repository": {"nameWithOwner": "owner/repo"},
        "labels": {"nodes": [{"name": "status:done"}], "pageInfo": {"hasNextPage": False}},
    }
    node.update(overrides)
    return node


def _child_payload(node: dict) -> dict:
    return {"data": {"repository": {"i91": node}}}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"errors": [{"message": "boom"}]}, "GraphQL error"),
        ({"data": {}}, "unavailable"),
        ({"data": {"repository": None}}, "unavailable"),
        ({"data": {"repository": "not-a-dict"}}, "unavailable"),
        (_child_payload(_raw_child_node(number=92)), "does not match the request"),
        (_child_payload(_raw_child_node(state="MERGED")), "unsupported state"),
        (
            _child_payload(
                _raw_child_node(
                    labels={"nodes": [{"missing": "name"}], "pageInfo": {"hasNextPage": False}}
                )
            ),
            "malformed",
        ),
        (
            _child_payload(_raw_child_node(labels={"nodes": ["bad"], "pageInfo": {"hasNextPage": False}})),
            "malformed",
        ),
        (
            _child_payload(_raw_child_node(labels={"nodes": [{"name": "status:done"}], "pageInfo": {"hasNextPage": True}})),
            "truncated",
        ),
        (_child_payload(_raw_child_node(labels={"nodes": [{"name": "status:done"}]})), "truncated"),
    ],
)
def test_child_issue_snapshots_fails_closed_on_malformed_graphql(monkeypatch, payload, message):
    import update_issue_status as uis

    monkeypatch.setattr(uis, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(uis, "gh_json", lambda args, *, cwd=None, auth=None: payload)

    with pytest.raises(uis.KernelError, match=message):
        uis.child_issue_snapshots([91])
