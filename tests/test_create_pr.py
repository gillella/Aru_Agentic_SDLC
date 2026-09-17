from __future__ import annotations

from pathlib import Path

import board_template
import create_pr

HEAD = "c" * 40


def install_creation(monkeypatch, *, number=6, created_head=HEAD, pr_id="PR_test123"):
    """Fake one In Progress claim, a published branch and the created-PR rereads."""
    lifecycle = ["In Progress"]
    commands, statuses = [], []
    monkeypatch.setattr(
        create_pr,
        "issue",
        lambda _n: {"number": number, "labels": [{"name": "agent:codex-1"}]},
    )
    monkeypatch.setattr(create_pr, "status_of", lambda _record: lifecycle[0])
    monkeypatch.setattr(
        create_pr, "current_branch", lambda: f"feat/issue-{number}-small-change"
    )
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: HEAD)
    monkeypatch.setattr(
        create_pr, "local_changed_paths", lambda: ["scripts/create_pr.py"]
    )
    monkeypatch.setattr(create_pr, "run", commands.append)
    snapshots = iter([
        {
            "number": 12,
            "url": "https://example/pr/12",
            "state": "OPEN",
            "headRefOid": created_head,
            "id": pr_id,
        },
        {"number": 12, "state": "CLOSED"},
    ])
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: next(snapshots))

    def transition(issue_number, status, **kwargs):
        kwargs["pre_mutation_check"]()
        statuses.append((issue_number, status, kwargs["expected_current"]))
        lifecycle[0] = status

    monkeypatch.setattr(create_pr, "set_status", transition)
    return commands, statuses


def test_create_pr_adds_pull_request_to_linked_project(monkeypatch, capsys):
    install_creation(monkeypatch, pr_id="PR_node_999")
    monkeypatch.setattr(create_pr, "repo_slug", lambda _cwd=None: "gillella/consumer")

    board_calls = []

    def fake_add_pr_to_board(slug, pr_node_id, directory):
        board_calls.append((slug, pr_node_id, directory))
        return "PVTI_item_123"

    monkeypatch.setattr(board_template, "add_pr_to_board", fake_add_pr_to_board)

    outcome = create_pr.create(6, "feat: small", "## Summary\n\nSmall change", "codex-1")
    assert outcome == {
        "pr": 12,
        "url": "https://example/pr/12",
        "head": HEAD,
        "next_action": "await-approval-by-another-account",
    }
    assert board_calls == [("gillella/consumer", "PR_node_999", Path.cwd())]


def test_create_pr_board_failure_does_not_refuse_pull_request(monkeypatch, capsys):
    install_creation(monkeypatch, pr_id="PR_node_fail")
    monkeypatch.setattr(create_pr, "repo_slug", lambda _cwd=None: "gillella/consumer")

    def fake_add_pr_to_board(slug, pr_node_id, directory):
        raise create_pr.KernelError("GraphQL error: permission denied")

    monkeypatch.setattr(board_template, "add_pr_to_board", fake_add_pr_to_board)

    outcome = create_pr.create(6, "feat: small", "## Summary\n\nSmall change", "codex-1")
    assert outcome["pr"] == 12
    captured = capsys.readouterr()
    assert "note: could not add PR to Project Board" in captured.err


def test_add_pr_to_board_success_and_issue_item_untouched(monkeypatch, tmp_path):
    mutations = []

    def fake_graphql(query, directory, **variables):
        if "projectsV2(" in query:
            return {
                "data": {
                    "repository": {
                        "projectsV2": {
                            "nodes": [{"id": "PVT_proj_1", "number": 1, "closed": False}]
                        }
                    }
                }
            }
        if "addProjectV2ItemById" in query:
            mutations.append(variables)
            return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_item_42"}}}}
        return {}

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    result = board_template.add_pr_to_board("gillella/consumer", "PR_node_100", tmp_path)
    assert result == "PVTI_item_42"
    assert mutations == [{"project": "PVT_proj_1", "content": "PR_node_100"}]


def test_add_pr_to_board_idempotent(monkeypatch, tmp_path):
    mutations = []

    def fake_graphql(query, directory, **variables):
        if "projectsV2(" in query:
            return {
                "data": {
                    "repository": {
                        "projectsV2": {
                            "nodes": [{"id": "PVT_proj_1", "number": 1, "closed": False}]
                        }
                    }
                }
            }
        if "addProjectV2ItemById" in query:
            mutations.append(variables)
            # GitHub returns the existing item when already present
            return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_existing_42"}}}}
        return {}

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    first = board_template.add_pr_to_board("gillella/consumer", "PR_node_100", tmp_path)
    second = board_template.add_pr_to_board("gillella/consumer", "PR_node_100", tmp_path)
    assert first == second == "PVTI_existing_42"
    assert len(mutations) == 2


def test_add_pr_to_board_no_linked_project_reported_and_not_refused(monkeypatch, tmp_path, capsys):
    def fake_graphql(query, directory, **variables):
        if "projectsV2(" in query:
            return {"data": {"repository": {"projectsV2": {"nodes": []}}}}
        return {}

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    result = board_template.add_pr_to_board("gillella/no-board", "PR_node_100", tmp_path)
    assert result is None
    captured = capsys.readouterr()
    assert "note: gillella/no-board has no linked Project" in captured.err


def test_add_pr_to_board_multiple_linked_projects_reported_and_not_refused(monkeypatch, tmp_path, capsys):
    def fake_graphql(query, directory, **variables):
        if "projectsV2(" in query:
            return {
                "data": {
                    "repository": {
                        "projectsV2": {
                            "nodes": [
                                {"id": "PVT_1", "number": 1, "closed": False},
                                {"id": "PVT_2", "number": 2, "closed": False},
                            ]
                        }
                    }
                }
            }
        return {}

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    result = board_template.add_pr_to_board("gillella/multi-board", "PR_node_100", tmp_path)
    assert result is None
    captured = capsys.readouterr()
    assert "note: could not read linked Project" in captured.err
    assert "multiple linked open Projects" in captured.err


def test_add_views_sets_filter_when_present(monkeypatch, tmp_path):
    created_views = []
    updated_views = []

    def fake_graphql(query, directory, **variables):
        if "createProjectV2View" in query:
            created_views.append(variables)
            return {"data": {"createProjectV2View": {"projectV2View": {"id": f"VIEW_{variables['name']}"}}}}
        if "updateProjectV2View" in query:
            updated_views.append(variables)
            return {"data": {"updateProjectV2View": {"projectV2View": {"id": variables["view"]}}}}
        return {}

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    views_to_add = [
        {"name": "Approval queue", "layout": "TABLE_LAYOUT", "filter": "is:pr is:open no:reviewers"},
        {"name": "NoFilter", "layout": "TABLE_LAYOUT", "filter": None},
    ]
    added = board_template.add_views("PVT_proj_1", views_to_add, tmp_path)
    assert added == ["Approval queue", "NoFilter"]
    assert len(created_views) == 2
    assert updated_views == [{"view": "VIEW_Approval queue", "filter": "is:pr is:open no:reviewers"}]


def test_board_template_state_reads_view_filters(monkeypatch, tmp_path):
    def fake_graphql(query, directory, **variables):
        return {
            "data": {
                "user": {
                    "projectV2": {
                        "id": "PVT_11",
                        "views": {
                            "nodes": [
                                {
                                    "id": "V1",
                                    "name": "Approval queue",
                                    "layout": "TABLE_LAYOUT",
                                    "filter": "is:pr is:open no:reviewers",
                                }
                            ]
                        },
                        "fields": {
                            "nodes": [
                                {
                                    "name": "Status",
                                    "options": [{"name": s} for s in board_template.STATUSES],
                                }
                            ]
                        },
                    }
                }
            }
        }

    monkeypatch.setattr(board_template, "_graphql", fake_graphql)
    st = board_template.state("gillella", 11, tmp_path)
    assert st["views"] == [
        {
            "name": "Approval queue",
            "layout": "TABLE_LAYOUT",
            "filter": "is:pr is:open no:reviewers",
        }
    ]


class FakePolicy:
    def __init__(self, reviewers):
        self.reviewers = reviewers
        self.posture = "human"


def test_reviewers_are_requested_so_github_notifies_the_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(create_pr, "run", calls.append)
    monkeypatch.setattr(
        create_pr.review_authority, "load_policy", lambda: FakePolicy(["gillella", "octocat"])
    )
    assert create_pr.request_reviewers(12) == ["gillella", "octocat"]
    assert calls == [
        ["gh", "pr", "edit", "12", "--add-reviewer", "gillella"],
        ["gh", "pr", "edit", "12", "--add-reviewer", "octocat"],
    ]


def test_a_reviewer_github_refuses_is_reported_and_skipped(monkeypatch, capsys):
    def refuse_one(argv):
        if argv[-1] == "author-account":
            raise create_pr.KernelError("gh failed: reviewer cannot be the author")

    monkeypatch.setattr(create_pr, "run", refuse_one)
    monkeypatch.setattr(
        create_pr.review_authority,
        "load_policy",
        lambda: FakePolicy(["author-account", "gillella"]),
    )
    # The refused account is skipped; the rest are still requested.
    assert create_pr.request_reviewers(12) == ["gillella"]
    assert "author-account" in capsys.readouterr().err


def test_no_declared_reviewers_requests_nobody_and_is_not_an_error(monkeypatch):
    calls = []
    monkeypatch.setattr(create_pr, "run", calls.append)
    monkeypatch.setattr(create_pr.review_authority, "load_policy", lambda: FakePolicy([]))
    assert create_pr.request_reviewers(12) == []
    assert calls == []


def test_create_pr_reviewer_failure_does_not_refuse_pull_request(monkeypatch, capsys):
    install_creation(monkeypatch, pr_id="PR_node_rev")
    monkeypatch.setattr(create_pr, "repo_slug", lambda _cwd=None: "gillella/consumer")
    monkeypatch.setattr(board_template, "add_pr_to_board", lambda *a, **k: "PVTI_x")

    def explode(_number):
        raise RuntimeError("policy read failed")

    monkeypatch.setattr(create_pr, "request_reviewers", explode)
    outcome = create_pr.create(6, "feat: small", "## Summary\n\nSmall change", "codex-1")
    # The pull request is returned; only a note is written.
    assert outcome["pr"] == 12
    assert "could not request reviewers" in capsys.readouterr().err
