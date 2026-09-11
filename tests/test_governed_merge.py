from __future__ import annotations

import pytest

import merge_pr
import merge_state


HEAD = "a" * 40
BASE = "b" * 40


def board_evidence(status="In Review"):
    return {"project_id": "PVT_1", "item_id": "PVTI_7", "status_field_id": "FIELD_1", "status": status}


@pytest.fixture(autouse=True)
def isolated_board_evidence(monkeypatch):
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: board_evidence())


def ready_pr(**overrides):
    record = dict(
        number=10, body="Closes #7", state="OPEN", isDraft=False,
        headRefOid=HEAD, headRefName="feat/issue-7-change", baseRefName="main", baseRefOid=BASE,
        mergeable="MERGEABLE", mergeStateStatus="CLEAN", reviewDecision=None,
        author={"login": "writer"}, labels=[], statusCheckRollup=[],
    )
    record.update(overrides)
    return record


def approval(**overrides):
    review = dict(id=1, user={"login": "reviewer"}, commit_id=HEAD, state="APPROVED")
    review.update(overrides)
    return review


def patch_gate(monkeypatch, **functions):
    for name, function in functions.items():
        monkeypatch.setattr(merge_pr, name, function)


def install_gate(monkeypatch, *, paths=None):
    patch_gate(
        monkeypatch, pull_request=lambda _n: ready_pr(),
        pull_changed_paths=lambda _n: paths or ["src/example.py"],
        issue_gate=lambda _issues, _paths, **_kw: [{"issue": 7, "criteria": 1}],
        ci_verdict=lambda _n: {"head": HEAD, "state": "success", "checks": ["aru-governed-pr"]},
        fetch_feedback=lambda _n: [], pull_reviews=lambda _n: [approval()],
        merge_queue_snapshot=lambda *_a: {"configured": False, "entry": None, "auto_merge": None},
    )


def test_gate_passes_on_green_ci_and_a_non_author_exact_head_approval(monkeypatch):
    install_gate(monkeypatch)
    gates = merge_pr.evaluate(10, HEAD)
    assert gates["approved"] is True
    assert gates["ci"] == ["aru-governed-pr"]


def test_documentation_only_changes_still_need_an_approval(monkeypatch):
    install_gate(monkeypatch, paths=["README.md"])
    patch_gate(monkeypatch, pull_reviews=lambda _n: [])
    with pytest.raises(merge_pr.KernelError, match="approval of the exact head"):
        merge_pr.evaluate(10, HEAD)


def test_unresolved_threads_block_even_with_an_approval(monkeypatch):
    install_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _number: [{"id": 1}])
    with pytest.raises(merge_pr.KernelError, match="unresolved review thread"):
        merge_pr.evaluate(10, HEAD)


def test_non_clean_merge_state_is_not_used_to_avoid_base_refresh(monkeypatch):
    install_gate(monkeypatch)
    patch_gate(monkeypatch, pull_request=lambda _n: ready_pr(mergeStateStatus="BEHIND"))
    with pytest.raises(merge_pr.KernelError, match="BEHIND"):
        merge_pr.evaluate(10, HEAD)


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
@pytest.mark.parametrize("mode", ["configured", "entry", "auto_merge"])
def test_unsupported_merge_mode_is_refused_before_ci(monkeypatch, state, mode):
    install_gate(monkeypatch)
    queue = {"configured": False, "entry": None, "auto_merge": None}
    queue[mode] = True if mode == "configured" else {"id": "pending"}
    monkeypatch.setattr(merge_pr, "pull_request", lambda _n: ready_pr(mergeStateStatus=state))
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_a: queue)
    monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: pytest.fail("unsupported admission"))
    with pytest.raises(merge_pr.KernelError, match="unsupported; no merge submitted"):
        merge_pr.merge(10, HEAD)


def issue_record(touches: str):
    return dict(
        body=f"## Acceptance Criteria\n\n- [x] exact behavior is verified\n\n### touches:\n{touches}\n",
        state="OPEN", labels=[{"name": "status:in-review"}, {"name": "agent:codex-1"}],
    )


def test_issue_gate_checks_actual_paths_with_canonical_touches_parser(monkeypatch):
    monkeypatch.setattr(merge_state, "issue", lambda _n: issue_record("src/example.py, tests/**"))
    evidence = merge_pr.issue_gate([7], ["src/example.py", "tests/test_example.py"])
    assert evidence == [dict(
        issue=7, criteria=1, acceptance=[{"done": True, "text": "exact behavior is verified"}],
        touches=["src/example.py", "tests/**"], claimant="codex-1",
        project=board_evidence(), dependencies=[],
    )]


def test_issue_gate_refuses_actual_path_outside_touches(monkeypatch):
    monkeypatch.setattr(merge_state, "issue", lambda _n: issue_record("src/example.py"))
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
        pr = dict(number=10, headRefOid=HEAD, baseRefOid=BASE,
                  mergeQueue={"id": "queue"}, mergeQueueEntry={"id": "entry", "state": "QUEUED"},
                  autoMergeRequest=None)
        return {"data": {"repository": {"pullRequest": pr}}}

    monkeypatch.setattr(merge_state, "gh_json", query)
    snapshot = merge_pr.merge_queue_snapshot(10, HEAD)
    assert snapshot["configured"] is True
    assert snapshot["entry"]["state"] == "QUEUED"
    assert seen["auth"] == merge_state.REPOSITORY_AUTH
    assert seen["argv"][:2] == ["api", "graphql"]


def test_merge_stops_when_base_changes_after_evaluation(monkeypatch):
    snapshots = iter(dict(base_sha=base, issues=[{"issue": 7}], merge_queue=False,
                          queue_entry=None, auto_merge=None) for base in (BASE, "c" * 40))
    patch_gate(monkeypatch, evaluate=lambda *_a: next(snapshots),
               run=lambda _a: pytest.fail("merge command must not run"))
    with pytest.raises(merge_pr.KernelError, match="authority changed"):
        merge_pr.merge(10, HEAD)


@pytest.mark.parametrize("mutation", [
    "stable", "deleted-branch", "base-moved", "queue", "feedback", "changes-requested", "stale-approval",
])
def test_finalize_association_disappears_after_direct_merge(monkeypatch, mutation):
    import check_ci
    from test_ci_and_feedback import historical_world
    world, calls = historical_world(monkeypatch)
    pr = world['pr']
    pr.update(body="Closes #7", reviewDecision=None, author={'login': 'writer'})
    closed, reviews = [], [approval()]
    install_gate(monkeypatch)
    patch_gate(monkeypatch, pull_reviews=lambda _n: reviews)
    monkeypatch.setattr(merge_pr, "pull_request", lambda _n: pr)
    monkeypatch.setattr(merge_pr, "ci_verdict", check_ci.ci_verdict)
    monkeypatch.setattr(merge_pr, "close_out", lambda numbers, _paths: closed.extend(numbers) or [{'issue': 7}])
    pr['state'] = 'OPEN'
    assert check_ci.ci_verdict(3)['state'] == 'success'
    pr['state'] = 'MERGED'
    world['run']['pull_requests'] = []
    if mutation == 'base-moved':
        pr['baseRefOid'] = 'd' * 40  # Never a historical merge parent.
    if mutation == 'deleted-branch':
        pr['headRef'] = None  # No live branch read is permitted by this fixture.
    if mutation == 'queue':
        world['history']['nodes'] = [{'__typename': 'AddedToMergeQueueEvent'}]
    if mutation == 'feedback':
        monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _n: [{'id': 1}])
    if mutation == 'changes-requested':
        pr['reviewDecision'] = 'CHANGES_REQUESTED'
    if mutation == 'stale-approval':
        reviews[0]['commit_id'] = 'd' * 40
    if mutation in {'queue', 'feedback', 'changes-requested', 'stale-approval'}:
        with pytest.raises(merge_pr.KernelError):
            merge_pr.finalize_queued(3, HEAD)
        assert closed == []
    else:
        result = merge_pr.finalize_queued(3, HEAD)
        assert result['finalized'] and result['issues'] == closed == [7]
    assert not any('/git/ref' in str(call) for call in calls)


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
    assert gates == [([7], ["src/app.py"], {"allow_closed": True, "allow_done": True})] * 2


@pytest.mark.parametrize(
    "mutation,refused",
    [
        ("linked-issue", True), ("missing-link", True), ("duplicate-link", True),
        ("touches-excluded", True), ("touches-expanded", True),
        ("acceptance-unchecked", True), ("acceptance-text", True),
        ("claimant", True), ("closed-issue", True), ("lifecycle", True),
        ("head", True), ("base", True), ("draft", True),
        ("review-changes", True), ("review-approved", False), ("approval-stale", True),
        ("unreadable-pr", True), ("unreadable-issue", True),
        ("pr-description", False), ("issue-description", False), ("closing-verb", False),
    ],
)
@pytest.mark.parametrize("during_ci_read", [1, 2])
def test_semantic_drift_never_reaches_merge_command(
    monkeypatch, mutation, refused, during_ci_read
):
    import subprocess
    from copy import deepcopy

    # Run real merge/evaluate/issue_gate. Only external reads and the command
    # boundary are fakes; any accidental subprocess (including GitHub) fails.
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    pr = ready_pr()
    record = issue_record("src/example.py")
    reviews = [approval()]
    events, commands = [], []

    def read_pr(_number):
        events.append("pr")
        if mutation == "unreadable-pr" and events.count("ci") >= during_ci_read:
            raise merge_pr.KernelError("PR is unavailable")
        return deepcopy(pr)

    def read_issue(number):
        events.append("issue")
        if mutation == "unreadable-issue" and events.count("ci") >= during_ci_read:
            raise merge_pr.KernelError("issue is unavailable")
        return deepcopy(record if number == 7 else issue_record("other.py"))

    def read_ci(_number):
        events.append("ci")
        if events.count("ci") == during_ci_read:
            if mutation in {"linked-issue", "missing-link", "duplicate-link"}:
                pr["body"] = {
                    "linked-issue": "Closes #999", "missing-link": "No directive",
                    "duplicate-link": "Closes #7\nCloses #7",
                }[mutation]
            elif mutation.startswith("touches-"):
                replacement = "other.py" if mutation == "touches-excluded" else "src/**"
                record["body"] = record["body"].replace("src/example.py", replacement)
            elif mutation == "acceptance-unchecked":
                record["body"] = record["body"].replace("[x]", "[ ]")
            elif mutation == "acceptance-text":
                record["body"] = record["body"].replace("exact behavior", "different behavior")
            elif mutation == "claimant":
                record["labels"][1]["name"] = "agent:other-writer"
            elif mutation == "closed-issue":
                record["state"] = "CLOSED"
            elif mutation == "lifecycle":
                record["labels"][0]["name"] = "status:in-progress"
            elif mutation in {"head", "base"}:
                pr["headRefOid" if mutation == "head" else "baseRefOid"] = "c" * 40
            elif mutation == "draft":
                pr["isDraft"] = True
            elif mutation in {"review-changes", "review-approved"}:
                pr["reviewDecision"] = (
                    "CHANGES_REQUESTED" if mutation == "review-changes" else "APPROVED"
                )
            elif mutation == "approval-stale":
                reviews[0]["commit_id"] = "c" * 40
            elif mutation == "pr-description":
                pr["body"] += "\n\n## Evidence\nMore test details."
            elif mutation == "issue-description":
                record["body"] += "\n## Evidence\nMore test details."
            elif mutation == "closing-verb":
                pr["body"] = "Fixes #7"
        return {"head": HEAD, "state": "success", "checks": ["aru-governed-pr"]}

    def submit(argv):
        assert events[-3:] == ["pr", "issue", "queue"]
        commands.append(argv)
        pr.update(state="MERGED", mergedAt="2026-09-05T12:00:00Z")

    monkeypatch.setattr(merge_pr, "pull_request", read_pr)
    monkeypatch.setattr(merge_state, "issue", read_issue)
    monkeypatch.setattr(merge_pr, "pull_changed_paths", lambda _n: ["src/example.py"])
    monkeypatch.setattr(merge_pr, "ci_verdict", read_ci)
    monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _n: [])
    monkeypatch.setattr(merge_pr, "pull_reviews", lambda _n: deepcopy(reviews))
    monkeypatch.setattr(
        merge_pr, "merge_queue_snapshot",
        lambda *_a: events.append("queue")
        or {"configured": False, "entry": None, "auto_merge": None},
    )
    monkeypatch.setattr(merge_pr, "run", submit)
    monkeypatch.setattr(merge_pr, "finalize_queued", lambda *_a: {"merged": True})
    if refused:
        with pytest.raises(merge_pr.KernelError):
            merge_pr.merge(10, HEAD)
        assert commands == []
    else:
        assert merge_pr.merge(10, HEAD)["merged"] is True
        assert commands == [[
            "gh", "pr", "merge", "10", "--merge",
            "--match-head-commit", HEAD,
        ]]
        assert events.count("ci") == 2
        assert events.count("issue") == 3
    assert events.count("ci") <= 2
    assert events.count("issue") <= 3
    if mutation == "review-changes":
        assert events == ["pr", "queue", "issue", "ci"] * 2 + (
            ["pr"] if during_ci_read == 2 else []
        )
    elif mutation == "review-approved":
        assert events == ["pr", "queue", "issue", "ci"] * 2 + ["pr", "issue", "queue", "pr"]


@pytest.mark.parametrize("mutation", [
    "entry", "auto", "configuration", "unreadable", "malformed", "missing", "head", "base", "stable",
])
def test_final_pending_request_reread(monkeypatch, mutation):
    configured = False
    import subprocess
    from copy import deepcopy

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    install_gate(monkeypatch)
    monkeypatch.setattr(merge_pr, "pull_request", merge_state.pull_request)
    monkeypatch.setattr(merge_pr, "issue_gate", merge_state.issue_gate)
    monkeypatch.setattr(merge_pr, "merge_queue_snapshot", merge_state.merge_queue_snapshot)
    monkeypatch.setattr(merge_state, "repo_slug", lambda: "owner/repo")
    events, commands = [], []
    pr = ready_pr()
    queue = dict(number=10, headRefOid=HEAD, baseRefOid=BASE,
                 mergeQueue={"id": "queue"} if configured else None,
                 mergeQueueEntry=None, autoMergeRequest=None)

    def read(argv, **kwargs):
        if argv[:2] == ["pr", "view"]:
            events.append("pr")
            return deepcopy(pr)
        assert argv[:2] == ["api", "graphql"]
        assert "mergeQueueEntry{id state}" in argv[3]
        assert "autoMergeRequest{enabledAt}" in argv[3]
        assert kwargs["auth"] == merge_state.REPOSITORY_AUTH
        events.append("queue")
        if mutation == "unreadable" and events.count("ci") == 2:
            raise merge_pr.KernelError("queue is unavailable")
        return {"data": {"repository": {"pullRequest": deepcopy(queue)}}}

    def read_issue(_number):
        events.append("issue")
        return issue_record("src/example.py")

    def read_ci(_number):
        events.append("ci")
        if events.count("ci") == 2:
            if mutation in {"entry", "malformed"}:
                queue["mergeQueueEntry"] = {
                    "id": "entry", "state": "QUEUED" if mutation == "entry" else "INVALID",
                }
            elif mutation == "auto":
                queue["autoMergeRequest"] = {"enabledAt": "2026-09-05T12:00:00Z"}
            elif mutation == "configuration":
                queue["mergeQueue"] = None if configured else {"id": "queue"}
            elif mutation == "missing":
                del queue["mergeQueue"]
            elif mutation in {"head", "base"}:
                queue["headRefOid" if mutation == "head" else "baseRefOid"] = "c" * 40
        return {"head": HEAD, "state": "success", "checks": ["aru-governed-pr"]}

    def submit(argv):
        commands.append(argv)
        pr.update(state="MERGED", mergedAt="2026-09-05T12:00:00Z")

    monkeypatch.setattr(merge_state, "gh_json", read)
    monkeypatch.setattr(merge_state, "issue", read_issue)
    monkeypatch.setattr(merge_pr, "ci_verdict", read_ci)
    monkeypatch.setattr(merge_pr, "run", submit)
    monkeypatch.setattr(merge_pr, "finalize_queued", lambda *_a: {"merged": True})
    if mutation == "stable":
        assert merge_pr.merge(10, HEAD)["merged"] is True
        assert commands == [[
            "gh", "pr", "merge", "10", *([] if configured else ["--merge"]),
            "--match-head-commit", HEAD,
        ]]
        assert events[-4:] == ["pr", "issue", "queue", "pr"]
    else:
        error = None
        try:
            merge_pr.merge(10, HEAD)
        except merge_pr.KernelError as exc:
            error = exc
        assert commands == [], f"stale command submitted: {commands}"
        assert error is not None
    assert events.count("ci") == 2
    assert events.count("issue") == 3
    assert events.count("queue") == 3


@pytest.mark.parametrize("mutation", ["stable", "dismissed", "stale-head", "changes-requested", "new-thread"])
def test_final_approval_and_threads_are_reread_before_submission(monkeypatch, mutation):
    install_gate(monkeypatch)
    reviews, threads, queue_reads, commands = [approval()], [], [], []

    def read_queue(*_a):
        queue_reads.append(True)
        if len(queue_reads) == 3:  # the final reread, after both gate evaluations
            if mutation == "dismissed":
                reviews.append(approval(id=2, state="DISMISSED"))
            elif mutation == "stale-head":
                reviews[0] = approval(commit_id="c" * 40)
            elif mutation == "changes-requested":
                reviews.append(approval(id=2, state="CHANGES_REQUESTED"))
            elif mutation == "new-thread":
                threads.append({"id": 1})
        return {"configured": False, "entry": None, "auto_merge": None}

    patch_gate(
        monkeypatch, merge_queue_snapshot=read_queue, run=commands.append,
        pull_reviews=lambda _n: list(reviews), fetch_feedback=lambda _n: list(threads),
        pull_request=lambda _n: ready_pr(**({"mergedAt": "2026-09-11T00:00:00Z"} if commands else {})),
        finalize_queued=lambda *_a: {"merged": True},
    )
    if mutation == "stable":
        assert merge_pr.merge(10, HEAD)["merged"] is True
        assert commands == [["gh", "pr", "merge", "10", "--merge", "--match-head-commit", HEAD]]
    else:
        with pytest.raises(merge_pr.KernelError, match="before merge submission"):
            merge_pr.merge(10, HEAD)
        assert commands == []
    assert len(queue_reads) == 3


@pytest.mark.parametrize("decision,reviews,reason", [
    (None, [], "no approval of the exact head"),
    ("CHANGES_REQUESTED", [approval(state="CHANGES_REQUESTED")], "still requests changes"),
    ("APPROVED", [approval()], "PR merge state is BLOCKED"),
])
def test_blocked_merge_state_is_judged_after_the_review_gates(monkeypatch, decision, reviews, reason):
    # GitHub reports BLOCKED while a required approval is missing, so the review reason must surface first.
    install_gate(monkeypatch)
    patch_gate(monkeypatch, pull_reviews=lambda _n: reviews,
               pull_request=lambda _n: ready_pr(mergeStateStatus="BLOCKED", reviewDecision=decision))
    with pytest.raises(merge_pr.KernelError, match=reason):
        merge_pr.evaluate(10, HEAD)


def test_blocked_merge_state_is_admissible_with_the_merge_authority_gate(monkeypatch):
    install_gate(monkeypatch)
    patch_gate(monkeypatch, pull_request=lambda _n: ready_pr(mergeStateStatus="BLOCKED"))
    monkeypatch.setattr(merge_pr.merge_authority, "configured", lambda: True)
    assert merge_pr.evaluate(10, HEAD)["approved"] is True
