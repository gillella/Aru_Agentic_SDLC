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


def test_merge_rechecks_head_and_base(monkeypatch, tmp_path):
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
    worktree = tmp_path / ".worktrees" / "issue-7"
    worktree.mkdir(parents=True)

    def submit(argv):
        # gh would merge remotely, then fail deleting a checked-out local branch.
        if "--delete-branch" in argv and worktree.exists():
            raise merge_pr.KernelError("branch is used by worktree")
        calls.append(argv)

    monkeypatch.setattr(merge_pr, "run", submit)
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
    assert worktree.is_dir()
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


@pytest.mark.parametrize("ci_state", ["success", "pending", "failure", "untrusted"])
def test_provenance_aware_duplicate_ci_does_not_bypass_review_or_merge(monkeypatch, ci_state):
    import check_ci
    from test_ci_and_feedback import PR, check_run, workflow_run, install_checks
    install_happy_gate(monkeypatch, base_pr(**PR))
    monkeypatch.setattr(merge_pr, "ci_verdict", check_ci.ci_verdict)
    monkeypatch.setattr(merge_pr, "exact_head_review", lambda *_a: False)
    old = workflow_run(10, created="2026-09-09T11:01:14Z")
    current = workflow_run(state="success" if ci_state == "untrusted" else ci_state)
    checks = [check_run("aru-governed-pr", run_id=10), check_run("aru-governed-pr",
        status=current["status"], conclusion=current["conclusion"] or "")]
    if ci_state == "untrusted":
        checks[0]["app"]["id"] = 999
    install_checks(monkeypatch, checks, workflows=[old, current])
    reason = "exact-head verdict" if ci_state == "success" else "untrusted source" if ci_state == "untrusted" else "required GitHub checks"
    with pytest.raises(merge_pr.KernelError, match=reason):
        merge_pr.evaluate(3, "a" * 40)


PAUSED_SUMMARY = "## Walkthrough\n<!-- review paused by coderabbit.ai -->\n> ## Reviews paused\n"


def install_coderabbit_evidence(monkeypatch, *, review_commit=None, approved=True, body=PAUSED_SUMMARY):
    """PR #655: CodeRabbit approved the head, then edited "Reviews paused" into its summary."""
    pr = base_pr(createdAt="2026-09-11T00:43:20Z")
    reviews = [{"id": 1, "state": "APPROVED" if approved else "COMMENTED", "body": "",
                "commit_id": review_commit or pr["headRefOid"], "submitted_at": "2026-09-11T00:44:54Z",
                "user": {"login": "coderabbitai[bot]", "type": "Bot"}}]
    comments = [{"id": 2, "created_at": "2026-09-11T00:43:40Z", "updated_at": "2026-09-11T00:48:05Z",
                 "body": body, "user": {"login": "coderabbitai[bot]", "type": "Bot"}}]
    events = [{"event": "labeled", "label": {"name": "review:coderabbit"}, "created_at": "2026-09-11T00:43:26Z"}]
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _n: reviews)
    monkeypatch.setattr(merge_pr, "pull_comments", lambda _n: comments)
    monkeypatch.setattr(merge_pr, "pull_events", lambda _n: events)
    monkeypatch.setattr(merge_pr, "pull_review_checks", lambda _head: [])
    return pr


def test_later_pause_notice_does_not_retract_an_exact_head_approval(monkeypatch):
    import review_evidence

    pr = install_coderabbit_evidence(monkeypatch)
    since = review_evidence.authority_assigned_at(pr, merge_pr.pull_events(655), "coderabbit")
    # Reviewer refresh still reads the pause as unavailability; only the verdict stands.
    assert review_evidence.external_state(
        "coderabbit", reviews=merge_pr.pull_reviews(655), comments=merge_pr.pull_comments(655),
        checks=[], head=pr["headRefOid"], since=since,
    ) == review_evidence.UNAVAILABLE
    assert merge_pr.exact_head_review(pr, 655, "coderabbit") is True


@pytest.mark.parametrize("review_commit, approved", [("b" * 40, True), (None, False)])
def test_pause_notice_still_blocks_without_a_current_head_approval(monkeypatch, review_commit, approved):
    pr = install_coderabbit_evidence(monkeypatch, review_commit=review_commit, approved=approved)
    assert merge_pr.exact_head_review(pr, 655, "coderabbit") is False


def test_pause_notice_with_another_unavailability_signal_still_retracts(monkeypatch):
    pr = install_coderabbit_evidence(monkeypatch, body=PAUSED_SUMMARY + "\nRate limit exceeded.\n")
    assert merge_pr.exact_head_review(pr, 655, "coderabbit") is False
