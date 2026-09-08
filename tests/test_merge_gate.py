from __future__ import annotations

import json

import pytest

import merge_pr


def base_pr(**overrides):
    pr = {
        "number": 10,
        "body": "Closes #7",
        "createdAt": "2026-08-27T09:00:00Z",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "headRefName": "feat/issue-7-change",
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "labels": [{"name": "review:coderabbit"}],
        "statusCheckRollup": [{"context": "CodeRabbit", "state": "SUCCESS"}],
    }
    pr.update(overrides)
    return pr


def install_happy_gate(monkeypatch, pr=None):
    pr = pr or base_pr()
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: ["scripts/merge_pr.py"])
    monkeypatch.setattr(merge_pr, "review_risk_tier", lambda _paths: 2)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda *_args: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: {"head": "a" * 40, "state": "success", "checks": ["Verify"]})
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: True)
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: "b" * 40)
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_args: {"configured": False, "entry": None, "auto_merge": None})
    return pr


def test_evaluate_accepts_exact_head_only(monkeypatch):
    install_happy_gate(monkeypatch)
    gates = merge_pr.evaluate(10, "a" * 40)
    assert gates["reviewer"] == "coderabbit"
    assert gates["base_sha"] == "b" * 40
    with pytest.raises(merge_pr.KernelError, match="expected head"):
        merge_pr.evaluate(10, "c" * 40)


def test_evaluate_blocks_unresolved_feedback(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"body": "fix"}])
    with pytest.raises(merge_pr.KernelError, match="unresolved"):
        merge_pr.evaluate(10, "a" * 40)


def test_evaluate_blocks_missing_review(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: False)
    with pytest.raises(merge_pr.KernelError, match="exact-head verdict"):
        merge_pr.evaluate(10, "a" * 40)


def test_evaluate_blocks_missing_required_github_check(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": "a" * 40, "state": "pending", "checks": []},
    )
    with pytest.raises(merge_pr.KernelError, match="required GitHub checks"):
        merge_pr.evaluate(10, "a" * 40)


def test_merge_rechecks_head_and_base(monkeypatch):
    pr = install_happy_gate(monkeypatch)
    gates = {
        "head": "a" * 40,
        "base_sha": "b" * 40,
        "base": "main",
        "changed_paths": ["scripts/merge_pr.py"],
        "risk_tier": 2, "reviewer": "coderabbit",
        "issues": [{"issue": 7, "criteria": 1}],
        "merge_queue": False,
        "queue_entry": None,
        "auto_merge": None,
    }
    evaluations = []
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: evaluations.append(True) or gates,
    )
    calls = []
    monkeypatch.setattr(merge_pr, "run", lambda argv: calls.append(argv))
    monkeypatch.setattr(merge_pr, "close_out", lambda numbers: calls.append(["close", *numbers]))
    monkeypatch.setattr(
        merge_pr,
        "pull_request",
        lambda _number: {**pr, "mergedAt": "now", "mergeCommit": {"oid": "c" * 40}},
    )
    monkeypatch.setattr(
        merge_pr,
        "finalize_queued",
        lambda number, head: calls.append(["finalize", number, head])
        or {
            "merged": True,
            "finalized": True,
            "pr": number,
            "head": head,
            "merge_commit": "c" * 40,
            "issues": [7],
        },
    )
    result = merge_pr.merge(10, "a" * 40)
    assert result["merged"] is True
    assert len(evaluations) == 2
    assert "--match-head-commit" in calls[0]
    assert calls[-1] == ["finalize", 10, "a" * 40]


def test_immediate_merge_does_not_close_out_when_post_merge_evidence_drifts(
    monkeypatch,
):
    pr = install_happy_gate(monkeypatch)
    snapshots = iter(
        [
            pr,
            pr,
            pr,
            {
                **pr,
                "state": "MERGED",
                "mergedAt": "now",
                "mergeCommit": {"oid": "c" * 40},
            },
        ]
    )
    monkeypatch.setattr(
        merge_pr,
        "pull_request",
        lambda _number: next(snapshots),
    )
    monkeypatch.setattr(merge_pr, "run", lambda _argv: None)
    monkeypatch.setattr(
        merge_pr,
        "finalize_queued",
        lambda *_args: (_ for _ in ()).throw(
            merge_pr.KernelError("post-merge authority changed")
        ),
    )
    monkeypatch.setattr(
        merge_pr,
        "close_out",
        lambda _numbers: pytest.fail("drifted post-merge evidence must stay In Review"),
    )

    with pytest.raises(merge_pr.KernelError, match="post-merge authority changed"):
        merge_pr.merge(10, "a" * 40)


def test_review_label_must_be_unique():
    with pytest.raises(merge_pr.KernelError, match="exactly one"):
        merge_pr.assigned_service(
            base_pr(labels=[{"name": "review:coderabbit"}, {"name": "review:sourcery"}])
        )


def make_codeant_record(
    commit: str,
    label: str = "Reviewed your PR",
    started: str = "2026-08-27T10:00:00Z",
    finished: str = "2026-08-27T10:05:00Z",
    done: bool = True,
    **extra,
) -> dict:
    rec = {
        "label": label,
        "commit": commit,
        "started": started,
        "finished": finished,
        "done": done,
    }
    rec.update(extra)
    return rec


def make_codeant_comment(
    records: list | str | None = None,
    login: str = "codeant-ai[bot]",
    actor_type: str = "Bot",
    body: str | None = None,
) -> dict:
    if body is None:
        if isinstance(records, list):
            import json

            marker = f"<!-- codeant-review-status:{json.dumps(records)} -->"
        elif isinstance(records, str):
            marker = f"<!-- codeant-review-status:{records} -->"
        else:
            marker = ""
        body = f"## 🤖 CodeAnt AI — Review Status\n\n| Status | Commit |\n{marker}"
    return {
        "id": 1,
        "body": body,
        "created_at": "2026-08-27T10:05:00Z",
        "user": {"login": login, "type": actor_type},
    }


def make_review(
    commit_id: str,
    state: str = "COMMENTED",
    login: str = "codeant-ai[bot]",
    actor_type: str = "Bot",
) -> dict:
    return {
        "id": 1,
        "commit_id": commit_id,
        "state": state,
        "submitted_at": "2026-08-27T10:05:00Z",
        "user": {"login": login, "type": actor_type},
    }


def install_codeant_pr(monkeypatch, head: str, pr_number: int = 146, feedback: list | None = None):
    pr = base_pr(
        number=pr_number,
        headRefOid=head,
        labels=[{"name": "review:codeant"}],
        statusCheckRollup=[],
    )
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: ["scripts/merge_pr.py"])
    monkeypatch.setattr(merge_pr, "review_risk_tier", lambda _paths: 2)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda *_args: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: {"head": head, "state": "success", "checks": ["Verify"]})
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: feedback or [])
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: "b" * 40)
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_args: {"configured": False, "entry": None, "auto_merge": None})
    monkeypatch.setattr(merge_pr, "pull_events", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_review_checks", lambda _head: [])
    return pr


def test_codeant_clean_status_reproducing_jmc_pr_146(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    prior_head = "1" * 40
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(
        commit_id=prior_head,
        state="COMMENTED",
        login="codeant-ai[bot]",
        actor_type="Bot",
    )
    status_comment = make_codeant_comment(
        records=[
            make_codeant_record(commit=head, label="Reviewed your PR", done=True),
            make_codeant_record(
                commit=head,
                label="Incremental review completed",
                done=True,
            ),
            make_codeant_record(commit=prior_head, label="Reviewed your PR", done=True),
        ],
        login="codeant-ai[bot]",
        actor_type="Bot",
    )

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")
    with pytest.raises(merge_pr.KernelError, match="retired review authority"):
        merge_pr.merge(146, head, dry_run=True)


def test_codeant_current_head_incremental_without_full_review_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    old_head = "1" * 40
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [
        make_codeant_record(commit=old_head, label="Reviewed your PR", done=True),
        make_codeant_record(commit=head, label="Incremental review completed", done=True),
    ]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_stale_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    old_head = "1" * 40
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=old_head, label="Reviewed your PR", done=True)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_abbreviated_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=head[:12], label="Reviewed your PR", done=True)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_unfinished_record_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=head, label="Reviewed your PR", done=False)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_missing_finished_timestamp_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, finished="")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_invalid_timestamp_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, started="invalid-timestamp")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_missing_record_keys_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = {"commit": head, "label": "Reviewed your PR", "done": True}
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_extra_record_keys_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, extra_field="unexpected")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_empty_label_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, label="   ")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_malformed_marker_json_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    status_comment = make_codeant_comment(records="{not-valid-json}")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_non_list_marker_payload_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    status_comment = make_codeant_comment(records='{"commit": "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"}')

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_multiple_markers_in_one_comment_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    marker1 = f"<!-- codeant-review-status:[{{\"commit\": \"{head}\", \"label\": \"a\", \"started\": \"2026-08-27T10:00:00Z\", \"finished\": \"2026-08-27T10:05:00Z\", \"done\": true}}] -->"
    marker2 = f"<!-- codeant-review-status:[{{\"commit\": \"{head}\", \"label\": \"b\", \"started\": \"2026-08-27T10:00:00Z\", \"finished\": \"2026-08-27T10:05:00Z\", \"done\": true}}] -->"
    body = f"Review\n{marker1}\n{marker2}"
    status_comment = make_codeant_comment(body=body)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_duplicate_full_records_for_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [
        make_codeant_record(commit=head, label="Reviewed your PR", done=True),
        make_codeant_record(commit=head, label="Reviewed your PR", done=True),
        make_codeant_record(commit=head, label="Incremental review completed", done=True),
    ]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_duplicate_trusted_comments_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    c1 = make_codeant_comment(records=[record])
    c2 = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [c1, c2])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_copied_human_marker_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    human_comment = make_codeant_comment(records=[record], login="some-developer", actor_type="User")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [human_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_spoofed_bot_login_with_user_type_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    spoofed_comment = make_codeant_comment(records=[record], login="codeant-ai", actor_type="User")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [spoofed_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_trusted_review_history_allows_status_without_exact_head_review(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    old_review = make_review(commit_id="1" * 40, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [old_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_status_without_trusted_review_history_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_canonical_github_app_author_cannot_satisfy_coding_review_submission():
    pr = base_pr(
        number=88,
        body="Closes #508",
        headRefOid="a" * 40,
        labels=[
            {"name": "review:claude-code"},
            {"name": "reviewer:claude-code-sub-1"},
            {"name": "reviewer-actor:aru-code-factory-gillella[bot]"},
            {"name": "author:codex-author"},
            {"name": "author-family:openai-codex"},
        ],
        author={"login": "app/aru-code-factory-gillella"},
        statusCheckRollup=[],
    )
    payload = {
        "head": "a" * 40,
        "reviewer": "claude-code-sub-1",
        "family": "claude-code",
        "submitted_by": "aru-code-factory-gillella[bot]",
        "verdict": "APPROVE",
        "summary": "I reviewed the issue contract, exact diff, surrounding code, and failure paths independently.",
        "verification": ["pytest tests/test_merge_gate.py -q completed successfully"],
        "findings": [
            {
                "severity": "low",
                "file": "scripts/merge_pr.py",
                "line": 300,
                "summary": "The resolved naming note does not block this exact head.",
                "resolved": True,
            }
        ],
        "issues": [508],
        "acceptance_criteria_reviewed": True,
        "diff_reviewed": True,
        "surrounding_code_reviewed": True,
    }
    review = {
        "id": 1,
        "commit_id": "a" * 40,
        "state": "APPROVED",
        "body": (
            "Substantive independent review.\n\n"
            f"<!-- aru-coding-review:v1 {json.dumps(payload, sort_keys=True)} -->"
        ),
        "user": {"login": "aru-code-factory-gillella[bot]", "type": "Bot"},
    }
    assert (
        merge_pr.successful_coding_agent_review(pr, [review], "claude-code", [508]) is False
    )


@pytest.mark.parametrize(
    "record",
    [
        make_codeant_record(
            commit="db49ace5f70ae8b5fe8b1ce341ad997bb77db071",
            label="Review completed",
        ),
        make_codeant_record(commit="1" * 40, label="Review completed"),
        make_codeant_record(commit="1" * 12),
        make_codeant_record(commit="1" * 40, done=False),
    ],
    ids=[
        "unknown-current-head-label",
        "unknown-history-label",
        "abbreviated-history-commit",
        "unfinished-history-record",
    ],
)
def test_codeant_invalid_status_history_fails_closed(monkeypatch, record):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id="1" * 40, state="COMMENTED")
    records = [make_codeant_record(commit=head), record]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_unfinished_incremental_record_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id="1" * 40, state="COMMENTED")
    records = [
        make_codeant_record(commit=head),
        make_codeant_record(
            commit=head,
            label="Incremental review completed",
            done=False,
        ),
    ]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_malformed_marker_beside_valid_comment_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    valid_record = make_codeant_record(commit=head, done=True)
    valid_comment = make_codeant_comment(records=[valid_record])
    malformed_comment = make_codeant_comment(records="{not-valid-json}")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [valid_comment, malformed_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_valid_marker_beside_trusted_prose_comment_passes(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    valid_record = make_codeant_record(commit=head, done=True)
    valid_comment = make_codeant_comment(records=[valid_record])
    prose_comment = {
        "id": 2,
        "body": "## CodeAnt AI\n\nGeneral summary prose with no status marker.",
        "user": {"login": "codeant-ai[bot]", "type": "Bot"},
    }

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [valid_comment, prose_comment])

    assert merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_changes_requested_review_blocks_merge(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    blocking_review = make_review(commit_id=head, state="CHANGES_REQUESTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [blocking_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_conflicting_approved_and_changes_requested_blocks_merge(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    approved_review = make_review(commit_id=head, state="APPROVED")
    blocking_review = make_review(commit_id=head, state="CHANGES_REQUESTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    # Test APPROVED before CHANGES_REQUESTED
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [approved_review, blocking_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])
    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")

    # Test CHANGES_REQUESTED before APPROVED
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [blocking_review, approved_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])
    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_check_cannot_override_exact_head_changes_requested(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    pr = install_codeant_pr(monkeypatch, head, pr_number=146)
    pr["statusCheckRollup"] = [{"context": "CodeAnt AI", "state": "SUCCESS"}]

    blocking_review = make_review(commit_id=head, state="CHANGES_REQUESTED")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [blocking_review])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_plain_prose_without_marker_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    prose_comment = {
        "id": 1,
        "body": "## CodeAnt AI\n\nReview completed successfully for db49ace5f70ae8b5fe8b1ce341ad997bb77db071. No issues found.",
        "user": {"login": "codeant-ai[bot]", "type": "Bot"},
    }

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [prose_comment])

    assert not merge_pr.exact_head_review(merge_pr.pull_request(146), 146, "codeant")


def test_codeant_blocks_on_unresolved_feedback(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(
        monkeypatch,
        head,
        pr_number=146,
        feedback=[{"path": "scripts/merge_pr.py", "line": 10, "body": "fix finding"}],
    )

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="1 unresolved review thread"):
        merge_pr.evaluate(146, head)
