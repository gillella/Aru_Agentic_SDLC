from __future__ import annotations

import pytest

import merge_pr

HEAD = "a" * 40


def base_pr(**overrides):
    pr = {
        "number": 10,
        "body": "Closes #7",
        "createdAt": "2026-08-27T09:00:00Z",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "feat/issue-7-change",
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "author": {"login": "writer"},
        "labels": [],
        "statusCheckRollup": [],
    }
    pr.update(overrides)
    return pr


def approval(**overrides):
    review = {"id": 1, "user": {"login": "reviewer"}, "commit_id": HEAD, "state": "APPROVED"}
    review.update(overrides)
    return review


def install_happy_gate(monkeypatch, pr=None):
    pr = pr or base_pr()
    monkeypatch.setattr(merge_pr, "pull_request", lambda _number: pr)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _number: ["scripts/merge_pr.py"])
    monkeypatch.setattr(merge_pr, "issue_gate", lambda *_args: [{"issue": 7, "criteria": 1}])
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: {"head": HEAD, "state": "success", "checks": ["Verify"]})
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [])
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [approval()])
    monkeypatch.setattr(merge_pr, "base_snapshot", lambda _pr: "b" * 40)
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_args: {"configured": False, "entry": None, "auto_merge": None})
    return pr


def test_evaluate_accepts_exact_head_only(monkeypatch):
    install_happy_gate(monkeypatch)
    gates = merge_pr.evaluate(10, HEAD)
    assert gates["approved"] is True
    assert gates["base_sha"] == "b" * 40
    with pytest.raises(merge_pr.KernelError, match="expected head"):
        merge_pr.evaluate(10, "c" * 40)


def test_evaluate_blocks_unresolved_feedback(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"body": "fix"}])
    with pytest.raises(merge_pr.KernelError, match="unresolved"):
        merge_pr.evaluate(10, HEAD)


def test_evaluate_blocks_missing_approval(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _number: [])
    with pytest.raises(merge_pr.KernelError, match="approval of the exact head"):
        merge_pr.evaluate(10, HEAD)


def test_evaluate_blocks_missing_required_github_check(monkeypatch):
    install_happy_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "ci_verdict",
        lambda _number: {"head": HEAD, "state": "pending", "checks": []},
    )
    with pytest.raises(merge_pr.KernelError, match="required GitHub checks"):
        merge_pr.evaluate(10, HEAD)


def test_merge_rechecks_head_and_base(monkeypatch, tmp_path):
    pr = install_happy_gate(monkeypatch)
    gates = {
        "head": HEAD,
        "base_sha": "b" * 40,
        "base": "main",
        "changed_paths": ["scripts/merge_pr.py"],
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
    result = merge_pr.merge(10, HEAD)
    assert result["merged"] is True
    assert worktree.is_dir()
    assert len(evaluations) == 2
    assert "--match-head-commit" in calls[0]
    assert calls[-1] == ["finalize", 10, HEAD]


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
        merge_pr.merge(10, HEAD)


@pytest.mark.parametrize("reviews,approved", [
    ([approval()], True),
    ([], False),
    # Approving an earlier commit does not carry to a new head.
    ([approval(commit_id="c" * 40)], False),
    ([approval(user={"login": "writer"})], False),
    # Each account's latest decisive review counts; comments change nothing.
    ([approval(), approval(id=2, state="COMMENTED")], True),
    ([approval(), approval(id=2, state="CHANGES_REQUESTED")], False),
    ([approval(), approval(id=2, state="DISMISSED")], False),
    ([approval(state="CHANGES_REQUESTED"), approval(id=2)], True),
    # Logins group by account, so casing cannot keep a superseded approval alive.
    ([approval(user={"login": "Reviewer"}), approval(id=2, state="DISMISSED")], False),
    ([approval(user={"login": "Writer"})], False),
])
def test_only_a_non_author_approval_of_the_exact_head_counts(reviews, approved):
    assert merge_pr.approved_at_head(base_pr(), reviews) is approved


def test_github_app_author_cannot_approve_through_its_bot_login():
    pr = base_pr(author={"login": "app/aru-code-factory-gillella"})
    bot = approval(user={"login": "aru-code-factory-gillella[bot]"})
    assert merge_pr.approved_at_head(pr, [bot]) is False
    assert merge_pr.approved_at_head(pr, [bot, approval(id=2)]) is True


@pytest.mark.parametrize("pr,reviews", [
    (base_pr(author=None), [approval()]),
    (base_pr(author="writer"), [approval()]),
    (base_pr(author={"login": "   "}), [approval()]),
    (base_pr(), [None]),
    (base_pr(), [{"id": 1, "state": "APPROVED", "commit_id": HEAD}]),
    # Blank or non-string reviewer identities are malformed, not "another account".
    (base_pr(), [approval(user={"login": ""})]),
    (base_pr(), [approval(user={"login": "   "})]),
    (base_pr(), [approval(user={"login": 42})]),
    (base_pr(), [approval(user={"login": {"name": "reviewer"}})]),
    (base_pr(), [approval(state="COMMENTED", user={"login": None})]),
])
def test_unreadable_author_or_review_evidence_fails_closed(pr, reviews):
    with pytest.raises(merge_pr.KernelError):
        merge_pr.approved_at_head(pr, reviews)


@pytest.mark.parametrize("ci_state", ["success", "pending", "failure", "untrusted"])
def test_provenance_aware_duplicate_ci_does_not_bypass_review_or_merge(monkeypatch, ci_state):
    import check_ci
    from test_ci_and_feedback import PR, check_run, workflow_run, install_checks
    install_happy_gate(monkeypatch, base_pr(**PR))
    monkeypatch.setattr(merge_pr, "ci_verdict", check_ci.ci_verdict)
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _n: [])
    old = workflow_run(10, created="2026-09-09T11:01:14Z")
    current = workflow_run(state="success" if ci_state == "untrusted" else ci_state)
    checks = [check_run("aru-governed-pr", run_id=10), check_run("aru-governed-pr",
        status=current["status"], conclusion=current["conclusion"] or "")]
    if ci_state == "untrusted":
        checks[0]["app"]["id"] = 999
    install_checks(monkeypatch, checks, workflows=[old, current])
    reason = "approval of the exact head" if ci_state == "success" else "untrusted source" if ci_state == "untrusted" else "required GitHub checks"
    with pytest.raises(merge_pr.KernelError, match=reason):
        merge_pr.evaluate(3, HEAD)
