from __future__ import annotations

import pytest

import merge_pr


def base_pr(**overrides):
    pr = {
        "number": 10,
        "body": "Closes #7",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "headRefName": "feat/issue-7-change",
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "labels": [{"name": "review:coderabbit"}],
        "statusCheckRollup": [{"context": "CodeRabbit", "state": "SUCCESS"}],
    }
    pr.update(overrides)
    return pr


def install_happy_gate(monkeypatch, pr=None):
    pr = pr or base_pr()
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda _numbers: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": "a" * 40, "state": "success", "checks": ["Verify"]},
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_args: True)
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))
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


def test_merge_rechecks_head_and_base(monkeypatch):
    pr = install_happy_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: {
            "head": "a" * 40,
            "base_sha": "b" * 40,
            "issues": [{"issue": 7}],
        },
    )
    calls = []
    monkeypatch.setattr(merge_pr, "run", lambda argv: calls.append(argv))
    monkeypatch.setattr(merge_pr, "close_out", lambda numbers: calls.append(["close", *numbers]))
    snapshots = iter([pr, {**pr, "mergedAt": "now", "mergeCommit": {"oid": "c" * 40}}])
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: next(snapshots))
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))
    result = merge_pr.merge(10, "a" * 40)
    assert result["merged"] is True
    assert "--match-head-commit" in calls[0]
    assert calls[-1] == ["close", 7]


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
    monkeypatch.setattr(merge_pr, "issue_gate", lambda _numbers: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": head, "state": "success", "checks": ["Verify"]},
    )
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: feedback or [])
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: ("b" * 40, 0))
    return pr


def test_codeant_clean_status_reproducing_jmc_pr_146(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED", login="codeant-ai[bot]", actor_type="Bot")
    status_record = make_codeant_record(commit=head, label="Reviewed your PR", done=True)
    status_comment = make_codeant_comment(records=[status_record], login="codeant-ai[bot]", actor_type="Bot")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    gates = merge_pr.evaluate(146, head)
    assert gates["reviewer"] == "codeant"
    assert gates["head"] == head
    assert gates["feedback"] == 0

    dry_run = merge_pr.merge(146, head, dry_run=True)
    assert dry_run["merged"] is False
    assert dry_run["gates"]["reviewer"] == "codeant"


def test_codeant_multiple_records_history_with_current_head_passes(monkeypatch):
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

    gates = merge_pr.evaluate(146, head)
    assert gates["reviewer"] == "codeant"


def test_codeant_stale_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    old_head = "1" * 40
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=old_head, label="Reviewed your PR", done=True)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_abbreviated_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=head[:12], label="Reviewed your PR", done=True)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_unfinished_record_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [make_codeant_record(commit=head, label="Reviewed your PR", done=False)]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_missing_finished_timestamp_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, finished="")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_invalid_timestamp_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, started="invalid-timestamp")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_missing_record_keys_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = {"commit": head, "label": "Reviewed your PR", "done": True}
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_extra_record_keys_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, extra_field="unexpected")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_empty_label_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    bad_record = make_codeant_record(commit=head, label="   ")
    status_comment = make_codeant_comment(records=[bad_record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_malformed_marker_json_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    status_comment = make_codeant_comment(records="{not-valid-json}")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_non_list_marker_payload_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    status_comment = make_codeant_comment(records='{"commit": "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"}')

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


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

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_duplicate_records_for_head_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    records = [
        make_codeant_record(commit=head, label="Reviewed your PR", done=True),
        make_codeant_record(commit=head, label="Incremental review completed", done=True),
    ]
    status_comment = make_codeant_comment(records=records)

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_duplicate_trusted_comments_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    c1 = make_codeant_comment(records=[record])
    c2 = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [c1, c2])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_copied_human_marker_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    human_comment = make_codeant_comment(records=[record], login="some-developer", actor_type="User")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [human_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_spoofed_bot_login_with_user_type_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    spoofed_comment = make_codeant_comment(records=[record], login="codeant-ai", actor_type="User")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [spoofed_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_missing_exact_head_review_object_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    # Review object exists for old commit, but not current head
    old_review = make_review(commit_id="1" * 40, state="COMMENTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [old_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


def test_codeant_malformed_marker_beside_valid_comment_fails_closed(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    review_obj = make_review(commit_id=head, state="COMMENTED")
    valid_record = make_codeant_record(commit=head, done=True)
    valid_comment = make_codeant_comment(records=[valid_record])
    malformed_comment = make_codeant_comment(records="{not-valid-json}")

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [review_obj])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [valid_comment, malformed_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


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

    gates = merge_pr.evaluate(146, head)
    assert gates["reviewer"] == "codeant"
    assert gates["head"] == head


def test_codeant_changes_requested_review_blocks_merge(monkeypatch):
    head = "db49ace5f70ae8b5fe8b1ce341ad997bb77db071"
    install_codeant_pr(monkeypatch, head, pr_number=146)

    blocking_review = make_review(commit_id=head, state="CHANGES_REQUESTED")
    record = make_codeant_record(commit=head, done=True)
    status_comment = make_codeant_comment(records=[record])

    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [blocking_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


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
    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)

    # Test CHANGES_REQUESTED before APPROVED
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [blocking_review, approved_review])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [status_comment])
    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


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

    with pytest.raises(merge_pr.KernelError, match="codeant has no successful exact-head verdict"):
        merge_pr.evaluate(146, head)


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


def test_preserve_coderabbit_and_sourcery_evidence_paths(monkeypatch):
    head = "a" * 40
    # 1. CodeRabbit check success
    pr_cr = base_pr(labels=[{"name": "review:coderabbit"}], statusCheckRollup=[{"context": "CodeRabbit", "state": "SUCCESS"}])
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    assert merge_pr.exact_head_review(pr_cr, 10, "coderabbit") is True

    # 2. Sourcery APPROVED review
    pr_sc = base_pr(labels=[{"name": "review:sourcery"}], statusCheckRollup=[])
    sourcery_approved = make_review(commit_id=head, state="APPROVED", login="sourcery-ai[bot]", actor_type="Bot")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [sourcery_approved])
    assert merge_pr.exact_head_review(pr_sc, 10, "sourcery") is True

    # 3. CodeAnt APPROVED review
    pr_ca = base_pr(labels=[{"name": "review:codeant"}], statusCheckRollup=[])
    codeant_approved = make_review(commit_id=head, state="APPROVED", login="codeant-ai[bot]", actor_type="Bot")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [codeant_approved])
    assert merge_pr.exact_head_review(pr_ca, 10, "codeant") is True

    # 4. Human APPROVED review does not satisfy coderabbit or codeant
    pr_cr_no_check = base_pr(labels=[{"name": "review:coderabbit"}], statusCheckRollup=[])
    human_approved = make_review(commit_id=head, state="APPROVED", login="human-reviewer", actor_type="User")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [human_approved])
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _number: [])
    assert merge_pr.exact_head_review(pr_cr_no_check, 10, "coderabbit") is False
    assert merge_pr.exact_head_review(pr_ca, 10, "codeant") is False

    # 5. Sourcery conflicting APPROVED + CHANGES_REQUESTED review fails
    sourcery_changes = make_review(commit_id=head, state="CHANGES_REQUESTED", login="sourcery-ai[bot]", actor_type="Bot")
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [sourcery_approved, sourcery_changes])
    assert merge_pr.exact_head_review(pr_sc, 10, "sourcery") is False
