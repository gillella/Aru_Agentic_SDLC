from __future__ import annotations

import os
import subprocess

import pytest

import common
import create_pr
import revert_merge


def test_create_opens_a_pr_marked_needs_reviewer_when_none_is_available(monkeypatch):
    lifecycle = ["In Progress"]
    monkeypatch.setattr(create_pr, "issue", lambda _n: {"number": 6, "labels": [{"name": "agent:codex-1"}]})
    monkeypatch.setattr(create_pr, "status_of", lambda _record: lifecycle[0])
    monkeypatch.setattr(create_pr, "current_branch", lambda: "feat/issue-6-small-change")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["scripts/create_pr.py"])
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)

    def no_reviewer(*_args, **_kwargs):
        raise create_pr.NoReviewerAvailable("no external or distinct coding-agent reviewer is available")

    monkeypatch.setattr(create_pr, "choose_initial_reviewer", no_reviewer)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: {
        "number": 12, "url": "https://example/pr/12", "state": "OPEN",
        "createdAt": "2026-09-01T12:00:00Z", "headRefOid": "a" * 40,
        "labels": [{"name": "author:codex-1"}, {"name": "author-family:openai-codex"}, {"name": "needs-reviewer"}],
    })

    def transition(number, status, **kwargs):
        kwargs["pre_mutation_check"]()
        lifecycle[0] = status

    monkeypatch.setattr(create_pr, "set_status", transition)
    states = {service: create_pr.UNAVAILABLE for service in create_pr.EXTERNAL_REVIEWERS}
    outcome = create_pr.create(6, "feat: small", "## Summary\n\nSmall change", "codex-1", external_states=states)
    assert (outcome["reviewer"], outcome["review_required"]) == (None, True)
    assert outcome["next_action"] == "refresh-reviewer"
    assert lifecycle == ["In Review"]
    labels = [commands[0][i + 1] for i, part in enumerate(commands[0]) if part == "--label"]
    assert "needs-reviewer" in labels and not any(label.startswith("review:") for label in labels)


def test_assigning_a_reviewer_clears_the_needs_reviewer_marker(monkeypatch):
    pr = {"number": 12, "createdAt": "2026-09-01T12:00:00Z", "headRefOid": "a" * 40,
          "labels": [{"name": "needs-reviewer"}, {"name": "author:codex-1"}, {"name": "author-family:openai-codex"}],
          "author": {"login": "someone"}}
    monkeypatch.setattr(create_pr, "gh_json", lambda _argv: pr)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    commands = []
    monkeypatch.setattr(create_pr, "run", lambda argv: commands.append(argv))
    create_pr.replace_authority(12, pr, "coderabbit", None, None)
    [edit] = commands
    assert edit[edit.index("--add-label") + 1] == "review:coderabbit"
    assert edit[edit.index("--remove-label") + 1] == "needs-reviewer"


def test_issue_inventory_filters_labels_locally_and_refuses_truncation(monkeypatch):
    seen = []
    records = [{"number": 1, "labels": [{"name": "status:backlog"}]}, {"number": 2, "labels": []}]
    monkeypatch.setattr(common, "gh_json", lambda args, **_kwargs: seen.append(args) or records)
    assert [record["number"] for record in common.list_issues(label="status:backlog")] == [1]
    assert "--label" not in seen[0]  # gh sends label filters through the lagging search index
    monkeypatch.setattr(common, "gh_json", lambda _args, **_kwargs: [{"number": n, "labels": []} for n in range(1000)])
    with pytest.raises(common.KernelError, match="truncated"):
        common.list_issues()


def test_revert_opens_its_pr_from_the_new_worktree(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(revert_merge, "merged_pr", lambda _n: {"title": "t", "mergeCommit": {"oid": "c" * 40}})
    monkeypatch.setattr(revert_merge, "create_worktree", lambda *_args: {"path": str(tmp_path), "branch": "fix/issue-9-r"})
    monkeypatch.setattr(revert_merge, "git", lambda *_args, **_kwargs: "p1 p2")
    monkeypatch.setattr(revert_merge, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""))

    def create(*_args):
        seen["cwd"] = os.getcwd()
        return {"pr": 5, "head": "d" * 40}

    monkeypatch.setattr(revert_merge, "create", create)
    before = os.getcwd()
    assert revert_merge.create_revert(3, 9, "codex-1")["revert_pr"] == 5
    assert seen["cwd"] == os.path.realpath(tmp_path)
    assert os.getcwd() == before
