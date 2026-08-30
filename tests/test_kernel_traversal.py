from __future__ import annotations

import copy

import claim_issue
import create_pr
import merge_pr
import triage_backlog


def status_label(status: str) -> str:
    return "status:" + status.lower().replace(" ", "-")


def traverse(monkeypatch, number: int) -> dict:
    state = {
        "issue": {
            "number": number,
            "title": "tiny consumer change",
            "body": (
                "## Acceptance Criteria\n\n"
                "- [ ] tiny behavior works\n\n"
                "touches: app.py, tests/test_app.py\n"
            ),
            "state": "OPEN",
            "labels": [{"name": "status:backlog"}, {"name": "priority:p2"}],
        },
        "pr": None,
        "merged": False,
    }

    def current_issue(_number):
        return copy.deepcopy(state["issue"])

    pre_mutation_observations: list[tuple[int, str, str | None]] = []

    def move_status(
        _number,
        status,
        *,
        expected_current=None,
        pre_mutation_check=None,
    ):
        current_statuses = [
            label["name"][len("status:"):]
            for label in state["issue"]["labels"]
            if label["name"].startswith("status:")
        ]
        current_status = current_statuses[-1].replace("-", " ").title() if current_statuses else None
        if expected_current is not None:
            assert current_status == expected_current
        if pre_mutation_check is not None:
            pre_mutation_check()
        pre_mutation_observations.append((_number, status, expected_current))
        labels = [
            label
            for label in state["issue"]["labels"]
            if not label["name"].startswith("status:")
        ]
        labels.append({"name": status_label(status)})
        state["issue"]["labels"] = labels

    monkeypatch.setattr(triage_backlog, "list_issues", lambda **_: [current_issue(number)])
    monkeypatch.setattr(triage_backlog, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(triage_backlog, "set_status", move_status)
    assert triage_backlog.triage()["promoted"] == [number]
    assert pre_mutation_observations == [(number, "Ready", "Backlog")]

    monkeypatch.setattr(claim_issue, "issue", current_issue)
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "ensure_label", lambda *_args, **_kwargs: None)

    def claim_command(argv, **_kwargs):
        if "--add-label" in argv:
            label = argv[argv.index("--add-label") + 1]
            state["issue"]["labels"].append({"name": label})

    monkeypatch.setattr(claim_issue, "run", claim_command)
    monkeypatch.setattr(claim_issue, "set_status", move_status)
    assert claim_issue.claim(number, "codex-1")["status"] == "In Progress"

    monkeypatch.setattr(create_pr, "issue", current_issue)
    monkeypatch.setattr(create_pr, "current_branch", lambda: f"feat/issue-{number}-tiny")
    monkeypatch.setattr(create_pr, "require_published_head", lambda _branch: "a" * 40)
    monkeypatch.setattr(create_pr, "ensure_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(create_pr, "run", lambda _argv: None)
    reviewer = "coderabbit"

    def pr_snapshot(_argv):
        return {
            "number": number + 100,
            "url": f"https://example/pr/{number + 100}",
            "headRefOid": "a" * 40,
            "labels": [{"name": "review:" + reviewer}],
        }

    monkeypatch.setattr(create_pr, "gh_json", pr_snapshot)
    monkeypatch.setattr(create_pr, "set_status", move_status)
    created = create_pr.create(
        number,
        "feat: tiny",
        "Summary",
        "codex-1",
        external_states={
            "coderabbit": create_pr.AVAILABLE,
            "sourcery": create_pr.UNAVAILABLE,
            "codeant": create_pr.UNAVAILABLE,
        },
        reviewer_actors={},
        author_actor="author-login",
    )
    assert created["reviewer"] == reviewer

    state["issue"]["body"] = state["issue"]["body"].replace("- [ ]", "- [x]")
    state["pr"] = {
        "number": number + 100,
        "body": f"Closes #{number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "headRefName": f"feat/issue-{number}-tiny",
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "labels": [{"name": "review:" + reviewer}],
        "statusCheckRollup": [{"name": reviewer, "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: copy.deepcopy(state["pr"]))
    monkeypatch.setattr(merge_pr, "issue", current_issue)
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": "a" * 40, "state": "success", "checks": ["Verify"]},
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: True)
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))

    def merge_command(_argv):
        state["pr"]["mergedAt"] = "now"
        state["pr"]["mergeCommit"] = {"oid": "c" * 40}

    monkeypatch.setattr(merge_pr, "run", merge_command)

    def close_out(numbers):
        assert numbers == [number]
        move_status(number, "Done")
        state["issue"]["state"] = "CLOSED"

    monkeypatch.setattr(merge_pr, "close_out", close_out)
    result = merge_pr.merge(number + 100, "a" * 40)
    assert result["merged"]
    return state


def test_two_complete_disposable_traversals_without_state_repair(monkeypatch):
    first = traverse(monkeypatch, 3)
    monkeypatch.undo()
    second = traverse(monkeypatch, 4)
    assert first["issue"]["state"] == second["issue"]["state"] == "CLOSED"
    assert first["issue"]["labels"][-1]["name"] == "status:done"
    assert second["issue"]["labels"][-1]["name"] == "status:done"
