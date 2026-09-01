from __future__ import annotations

import pytest

import merge_pr
import merge_state


HEAD = "a" * 40
BASE = "b" * 40


def ready_pr(**overrides):
    record = {
        "number": 10,
        "body": "Closes #7",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "feat/issue-7-change",
        "baseRefName": "main",
        "baseRefOid": BASE,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "labels": [],
        "statusCheckRollup": [],
    }
    record.update(overrides)
    return record


def install_low_risk_gate(monkeypatch, *, paths=None):
    paths = paths or ["src/example.py"]
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: ready_pr())
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: paths)
    monkeypatch.setattr(merge_pr, "review_risk_tier", lambda _paths: 1)
    monkeypatch.setattr(
        merge_pr,
        "issue_gate",
        lambda _issues, _paths: [{"issue": 7, "criteria": 1}],
    )
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {
            "head": HEAD,
            "state": "success",
            "checks": ["aru-governed-pr"],
        },
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(
        merge_pr,
        "merge_queue_snapshot",
        lambda *_args: {"configured": False, "entry": None, "auto_merge": None},
    )


def test_tier_one_skips_authoritative_ai_review_but_keeps_server_gate(monkeypatch):
    install_low_risk_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "assigned_service",
        lambda _pr: pytest.fail("low-risk paths must not allocate a reviewer"),
    )
    gates = merge_pr.evaluate(10, HEAD)
    assert gates["reviewer"] == "not-required"
    assert gates["risk_tier"] == 1
    assert gates["ci"] == ["aru-governed-pr"]


def test_tier_one_still_blocks_unresolved_threads(monkeypatch):
    install_low_risk_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"id": 1}])
    with pytest.raises(merge_pr.KernelError, match="unresolved review thread"):
        merge_pr.evaluate(10, HEAD)


def test_non_clean_merge_state_is_not_used_to_avoid_base_refresh(monkeypatch):
    install_low_risk_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "pull_request",
        lambda _number: ready_pr(mergeStateStatus="BEHIND"),
    )
    with pytest.raises(merge_pr.KernelError, match="BEHIND"):
        merge_pr.evaluate(10, HEAD)


def test_behind_head_is_allowed_only_when_merge_queue_rechecks_integration(monkeypatch):
    install_low_risk_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "pull_request",
        lambda _number: ready_pr(mergeStateStatus="BEHIND"),
    )
    monkeypatch.setattr(
        merge_pr,
        "merge_queue_snapshot",
        lambda *_args: {"configured": True, "entry": None, "auto_merge": None},
    )
    assert merge_pr.evaluate(10, HEAD)["merge_queue"] is True


def issue_record(touches: str):
    return {
        "body": (
            "## Acceptance Criteria\n\n"
            "- [x] exact behavior is verified\n\n"
            "### touches:\n"
            f"{touches}\n"
        ),
        "state": "OPEN",
        "labels": [
            {"name": "status:in-review"},
            {"name": "agent:codex-1"},
        ],
    }


def test_issue_gate_checks_actual_paths_with_canonical_touches_parser(monkeypatch):
    monkeypatch.setattr(
        merge_state,
        "issue",
        lambda _number: issue_record("src/example.py, tests/**"),
    )
    evidence = merge_pr.issue_gate([7], ["src/example.py", "tests/test_example.py"])
    assert evidence == [
        {
            "issue": 7,
            "criteria": 1,
            "touches": ["src/example.py", "tests/**"],
            "claimant": "codex-1",
        }
    ]


def test_issue_gate_refuses_actual_path_outside_touches(monkeypatch):
    monkeypatch.setattr(
        merge_state,
        "issue",
        lambda _number: issue_record("src/example.py"),
    )
    with pytest.raises(merge_pr.KernelError, match="outside.*touches"):
        merge_pr.issue_gate([7], ["src/example.py", "prod/config.yml"])


def test_pull_changed_paths_includes_both_sides_of_rename(monkeypatch):
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        merge_state,
        "gh_paginated",
        lambda endpoint: [
            {"filename": "src/new.py", "previous_filename": "src/old.py"},
            {"filename": "tests/test_new.py"},
        ],
    )
    assert merge_pr.pull_changed_paths(10) == [
        "src/new.py",
        "src/old.py",
        "tests/test_new.py",
    ]


def test_pull_changed_paths_fails_closed_on_empty_inventory(monkeypatch):
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(merge_state, "gh_paginated", lambda _endpoint: [])
    with pytest.raises(merge_pr.KernelError, match="no changed files"):
        merge_pr.pull_changed_paths(10)


def test_base_snapshot_uses_current_base_ref_oid_without_behind_gate():
    assert merge_pr.base_snapshot(ready_pr()) == BASE
    with pytest.raises(merge_pr.KernelError, match="base snapshot"):
        merge_pr.base_snapshot(ready_pr(baseRefOid="short"))


def test_merge_queue_snapshot_is_bound_to_exact_head(monkeypatch):
    seen = {}
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")

    def query(argv, *, auth):
        seen["argv"] = argv
        seen["auth"] = auth
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 10,
                        "headRefOid": HEAD,
                        "baseRefOid": BASE,
                        "mergeQueue": {"id": "queue"},
                        "mergeQueueEntry": {"id": "entry", "state": "QUEUED"},
                        "autoMergeRequest": None,
                    }
                }
            }
        }

    monkeypatch.setattr(merge_state, "gh_json", query)
    snapshot = merge_pr.merge_queue_snapshot(10, HEAD)
    assert snapshot["configured"] is True
    assert snapshot["entry"]["state"] == "QUEUED"
    assert seen["auth"] == merge_state.REPOSITORY_AUTH
    assert seen["argv"][:2] == ["api", "graphql"]


def test_merge_submits_to_configured_queue_without_merge_strategy(monkeypatch):
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: {
            "head": HEAD,
            "base_sha": BASE,
            "issues": [{"issue": 7}],
            "merge_queue": True,
            "queue_entry": None,
            "auto_merge": None,
        },
    )
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: ready_pr())
    monkeypatch.setattr(
        merge_pr,
        "merge_queue_snapshot",
        lambda *_args: {
            "configured": True,
            "entry": {"id": "entry", "state": "QUEUED"},
            "auto_merge": None,
        },
    )
    calls = []
    monkeypatch.setattr(merge_pr, "run", lambda argv: calls.append(argv))
    monkeypatch.setattr(
        merge_pr,
        "close_out",
        lambda _issues: pytest.fail("queued PR is not closed out before merge"),
    )

    result = merge_pr.merge(10, HEAD)

    assert result["merged"] is False
    assert result["queued"] is True
    assert "--merge" not in calls[0]
    assert calls[0][-2:] == ["--match-head-commit", HEAD]


def test_merge_stops_when_base_changes_after_evaluation(monkeypatch):
    snapshots = iter(
        [
            {
                "base_sha": BASE,
                "issues": [{"issue": 7}],
                "merge_queue": False,
                "queue_entry": None,
                "auto_merge": None,
            },
            {
                "base_sha": "c" * 40,
                "issues": [{"issue": 7}],
                "merge_queue": False,
                "queue_entry": None,
                "auto_merge": None,
            },
        ]
    )
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: next(snapshots),
    )
    monkeypatch.setattr(
        merge_pr, "run", lambda _argv: pytest.fail("merge command must not run")
    )
    with pytest.raises(merge_pr.KernelError, match="authority changed"):
        merge_pr.merge(10, HEAD)


def test_finalize_merged_pr_revalidates_evidence_before_close_out(monkeypatch):
    merged = ready_pr(
        state="MERGED",
        mergedAt="2026-09-01T12:00:00Z",
        mergeCommit={"oid": "c" * 40},
    )
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: merged)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: ["src/app.py"])
    monkeypatch.setattr(merge_pr, "review_risk_tier", lambda _paths: 1)
    monkeypatch.setattr(
        merge_pr,
        "issue_gate",
        lambda *_args, **_kwargs: [{"issue": 7, "criteria": 1}],
    )
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {
            "head": HEAD,
            "state": "success",
            "checks": ["aru-governed-pr"],
        },
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    closed = []
    monkeypatch.setattr(
        merge_pr,
        "close_out",
        lambda numbers, paths: closed.extend(numbers) or [
            {"issue": numbers[0], "criteria": 1, "paths": paths}
        ],
    )

    result = merge_pr.finalize_queued(10, HEAD)

    assert result["finalized"] is True
    assert result["reviewer"] == "not-required"
    assert closed == [7]
    assert result["issues"] == [7]


def test_finalize_merged_pr_leaves_issue_open_on_post_merge_feedback(monkeypatch):
    merged = ready_pr(
        state="MERGED",
        mergedAt="2026-09-01T12:00:00Z",
        mergeCommit={"oid": "c" * 40},
    )
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: merged)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: ["src/app.py"])
    monkeypatch.setattr(
        merge_pr,
        "issue_gate",
        lambda *_args, **_kwargs: [{"issue": 7, "criteria": 1}],
    )
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {
            "head": HEAD,
            "state": "success",
            "checks": ["aru-governed-pr"],
        },
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"id": 1}])
    monkeypatch.setattr(
        merge_pr,
        "close_out",
        lambda _numbers: pytest.fail("post-merge feedback must block close-out"),
    )

    with pytest.raises(merge_pr.KernelError, match="post-queue review thread"):
        merge_pr.finalize_queued(10, HEAD)


def test_close_out_rechecks_contract_after_other_post_merge_network_calls(monkeypatch):
    gates = []
    monkeypatch.setattr(
        merge_state,
        "issue_gate",
        lambda numbers, paths, **kwargs: gates.append((numbers, paths, kwargs))
        or [{"issue": 7, "criteria": 1}],
    )
    monkeypatch.setattr(
        merge_state,
        "issue",
        lambda _number: {
            "number": 7,
            "state": "CLOSED",
            "labels": [{"name": "status:done"}],
        },
    )

    evidence = merge_state.close_out([7], ["src/app.py"])

    assert evidence == [{"issue": 7, "criteria": 1}]
    assert gates == [
        (
            [7],
            ["src/app.py"],
            {"allow_closed": True, "allow_done": True},
        ),
        (
            [7],
            ["src/app.py"],
            {"allow_closed": True, "allow_done": True},
        ),
    ]
