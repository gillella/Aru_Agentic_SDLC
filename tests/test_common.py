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
    body = "\n## Acceptance Criteria\n\n- [ ] behavior is observable\n\ntouches: scripts/a.py\n\n### touches:\n\ndocs/**\n"
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
    assert common.path_allowed("scripts/a.py", declared) and common.path_allowed("docs/guide/one.md", declared)
    assert not common.path_allowed("scripts/b.py", declared) and not common.path_allowed("../docs/guide.md", declared)


def test_status_fails_closed_on_contradiction():
    issue = record("", ["status:ready", "status:in-progress"])
    with pytest.raises(common.KernelError, match="contradictory"):
        common.status_of(issue)


def test_dependencies_are_unique_and_ordered():
    body = "depends-on: #9\ndepends-on: #2\ndepends-on: #9\n"
    assert common.dependencies(body) == [2, 9]


@pytest.mark.parametrize(
    "declaration",
    [
        "depends-on: #9, #10",
        "depends-on: none",
        "depends-on:",
        "depends-on: 9",
        "depends-on: #09",
        "Depends-On: #9",
        " depends-on: #9",
        "depends-on: #9 trailing",
    ],
)
def test_dependencies_reject_noncanonical_declarations(declaration):
    with pytest.raises(
        common.KernelError,
        match=r"depends-on declarations must each match 'depends-on: #N'",
    ):
        common.dependencies(declaration)


def test_issue_contract_reports_malformed_dependency_declaration():
    body = (
        "## Acceptance Criteria\n\n- [ ] behavior is observable\n\n"
        "depends-on: #8, #9\n"
        "touches: scripts/a.py\n"
    )

    assert common.contract_errors(record(body)) == [
        "depends-on declarations must each match 'depends-on: #N'"
    ]


def test_project_command_uses_stored_pat_without_token_overrides(monkeypatch):
    monkeypatch.setenv("ARU_GITHUB_APP_RUNNER", "/not/used/for/projects")
    monkeypatch.setenv("GH_TOKEN", "app-token-must-not-reach-projects")
    monkeypatch.setenv("GITHUB_TOKEN", "also-not-for-projects")
    calls = []
    monkeypatch.setattr(common.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0, stdout="{}", stderr=""))
    common.run(["gh", "project", "item-list", "5"])
    argv, kwargs = calls[0]
    assert argv == ["gh", "project", "item-list", "5"]
    assert "GH_TOKEN" not in kwargs["env"] and "GITHUB_TOKEN" not in kwargs["env"]


def test_graphql_requires_an_explicit_authority():
    with pytest.raises(common.KernelError, match="GraphQL authority is ambiguous"):
        common.gh_json(["api", "graphql", "-f", "query=query { viewer { login } }"])


def test_mixed_graphql_fails_closed_even_with_explicit_authority():
    query = "query { repository(owner: \"o\", name: \"r\") { pullRequest(number: 1) { id } projectsV2(first: 1) { nodes { id } } } }"
    with pytest.raises(common.KernelError, match="mixes repository and Project V2"):
        common.gh_json(["api", "graphql", "-f", f"query={query}"], auth=common.PROJECT_AUTH)


def test_linked_project_explicitly_uses_project_authority(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {
            "data": {
                "repository": {
                    "projectsV2": {
                        "nodes": [{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}],
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
                    "nodes": [{"id": "PVT_1", "number": number, "title": "Delivery", "closed": False}],
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

    assert first == second == {"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}
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
                            "nodes": [{"id": "PVT_BAD", "number": 5, "title": "Bad", "closed": False}],
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


@pytest.mark.parametrize(
    ("nodes", "page_info", "message"),
    [
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, {"id": "PVT_2", "number": -1, "title": "Inv", "closed": False}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, "not-a-dict"], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, {"id": "", "number": 6, "title": "Inv", "closed": False}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, {"id": "PVT_2", "number": True, "title": "Inv", "closed": False}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, {"id": "PVT_2", "number": 6, "title": "", "closed": False}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery"}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}, {"id": "PVT_2", "number": 6, "title": "Other"}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": "false"}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": 0}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": None}], {"hasNextPage": False}, "malformed"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}], {"hasNextPage": True}, "truncated"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}], {"hasNextPage": None}, "truncated"),
        ([{"id": "PVT_1", "number": 5, "title": "Delivery", "closed": False}], "not-a-dict", "unavailable"),
    ],
)
def test_linked_project_fails_closed_on_malformed_inventory_without_caching(
    monkeypatch, nodes, page_info, message
):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    common._LINKED_PROJECT_CACHE.clear()
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append(args)
        if len(calls) == 1:
            return {"data": {"repository": {"projectsV2": {"nodes": nodes, "pageInfo": page_info}}}}
        return _linked_project_payload(5)

    monkeypatch.setattr(common, "gh_json", fake_gh_json)

    with pytest.raises(common.KernelError, match=message):
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


@pytest.mark.parametrize("stderr", ["gh: HTTP 429", "API rate limit exceeded for user", "You have exceeded a secondary rate limit", "resource-limits exceeded"])
def test_github_quota_failure_raises_without_retry(monkeypatch, stderr):
    calls = []
    monkeypatch.setattr(common.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr))
    with pytest.raises(common.KernelError, match="quota exhausted"):
        common.run(["gh", "api", "graphql", "-f", "query=query { viewer { login } }"], auth=common.REPOSITORY_AUTH)
    assert len(calls) == 1 and "stop and wait" in common.QUOTA_STOP_MESSAGE


@pytest.mark.parametrize("stderr", ["GraphQL resource 429 was not found", "issue #429 does not exist"])
def test_unrelated_429_error_is_not_classified_as_quota(monkeypatch, stderr):
    monkeypatch.setattr(common.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr))
    with pytest.raises(common.KernelError, match=stderr):
        common.run(["gh", "api", "graphql", "-f", "query=query { viewer { login } }"], auth=common.REPOSITORY_AUTH)


def test_graphql_rate_limited_payload_raises_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(common.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout='{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}', stderr=""))
    with pytest.raises(common.KernelError, match="quota exhausted"):
        common.gh_json(["api", "graphql", "-f", "query=query { viewer { login } }"], auth=common.REPOSITORY_AUTH)
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
    if items is None:
        items = [{"id": "PVTI_7", "project": {"id": "PVT_1"}}]
    if field is None:
        field = {
            "id": "PVTSSF_status",
            "name": "Status",
            "options": [
                {"id": "backlog-option", "name": "Backlog"},
                {"id": "ready-option", "name": "Ready"},
                {"id": "done-option", "name": "Done"},
            ],
        }
    return {
        "data": {
            "issueNode": {"projectItems": {"nodes": items, "pageInfo": {"hasNextPage": has_next_page}}},
            "projectNode": {"field": field},
        }
    }


def test_board_edit_reads_only_target_issue_item_and_status_field(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {"number": 7, "node_id": "I_7"} if args[:2] == ["api", "repos/owner/repo/issues/7"] else board_payload()

    monkeypatch.setattr(common, "gh_json", fake_gh_json)
    assert common.board_edit(7, "Done") == [
        "project", "item-edit", "--id", "PVTI_7", "--project-id", "PVT_1",
        "--field-id", "PVTSSF_status", "--single-select-option-id", "done-option",
    ]
    assert calls[0][1] is None and calls[1][1] == common.PROJECT_AUTH
    query = " ".join(calls[1][0])
    assert "projectItems(first:20)" in query and 'field(name:"Status")' in query
    assert "item-list" not in query and "field-list" not in query


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (board_payload(has_next_page=True), "truncated"),
        (board_payload(items=[]), "not a member"),
        (board_payload(items=[{"id": "PVTI_7a", "project": {"id": "PVT_1"}}, {"id": "PVTI_7b", "project": {"id": "PVT_1"}}]), "ambiguous"),
        (board_payload(items=[{"id": "PVTI_7", "project": {}}]), "malformed"),
        (board_payload(field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "ready-option", "name": "Ready"}]}), "no unique 'Done' option"),
        (board_payload(field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "", "name": "Done"}]}), "options are malformed"),
        (board_payload(field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt2", "name": "Done"}]}), "options are malformed"),
        (board_payload(field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt1", "name": "Ready"}]}), "options are malformed"),
    ],
)
def test_board_edit_fails_closed_on_incomplete_targeted_evidence(monkeypatch, payload, message):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(common, "gh_json", lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"} if args[:2] == ["api", "repos/owner/repo/issues/7"] else payload)
    with pytest.raises(common.KernelError, match=message):
        common.board_edit(7, "Done")


@pytest.mark.parametrize("issue_record", [{}, {"number": 8, "node_id": "I_7"}, {"number": 7, "node_id": ""}, {"number": 7, "node_id": "I_7", "pull_request": {}}])
def test_board_edit_rejects_missing_or_non_issue_project_identity(monkeypatch, issue_record):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(common, "gh_json", lambda *args, **kwargs: issue_record)
    with pytest.raises(common.KernelError, match="Project identity is unavailable"):
        common.board_edit(7, "Done")


@pytest.mark.parametrize(
    ("items", "expected_current", "match"),
    [
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Backlog"}}], "Backlog", None),
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Ready"}}], "Backlog", r"Project card status \('Ready'\) does not equal expected 'Backlog'"),
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": None}], "Backlog", r"Project card status \(None\) does not equal expected 'Backlog'"),
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}}], "Backlog", r"Project card status \(None\) does not equal expected 'Backlog'"),
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {}}], "Backlog", "Project Board Status field value is malformed"),
        ([{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": 123}}], "Backlog", "Project Board Status field value is malformed"),
    ],
)
def test_board_edit_expected_current_contract(monkeypatch, items, expected_current, match):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"}
        if args[:2] == ["api", "repos/owner/repo/issues/7"]
        else board_payload(items=items),
    )
    if match is not None:
        with pytest.raises(common.StatusPreconditionError, match=match):
            common.board_edit(7, "Done", expected_current=expected_current)
    else:
        assert common.board_edit(7, "Done", expected_current=expected_current) == [
            "project", "item-edit", "--id", "PVTI_7", "--project-id", "PVT_1",
            "--field-id", "PVTSSF_status", "--single-select-option-id", "done-option",
        ]


def project_status_payload(*, items: list[dict] | None = None, has_next_page: bool = False) -> dict:
    if items is None:
        items = [{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}]
    return board_payload(items=items, has_next_page=has_next_page)


def test_project_item_status_reads_back_the_settled_option(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    calls = []

    def fake_gh_json(args, *, cwd=None, auth=None):
        calls.append((args, auth))
        return {"number": 7, "node_id": "I_7"} if args[:2] == ["api", "repos/owner/repo/issues/7"] else project_status_payload()

    monkeypatch.setattr(common, "gh_json", fake_gh_json)
    assert common.project_item_status(7) == "Done"
    assert calls[0][1] is None and calls[1][1] == common.PROJECT_AUTH
    query = " ".join(calls[1][0])
    assert "projectItems(first:20)" in query and 'fieldValueByName(name:"Status")' in query


def test_project_item_status_returns_none_without_a_status_value(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"}
        if args[:2] == ["api", "repos/owner/repo/issues/7"]
        else project_status_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": None}]),
    )
    assert common.project_item_status(7) is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (project_status_payload(has_next_page=True), "truncated"),
        (project_status_payload(items=[]), "not a member"),
        (project_status_payload(items=[{"id": "PVTI_7a", "project": {"id": "PVT_1"}, "fieldValueByName": None}, {"id": "PVTI_7b", "project": {"id": "PVT_1"}, "fieldValueByName": None}]), "ambiguous"),
        (project_status_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {}}]), "malformed"),
        (board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}], field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "", "name": "Done"}]}), "options are malformed"),
        (board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}], field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt2", "name": "Done"}]}), "options are malformed"),
        (board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}], field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt1", "name": "Ready"}]}), "options are malformed"),
        (board_payload(items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "UnknownOption"}}]), "malformed"),
        ({"data": {"issueNode": {"projectItems": {"nodes": [{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}], "pageInfo": {"hasNextPage": False}}}, "projectNode": {"field": None}}}, "ambiguous"),
    ],
)
@pytest.mark.parametrize("same_status", [False, True])
def test_project_item_status_fails_closed_on_incomplete_evidence(monkeypatch, payload, message, same_status):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"}
        if args[:2] == ["api", "repos/owner/repo/issues/7"]
        else payload,
    )
    monkeypatch.setattr(common, "issue", lambda *a, **kw: record("", ["status:done"]))
    monkeypatch.setattr(common, "run", lambda *a, **kw: pytest.fail("unexpected mutation"))
    with pytest.raises(common.KernelError, match=message):
        if same_status:
            common.set_status(7, "Done")
        else:
            common.project_item_status(7)


def test_project_status_rejects_duplicate_options_on_item_status_and_board_edit(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    dup_name_payload = board_payload(
        items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}],
        field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt2", "name": "Done"}]},
    )
    dup_id_payload = board_payload(
        items=[{"id": "PVTI_7", "project": {"id": "PVT_1"}, "fieldValueByName": {"name": "Done"}}],
        field={"id": "PVTSSF_status", "name": "Status", "options": [{"id": "opt1", "name": "Done"}, {"id": "opt1", "name": "Ready"}]},
    )
    for p in (dup_name_payload, dup_id_payload):
        monkeypatch.setattr(common, "gh_json", lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"} if args[:2] == ["api", "repos/owner/repo/issues/7"] else p)
        with pytest.raises(common.KernelError, match="options are malformed"):
            common.project_item_status(7)
        with pytest.raises(common.KernelError, match="options are malformed"):
            common.board_edit(7, "Done")


def test_project_item_status_fails_closed_on_top_level_graphql_errors(monkeypatch):
    monkeypatch.setattr(common, "repo_slug", lambda cwd=None: "owner/repo")
    monkeypatch.setattr(common, "linked_project", lambda cwd=None: {"id": "PVT_1", "number": 5, "title": "Delivery"})
    monkeypatch.setattr(
        common,
        "gh_json",
        lambda args, *, cwd=None, auth=None: {"number": 7, "node_id": "I_7"}
        if args[:2] == ["api", "repos/owner/repo/issues/7"]
        else {"errors": [{"message": "boom"}], "data": {"issueNode": None}},
    )
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
        assert "[REDACTED]" in result.stdout
        assert "[REDACTED]" in result.stderr
