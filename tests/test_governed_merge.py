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
            "acceptance": [{"done": True, "text": "exact behavior is verified"}],
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
    install_low_risk_gate(monkeypatch)
    monkeypatch.setattr(
        merge_pr,
        "evaluate",
        lambda *_args: {
            "head": HEAD,
            "base_sha": BASE,
            "base": "main",
            "changed_paths": ["src/example.py"],
            "risk_tier": 1,
            "issues": [{"issue": 7, "criteria": 1}],
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
            "entry": {"id": "entry", "state": "QUEUED"} if calls else None,
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


@pytest.mark.parametrize(
    "mutation,refused",
    [
        ("linked-issue", True), ("missing-link", True), ("duplicate-link", True),
        ("touches-excluded", True), ("touches-expanded", True),
        ("acceptance-unchecked", True), ("acceptance-text", True),
        ("claimant", True), ("closed-issue", True), ("lifecycle", True),
        ("head", True), ("base", True), ("draft", True),
        ("review-changes", True), ("review-approved", False),
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
            "gh", "pr", "merge", "10", "--merge", "--delete-branch",
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


@pytest.mark.parametrize("configured,mutation", [
    (True, "entry"), (True, "auto"), (False, "auto"),
    (False, "configuration"), (True, "configuration"),
    (True, "unreadable"), (True, "malformed"),
    (True, "head"), (True, "base"),
    (False, "stable"), (True, "stable"),
])
def test_final_pending_request_reread(monkeypatch, configured, mutation):
    import subprocess
    from copy import deepcopy

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    install_low_risk_gate(monkeypatch)
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
            "--delete-branch", "--match-head-commit", HEAD,
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


def review_world(form):
    """Mutable fake GitHub records; validators consume copies through real helpers."""
    import json
    from copy import deepcopy

    service = form.split('/')[0]
    actor = {'sourcery': 'sourcery-ai', 'codeant': 'codeant-ai',
             'coderabbit': 'coderabbitai', 'claude-code': 'independent-reviewer'}[service]
    t0, t1 = '2026-09-05T08:00:00Z', '2026-09-05T08:30:00Z'
    pr = ready_pr(createdAt=t0, author={'login': 'writer'},
                  labels=[{'name': 'review:' + service}])
    review = dict(id=1, user={'login': actor, 'type': 'Bot'}, commit_id=HEAD,
                  state='APPROVED', submitted_at=t1, body='Review completed.')
    world = dict(pr=pr, reviews=[review], comments=[], checks=[], threads=[],
                 assignments=[dict(event='labeled', label={'name': 'review:' + service},
                                   created_at=t0)])
    if form.endswith('/check'):
        review['state'] = 'APPROVED' if service == 'coderabbit' else 'COMMENTED'
        check = dict(id=2, name={'sourcery': 'Sourcery review', 'codeant': 'CodeAnt',
                               'coderabbit': 'CodeRabbit'}[service], head_sha=HEAD,
                     status='completed', conclusion='success', completed_at=t1,
                     app={'slug': actor})
        world['checks'] = [check]
        pr['statusCheckRollup'] = [deepcopy(check)]
    elif form.endswith('/status'):
        review['state'] = 'COMMENTED'
        payload = [dict(label='Reviewed your PR', commit=HEAD, started=t0,
                        finished=t1, done=True)]
        world['comments'] = [dict(id=3, user=review['user'], updated_at=t1,
                                 body='<!-- codeant-review-status:' + json.dumps(payload) + ' -->')]
    elif service == 'claude-code':
        pr['labels'] += [{'name': name} for name in (
            'reviewer:independent-agent', 'reviewer-actor:' + actor,
            'author:writer-agent', 'author-family:openai-codex')]
        payload = dict(head=HEAD, reviewer='independent-agent', family=service,
                       submitted_by=actor, verdict='APPROVE', issues=[7],
                       summary='Reviewed the exact diff, issue criteria and surrounding failure paths.',
                       verification=['Isolated merge regression tests passed.'],
                       findings=[dict(severity='low', file='scripts/merge_pr.py', line=1,
                                      summary='The earlier review finding has been resolved.',
                                      resolved=True)], acceptance_criteria_reviewed=True,
                       diff_reviewed=True, surrounding_code_reviewed=True)
        review['body'] = '<!-- aru-coding-review:v1 ' + json.dumps(payload) + ' -->'
    return world


def install_review_boundary(monkeypatch, world, mutate, *, boundary='pr', tier=2):
    import socket
    import subprocess
    from copy import deepcopy
    import fetch_pr_feedback

    def blocked(*_a, **_kw):
        pytest.fail('unexpected subprocess/network call')

    monkeypatch.setattr(subprocess, 'run', blocked)
    monkeypatch.setattr(subprocess, 'Popen', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    events, commands = [], []

    def read(kind, value):
        events.append(kind)
        if kind == boundary and events.count(kind) == (2 if boundary == 'ci' else 3):
            mutate(world)
        if world.get('unreadable') == kind:
            raise merge_pr.KernelError(kind + ' evidence is unreadable')
        return deepcopy(value() if callable(value) else value)

    monkeypatch.setattr(merge_pr, 'pull_request', lambda _n: read('pr', world['pr']))
    monkeypatch.setattr(merge_state, 'issue', lambda _n: read('issue', issue_record('scripts/merge_pr.py')))
    monkeypatch.setattr(merge_pr, 'pull_changed_paths', lambda _n: ['scripts/merge_pr.py'])
    monkeypatch.setattr(merge_pr, 'review_risk_tier', lambda _p: tier)
    monkeypatch.setattr(merge_pr, 'ci_verdict', lambda _n: read(
        'ci', dict(head=HEAD, state='success', checks=['aru-governed-pr'])))
    monkeypatch.setattr(merge_pr, 'merge_queue_snapshot', lambda *_a: read(
        'queue', dict(configured=False, entry=None, auto_merge=None)))
    for function, kind in [('pull_reviews', 'reviews'), ('pull_comments', 'comments'),
                           ('pull_events', 'assignments'), ('pull_review_checks', 'checks')]:
        monkeypatch.setattr(merge_pr, function, lambda _n, k=kind: read(k, world[k]))
    monkeypatch.setattr(fetch_pr_feedback, 'repo_slug', lambda: 'owner/repo')
    monkeypatch.setattr(fetch_pr_feedback, 'gh_json', lambda *_a, **_kw: read('threads', {
        'data': {'repository': {'pullRequest': {'reviewThreads': {
            'nodes': world['threads'], 'pageInfo': {'hasNextPage': False}}}}}}))
    monkeypatch.setattr(merge_pr, 'fetch_feedback', fetch_pr_feedback.fetch_feedback)

    def submit(argv):
        commands.append(argv)
        # Deliberately stop at the fake command boundary; never simulate a server merge.
        raise RuntimeError('command spy reached')

    monkeypatch.setattr(merge_pr, 'run', submit)
    return events, commands


def change_review(world, mutation):
    if mutation == 'stable':
        return
    if mutation.startswith('unreadable-'):
        world['unreadable'] = mutation.removeprefix('unreadable-')
    elif mutation == 'dismissed':
        world['reviews'][0]['state'] = 'DISMISSED'
        world['pr']['reviewDecision'] = None
    elif mutation == 'missing':
        world['reviews'].clear()
    elif mutation in {'revoked', 'pending', 'ambiguous', 'check-body', 'stale-head'}:
        check = world['checks'][0]
        if mutation in {'revoked', 'pending', 'stale-head'}:
            world['reviews'].clear()
        if mutation == 'ambiguous':
            world['checks'].append(dict(check, id=99))
        else:
            check.update({'revoked': {'conclusion': 'failure'},
                          'pending': {'status': 'in_progress', 'conclusion': None},
                          'check-body': {'output': {'summary': 'rate limit exceeded'}},
                          'stale-head': {'head_sha': 'c' * 40}}[mutation])
    elif mutation in {'status-body', 'coding-body', 'malformed-body', 'review-body'}:
        record = world['comments'][0] if mutation == 'status-body' else world['reviews'][0]
        record['body'] = (record['body'].replace('true', 'false') if mutation.endswith('-body')
                          and mutation in {'status-body', 'coding-body'} else
                          '<!-- aru-coding-review:v1 broken -->' if mutation == 'malformed-body'
                          else 'rate limit exceeded')
    elif mutation in {'assignment-reset', 'assignment-malformed'}:
        world['assignments'][0]['created_at'] = (
            '2026-09-05T09:00:00Z' if mutation == 'assignment-reset' else 'invalid')
    elif mutation in {'authority', 'multiple-authorities', 'actor', 'author', 'author-identity'}:
        labels = world['pr']['labels']
        if mutation == 'authority':
            labels[0]['name'] = 'review:sourcery'
        elif mutation == 'multiple-authorities':
            labels.append({'name': 'review:coderabbit'})
        elif mutation == 'actor':
            labels[2]['name'] = 'reviewer-actor:different-actor'
        elif mutation == 'author':
            world['pr']['author']['login'] = 'independent-reviewer'
        else:
            labels[3]['name'] = 'author:independent-agent'
    elif mutation == 'changes-requested':
        world['reviews'][0]['state'] = 'CHANGES_REQUESTED'
    elif mutation in {'new-thread', 'reopened-thread'}:
        thread = dict(isResolved=False, isOutdated=False, path='scripts/merge_pr.py', line=1,
                      comments={'nodes': [{'body': 'Blocking finding', 'author': {'login': 'sourcery-ai'}}],
                                'pageInfo': {'hasNextPage': False}})
        if mutation == 'reopened-thread':
            world['threads'][0]['isResolved'] = False
        else:
            world['threads'].append(thread)


REVIEW_FORMS = ['coderabbit/approval', 'coderabbit/check', 'claude-code/attestation']


@pytest.mark.parametrize('form,mutation', [
    *((form, 'stable') for form in REVIEW_FORMS),
    *(('coderabbit/approval', m) for m in ['dismissed', 'missing', 'review-body',
       'assignment-reset', 'assignment-malformed', 'authority', 'multiple-authorities',
       'changes-requested', 'unreadable-reviews', 'unreadable-comments', 'unreadable-assignments']),
    *(('coderabbit/check', m) for m in ['revoked', 'pending', 'ambiguous', 'check-body',
       'stale-head', 'unreadable-checks', 'new-thread', 'reopened-thread', 'unreadable-threads']),
    *(('claude-code/attestation', m) for m in ['coding-body', 'malformed-body', 'actor',
       'author', 'author-identity', 'dismissed', 'missing']),
])
@pytest.mark.parametrize('boundary', ['pr', 'queue'])
def test_final_review_authorization(monkeypatch, form, mutation, boundary):
    world = review_world(form)
    if mutation == 'dismissed':
        world['pr']['reviewDecision'] = 'APPROVED'
    if mutation == 'reopened-thread':
        change_review(world, 'new-thread')
        world['threads'][0]['isResolved'] = True
    # PR-label drift must be visible in final_pr, while evidence/thread drift
    # also gets injected during the later queue read to prove final ordering.
    if boundary == 'queue' and mutation in {'authority', 'multiple-authorities', 'actor',
                                          'author', 'author-identity'}:
        boundary = 'pr'
    events, commands = install_review_boundary(
        monkeypatch, world, lambda w: change_review(w, mutation), boundary=boundary)
    if mutation == 'stable':
        with pytest.raises(RuntimeError, match='command spy reached'):
            merge_pr.merge(10, HEAD)
        assert len(commands) == 1 and commands[0][-2:] == ['--match-head-commit', HEAD]
    else:
        with pytest.raises(merge_pr.KernelError) as exc:
            merge_pr.merge(10, HEAD)
        assert commands == []
        if mutation == 'ambiguous':
            assert str(exc.value) == 'external reviewer returned ambiguous checks'
        if mutation.startswith('unreadable-'):
            assert str(exc.value) == mutation.removeprefix('unreadable-') + ' evidence is unreadable'
    assert events.count('ci') == 2
    assert events.count('pr') == 3
    assert events.count('issue') == events.count('queue') == 3


@pytest.mark.parametrize('form', ['sourcery/approval', 'codeant/approval', 'codeant/status'])
def test_retired_provider_evidence_cannot_authorize_new_merge(monkeypatch, form):
    world = review_world(form)
    _, commands = install_review_boundary(monkeypatch, world, lambda _w: None)
    with pytest.raises(merge_pr.KernelError, match='retired review authority'):
        merge_pr.merge(10, HEAD)
    assert commands == []
