from __future__ import annotations

import copy
import subprocess

import pytest

import claim_issue
import create_pr
import merge_pr
import review_authority
import merge_state
import triage_backlog


def status_label(status: str) -> str:
    return "status:" + status.lower().replace(" ", "-")


def verification_body(command: str) -> str:
    return f"## Summary\n\nSummary\n\n## Verification\n\n- `{command}`"


def traverse(monkeypatch, number: int) -> dict:
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("unexpected external call"))
    # This traversal stubs every external. The review posture is one: resolve it to the
    # permissive rule so the merge gate is exercised without reaching the default branch.
    monkeypatch.setattr(review_authority, "read_policy_text", lambda **_: '{"authority": "any"}')
    finalized = []
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

    def move_status(_number, status, *, expected_current=None, pre_mutation_check=None):
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
    monkeypatch.setattr(triage_backlog, "ensure_label", lambda *_args, **_kwargs: None)

    def pin_command(argv, **_kwargs):  # apply the Ready pin so the merge gate verifies it later
        if "--add-label" in argv:
            state["issue"]["labels"].append({"name": argv[argv.index("--add-label") + 1]})

    monkeypatch.setattr(triage_backlog, "run", pin_command)
    assert triage_backlog.triage()["promoted"] == [number]
    assert any(label["name"].startswith("ready:") for label in state["issue"]["labels"])
    assert pre_mutation_observations == [(number, "Ready", "Backlog")]

    monkeypatch.setattr(claim_issue, "issue", current_issue)
    monkeypatch.setattr(claim_issue, "unresolved_dependencies", lambda _record: [])
    monkeypatch.setattr(claim_issue, "other_active_claims", lambda *_args: [])
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
    monkeypatch.setattr(create_pr, "local_changed_paths", lambda: ["src/auth/session.py"])
    monkeypatch.setattr(create_pr, "run", lambda _argv: None)

    def pr_snapshot(_argv):
        return {
            "number": number + 100,
            "url": f"https://example/pr/{number + 100}",
            "state": "OPEN",
            "headRefOid": "a" * 40,
        }

    monkeypatch.setattr(create_pr, "gh_json", pr_snapshot)
    monkeypatch.setattr(create_pr, "set_status", move_status)
    created = create_pr.create(
        number,
        "feat: tiny",
        verification_body("pytest tests/test_kernel_traversal.py -q"),
        "codex-1",
    )
    assert created["next_action"] == "await-approval-by-another-account"

    state["issue"]["body"] = state["issue"]["body"].replace("- [ ]", "- [x]")
    state["pr"] = {
        "number": number + 100,
        "body": f"Closes #{number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "headRefName": f"feat/issue-{number}-tiny",
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "author": {"login": "writer"},
        "labels": [],
        "statusCheckRollup": [],
    }
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: copy.deepcopy(state["pr"]))
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _n: ["app.py", "tests/test_app.py"])
    monkeypatch.setattr(merge_state, "issue", current_issue)
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: {
        "project_id": "PVT_1", "item_id": f"PVTI_{number}", "status_field_id": "FIELD_1",
        "status": merge_state.status_of(state["issue"]),
    })
    ci = {"head": "a" * 40, "state": "success", "checks": ["Verify"]}
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: ci)

    def finalization_ci(pr):
        # This lifecycle fixture stubs CI, but retains the real queue-history gate.
        merge_state.require_direct_merge_history(pr["number"], pr["headRefOid"], pr["mergeCommit"]["oid"])
        finalized.append(pr["number"])
        return ci

    monkeypatch.setattr(merge_pr, "finalization_verdict", finalization_ci)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [
        {"id": 1, "user": {"login": "reviewer"}, "commit_id": "a" * 40, "state": "APPROVED"},
    ])
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: "b" * 40)
    monkeypatch.setattr(
        merge_pr,
        "merge_queue_snapshot",
        lambda *_args: {"configured": False, "entry": None, "auto_merge": None},
    )

    def merge_command(_argv):
        state["pr"]["state"] = "MERGED"
        state["pr"]["mergedAt"] = "now"
        state["pr"]["mergeCommit"] = {"oid": "c" * 40}

    monkeypatch.setattr(merge_pr, "run", merge_command)
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/disposable")
    monkeypatch.setattr(merge_state, "gh_json", lambda *_a, **_kw: {"data": {"repository": {
        "pullRequest": {**copy.deepcopy(state["pr"]), "timelineItems": {
            "nodes": [], "pageInfo": {"hasNextPage": False},
        }},
    }}})

    def close_out(numbers, _changed_paths):
        assert numbers == [number] and finalized == [number + 100]
        move_status(number, "Done")
        state["issue"]["state"] = "CLOSED"
        return [{"issue": number}]

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
