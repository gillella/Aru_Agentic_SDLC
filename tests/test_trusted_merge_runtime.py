from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import stat
import sys
import zipfile
from types import SimpleNamespace

import pytest

from test_trusted_merge import HEAD, ROOT, bundle, config_fixture, installer, kernel, request
from test_trusted_merge import closing_effect_fixture, mock_closing_effects  # noqa: F401

READ_CLOSING_EFFECTS = request.read_closing_effects
SCOPE_CONFIG = {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822, 792]}


def canonical_effect_flow(monkeypatch, mutate=None, at=1):
    """Actual merge/finalization helpers, with only offline GitHub transport."""
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, title="Implement requests", body="Closes #821",
                  reviewDecision="APPROVED")
    posts, submissions, closures, reads = [], [], [], []

    def transport(argv, **kwargs):
        if argv[:3] == ["gh", "api", "graphql"]:
            assert kwargs["auth"] == "repository"
            if "closingIssuesReferences" in argv[4]:
                assert "excludeUserLinked:false" in argv[4]
                assert "commits(first:100)" in argv[4]
                assert kwargs["timeout"] == 30
                reads.append(True)
                live = closing_effect_fixture(record)
                if mutate and len(reads) >= at:
                    mutate(live)
            else:
                live = {key: record[key] for key in
                        ("number", "state", "headRefOid", "body", "mergedAt")}
                live["lastEditedAt"] = None
            return SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": live}}}))
        assert argv == ["gh", "pr", "merge", "825", "--merge", "--match-head-commit", HEAD]
        submissions.append(argv)
        record.update(state="MERGED", mergedAt="2026-10-11T03:00:00Z",
                      mergeCommit={"oid": "c" * 40})
        return SimpleNamespace(stdout="")

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        return len(posts)

    def close(numbers, _paths):
        closures.append(numbers)
        return [{"issue": numbers[0]}]

    monkeypatch.setattr(request, "read_closing_effects", READ_CLOSING_EFFECTS)
    monkeypatch.setattr(merge_pr, "repo_slug", lambda: SCOPE_CONFIG["repository"])
    monkeypatch.setattr(merge_pr, "run", transport)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda numbers, *_a, **_k: [{"issue": numbers[0]}])
    monkeypatch.setattr(merge_pr, "finalization_verdict", lambda _r: {
        "head": HEAD, "state": "success", "checks": ["Verify"]})
    monkeypatch.setattr(merge_pr.merge_authority, "post", post)
    monkeypatch.setattr(merge_pr, "close_out", close)
    return merge_pr, record, posts, submissions, closures, reads


@pytest.mark.parametrize("extra", [
    "Closes: #793", "CLOSES #793", "CLOSES: #793",
    "Resolves #821, resolves #793, resolves octo-org/octo-repo#100",
    "Fixes octo-org/octo-repo#821", "Closes https://github.com/other/repo/issues/793",
    "Closes: #822", "<!-- closes #793 -->", "`fixes #793`", "Closes\n#793",
    "Closes #821\n#793", "Closes #821\nhttps://github.com/other/repo/issues/793",
    "Fixes GH-793", "CLOSES: gh-793", "Resolved (GH-793).", "Closes GH-821",
    "Fixes other/repo#793", "Fixes https://github.com/other/repo/pull/793",
    "Fixes https://github.com/other/repo/issues/793#issuecomment-1",
    "Fixes [target](https://github.com/other/repo/issues/793)",
    "Fixes <https://github.com/other/repo/issues/793>",
    "Fixes GH\n-793", "Closes #821\nGH-793", "Fixes 793", "Fixes: (793)",
    "Fixes &#35;793", "Fixes GH-%37%39%33", "Fixes GH-７９３", "Fixes GH-٧٩٣",
    "Closes #821, GH-793", "Closes #821\n, GH-793", "Fixes\\nGH-793",
])
@pytest.mark.parametrize("field", ["body", "title", "headRefName", "commit", "head-commit", "merge-message"])
def test_additional_closing_effects_never_authorize_submit_or_close(monkeypatch, extra, field):
    helper, record, posts, submissions, closures, _reads = canonical_effect_flow(monkeypatch)
    if field in ("commit", "head-commit", "merge-message"):
        def mutate(live):
            if field == "commit":
                live["commits"]["nodes"].insert(0, {"commit": {"oid": "d" * 40, "message": extra}})
                live["commits"]["totalCount"] = 2
            elif field == "head-commit":
                live["commits"]["nodes"][0]["commit"]["message"] = extra
            else:
                live["mergeCommit"]["message"] += "\n\n" + extra
        helper, record, posts, submissions, closures, _reads = canonical_effect_flow(monkeypatch, mutate)
        if field == "merge-message":
            record.update(state="MERGED", mergedAt="2026-10-11T03:00:00Z", mergeCommit={"oid": "c" * 40})
    else:
        record[field] = "Closes #821\n\n" + extra if field == "body" else extra
    with pytest.raises(request.Refusal, match="authorized-(closing|issue)-scope"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert not posts and not submissions and not closures


@pytest.mark.parametrize("branch", ["Fixes", "feat/requests"])
def test_generated_merge_text_cannot_hide_keyword_or_target_across_fields(monkeypatch, branch):
    helper, record, posts, submissions, closures, reads = canonical_effect_flow(monkeypatch)
    record.update(title="GH-793", headRefName=branch)
    with pytest.raises(request.Refusal, match="authorized-closing-scope"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert len(reads) == 1 and not posts and not submissions and not closures


def corrupt_effect_evidence(live, defect):
    if defect in ("extra-link", "allowed-extra-link", "foreign-link", "missing-link"):
        targets = live["closingIssuesReferences"]
        if defect == "missing-link":
            targets["nodes"] = []
        else:
            targets["nodes"].append({"number": 822 if defect == "allowed-extra-link" else 793,
                                    "repository": {"nameWithOwner": "other/repo" if defect == "foreign-link"
                                                   else SCOPE_CONFIG["repository"]}})
        targets["totalCount"] = len(targets["nodes"])
    elif defect in ("body", "title", "state", "baseRefOid", "headRefOid", "headRefName", "number"):
        live[defect] = "drift"
    elif defect in ("repository", "headRepository"):
        live[defect] = {"nameWithOwner": "other/repo"}
    elif defect.startswith("commits-") or defect.startswith("links-"):
        connection = live["commits" if defect.startswith("commits-") else "closingIssuesReferences"]
        kind = defect.split("-")[1]
        if kind == "page":
            connection["pageInfo"]["hasNextPage"] = True
        elif kind == "boolean":
            connection["pageInfo"]["hasNextPage"] = 0
        elif kind == "count":
            connection["totalCount"] += 1
        elif kind == "overflow":
            connection["totalCount"] = 101
        elif kind == "null":
            connection["nodes"][0] = None
    elif defect == "message":
        live["commits"]["nodes"][0]["commit"]["message"] = None
    elif defect == "missing-head":
        live["commits"]["nodes"][0]["commit"]["oid"] = "d" * 40
    elif defect == "duplicate":
        live["commits"]["nodes"] *= 2
        live["commits"]["totalCount"] = 2
    elif defect == "absent":
        live.clear()


@pytest.mark.parametrize("defect", [
    "extra-link", "allowed-extra-link", "foreign-link", "missing-link", "body", "title", "state",
    "baseRefOid", "headRefOid", "headRefName", "number", "repository", "headRepository", "message", "missing-head",
    "duplicate", "absent", *[prefix + suffix for prefix in ("commits-", "links-")
                             for suffix in ("page", "boolean", "count", "overflow", "null")],
])
def test_incomplete_or_conflicting_effect_proof_refuses_before_authority(monkeypatch, defect):
    helper, _record, posts, submissions, closures, _reads = canonical_effect_flow(
        monkeypatch, lambda live: corrupt_effect_evidence(live, defect))
    with pytest.raises(request.Refusal, match="closing-(effect-evidence|scope)"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert not posts and not submissions and not closures


@pytest.mark.parametrize("failure", ["transport", "json", "errors", "null"])
def test_unreadable_effect_lookup_refuses_without_fallback(monkeypatch, failure):
    helper, _record, posts, submissions, closures, _reads = canonical_effect_flow(monkeypatch)
    def unreadable(*_a, **_k):
        if failure == "transport":
            raise helper.KernelError("unreadable private diagnostic")
        return SimpleNamespace(stdout={"json": "broken", "errors": '{"data":{},"errors":[{}]}',
                                       "null": '{"data":{"repository":null}}'}[failure])
    monkeypatch.setattr(helper, "run", unreadable)
    with pytest.raises(request.Refusal, match="closing-effect-evidence"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert not posts and not submissions and not closures


@pytest.mark.parametrize("at", range(2, 11))
@pytest.mark.parametrize("source", ["linked", "commit", "gh-commit"])
def test_effect_scope_drift_on_each_reread_and_action_boundary_refuses(monkeypatch, at, source):
    def mutate(live):
        if source == "linked":
            corrupt_effect_evidence(live, "extra-link")
        else:
            live["commits"]["nodes"][0]["commit"]["message"] = (
                "Fixes GH-793" if source == "gh-commit" else "CLOSES: #793")
    helper, _record, posts, submissions, closures, reads = canonical_effect_flow(monkeypatch, mutate, at)
    originals = (helper.pull_request, helper.run, helper.issue_gate, helper.close_out, helper.merge_authority.post)
    with pytest.raises(request.Refusal, match="authorized-closing-scope"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert len(reads) == at and not closures
    if at <= 5:  # Initial evaluation, final reread, or immediately before authorization.
        assert not posts and not submissions
    elif at <= 7:  # await_unblocked or immediately before the merge command.
        assert posts == [(HEAD, "success"), (HEAD, "failure")] and not submissions
    else:  # Observed only after GitHub confirmed merge: cannot undo submission.
        assert posts == [(HEAD, "success")] and len(submissions) == 1
    assert originals == (helper.pull_request, helper.run, helper.issue_gate, helper.close_out,
                         helper.merge_authority.post)


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("keyword", ["close", "closes", "closed", "fix", "fixes", "fixed",
                                     "resolve", "resolves", "resolved"])
def test_complete_single_issue_effect_proof_preserves_legitimate_canonical_flow(monkeypatch, check, keyword):
    def mutate(live):
        live["commits"]["nodes"][0]["commit"]["message"] = "fix: protect requests\n\n" + keyword.upper() + "\t#821"
        if live["mergeCommit"] is not None:
            live["mergeCommit"]["message"] += "\n\n" + live["body"]
    helper, record, posts, submissions, closures, reads = canonical_effect_flow(monkeypatch, mutate)
    record.update(title="fix: protect requests", body=(
        "## Implementation\n\nProtect requests (including `request.py`).\n\n"
        "touches: integrations/merge-authority/**\n\nCloses #821"))
    result = request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, check)
    assert result["merged"] is not check
    assert posts == ([] if check else [(HEAD, "success")])
    assert len(submissions) == (0 if check else 1)
    assert closures == ([] if check else [[821]])
    assert len(reads) == (2 if check else 10)


@pytest.mark.parametrize("defect", ["additional", "missing", "mismatch", "message"])
def test_merged_retry_reads_actual_merge_commit_before_any_closeout(monkeypatch, defect):
    def mutate(live):
        if defect == "additional":
            live["mergeCommit"]["message"] += "\nCLOSES: other/repo#793"
        elif defect == "missing":
            live["mergeCommit"] = None
        else:
            live["mergeCommit"]["oid" if defect == "mismatch" else "message"] = None
    helper, record, posts, submissions, closures, _reads = canonical_effect_flow(monkeypatch, mutate)
    record.update(state="MERGED", mergedAt="2026-10-11T03:00:00Z", mergeCommit={"oid": "c" * 40})
    with pytest.raises(request.Refusal, match="closing-(scope|effect-evidence)"):
        request.execute_request(helper, SCOPE_CONFIG, 825, HEAD, False)
    assert not posts and not submissions and not closures


@pytest.mark.parametrize("failure", ["ci", "feedback", "approval", "queue", "auto", "criteria",
                                     "claim", "dependency", "touches", "project", "base"])
def test_actual_canonical_admission_refuses_without_app_post_or_merge(monkeypatch, failure):
    import merge_pr
    import merge_state
    from test_merge_gate import install_happy_gate

    pr = install_happy_gate(monkeypatch)
    pr.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    issue = {"state": "OPEN", "labels": [{"name": "agent:owner"}, {"name": "status:in-review"}],
             "body": "## Acceptance Criteria\n\n- [x] Accepted\n\ntouches: scripts/merge_pr.py\n"}
    monkeypatch.setattr(merge_state, "issue", lambda n: issue if n == 821 else {"state": "OPEN"})
    monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: {"status": "In Review"})
    monkeypatch.setattr(merge_pr, "issue_gate", merge_state.issue_gate)
    monkeypatch.setattr(merge_pr.merge_authority, "post", lambda *_a: pytest.fail("refusal posted authorization"))
    monkeypatch.setattr(merge_pr, "run", lambda *_a: pytest.fail("refusal submitted merge"))
    if failure == "ci":
        monkeypatch.setattr(merge_pr, "ci_verdict", lambda _n: {"state": "pending", "head": HEAD})
    elif failure == "feedback":
        monkeypatch.setattr(merge_pr, "fetch_feedback", lambda _n: [{"body": "fix"}])
    elif failure == "approval":
        monkeypatch.setattr(merge_pr, "pull_reviews", lambda _n: [])
    elif failure in ("queue", "auto"):
        monkeypatch.setattr(merge_pr, "merge_queue_snapshot", lambda *_a: {
            "configured": failure == "queue", "entry": None, "auto_merge": {} if failure == "auto" else None,
        })
    elif failure == "criteria":
        issue["body"] = issue["body"].replace("[x]", "[ ]")
    elif failure == "claim":
        issue["labels"].append({"name": "agent:peer"})
    elif failure == "dependency":
        issue["body"] += "\ndepends-on: #818\n"
    elif failure == "touches":
        issue["body"] = issue["body"].replace("scripts/merge_pr.py", "README.md")
    elif failure == "project":
        monkeypatch.setattr(merge_state, "project_item_evidence", lambda _n: {"status": "Done"})
    elif failure == "base":
        pr["mergeStateStatus"] = "BEHIND"
    expected = {
        "ci": "exact-current-head required GitHub checks", "feedback": "unresolved",
        "approval": "approval of the exact head", "queue": "merge queues", "auto": "auto-merge",
        "criteria": "incomplete Acceptance Criteria", "claim": "exclusive claimant",
        "dependency": "dependency #818", "touches": "outside the linked issue touches",
        "project": "labels and linked Project", "base": "BEHIND",
    }
    with pytest.raises(merge_pr.KernelError, match=expected[failure]):
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)




@pytest.mark.parametrize("signum", sorted(request.catchable_signals()))
@pytest.mark.parametrize("stage", ["post", "await"])
def test_catchable_interruption_cancels_authorization_and_restores_handlers(monkeypatch, signum, stage):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    reads, posts = [], []
    handlers = {number: signal.getsignal(number) for number in request.catchable_signals()}

    def interrupt():
        handler = signal.getsignal(signum)
        assert callable(handler)
        handler(signum, None)

    def pull(_number):
        reads.append(True)
        if stage == "await" and len(reads) == 5:
            interrupt()
        return record

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        if stage == "post" and conclusion == "success":
            interrupt()
        return len(posts)

    monkeypatch.setattr(merge_pr, "pull_request", pull)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda numbers, *_a, **_k: [{"issue": numbers[0], "criteria": 1}])
    monkeypatch.setattr(merge_pr.merge_authority, "post", post)
    monkeypatch.setattr(merge_pr, "run", lambda *_a, **_k: pytest.fail("interrupted request submitted merge"))
    monkeypatch.setattr(merge_pr, "close_out", lambda *_a, **_k: pytest.fail("interrupted request closed issue"))
    with pytest.raises(request.Refusal, match="request-interrupted"):
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert posts == [(HEAD, "success"), (HEAD, "failure")]
    assert merge_pr.pull_request is pull and merge_pr.merge_authority.post is post
    assert all(signal.getsignal(number) is handler for number, handler in handlers.items())


def test_keyboard_interrupt_after_post_is_also_canceled(monkeypatch):
    helper = kernel()
    posts = []
    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        if conclusion == "success":
            raise KeyboardInterrupt
        return 1
    helper.merge_authority.post = post
    helper.merge = lambda *_a, **_k: helper.merge_authority.post(HEAD, "success", "Passed")
    with pytest.raises(KeyboardInterrupt):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert posts == [(HEAD, "success"), (HEAD, "failure")]
    assert helper.merge_authority.post is post


@pytest.mark.parametrize("stage", ["post", "before-submit", "ordinary-error", "ordinary-unwind"])
def test_second_signal_during_unwind_and_compensation_cannot_skip_cancellation(monkeypatch, stage):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    signals = request.catchable_signals()
    old_handlers = {number: signal.getsignal(number) for number in signals}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGWINCH})
    delivered, posts, reads = [], [], []
    signal.signal(signal.SIGUSR1, lambda number, _frame: delivered.append(number))
    signal.signal(signal.SIGUSR2, lambda number, _frame: delivered.append(number))
    handlers = {number: signal.getsignal(number) for number in signals}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    def interrupted_operation():
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            pytest.fail("first interruption returned")
        finally:
            # A second OS signal arrives while the original exception unwinds.
            os.kill(os.getpid(), signal.SIGUSR2)

    def pull(_number):
        reads.append(True)
        if stage == "before-submit" and len(reads) == 5:
            interrupted_operation()
        if stage == "ordinary-unwind" and len(reads) == 5:
            raise OSError("ordinary metadata exception")
        return record

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        if conclusion == "success":
            if stage == "post":
                interrupted_operation()
            if stage == "ordinary-error":
                raise RuntimeError("ordinary adapter exception")
        else:
            assert signals <= signal.pthread_sigmask(signal.SIG_BLOCK, set())
            os.kill(os.getpid(), signal.SIGUSR1)
            os.kill(os.getpid(), signal.SIGUSR2)
        return len(posts)

    monkeypatch.setattr(merge_pr, "pull_request", pull)
    monkeypatch.setattr(merge_pr, "issue_gate", lambda numbers, *_a, **_k: [{"issue": numbers[0], "criteria": 1}])
    monkeypatch.setattr(merge_pr.merge_authority, "post", post)
    monkeypatch.setattr(merge_pr, "run", lambda *_a, **_k: pytest.fail("submitted after interruption"))
    monkeypatch.setattr(merge_pr, "close_out", lambda *_a, **_k: pytest.fail("closed after interruption"))
    try:
        gate = {"ordinary-error": "authority-unavailable", "ordinary-unwind": "ordinary metadata exception"}.get(stage, "request-interrupted")
        with pytest.raises(OSError if stage == "ordinary-unwind" else request.Refusal, match=gate):
            request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
        assert posts == [(HEAD, "success"), (HEAD, "failure")]
        assert merge_pr.pull_request is pull and merge_pr.merge_authority.post is post
        assert all(signal.getsignal(number) is handler for number, handler in handlers.items())
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == mask
        # Pending signals reach the original handlers only after compensation.
        assert signal.SIGUSR1 in delivered and signal.SIGUSR2 in delivered
    finally:
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def test_reentrant_python_delivery_while_first_signal_blocks_does_not_raise_twice(monkeypatch):
    helper = kernel()
    posts, reentered = [], []
    original_mask = signal.pthread_sigmask
    before = original_mask(signal.SIG_BLOCK, set())
    handlers = {number: signal.getsignal(number) for number in request.catchable_signals()}

    def mask(how, numbers):
        # Python may already have queued another handler before SIG_BLOCK takes
        # effect. Simulate its dispatch inside the first handler's mask call.
        if how == signal.SIG_BLOCK and posts == [(HEAD, "success")] and not reentered:
            reentered.append(True)
            signal.getsignal(signal.SIGUSR2)(signal.SIGUSR2, None)
        return original_mask(how, numbers)

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        if conclusion == "success":
            signal.getsignal(signal.SIGUSR1)(signal.SIGUSR1, None)
        return 1

    monkeypatch.setattr(signal, "pthread_sigmask", mask)
    helper.merge_authority.post = post
    helper.merge = lambda *_a, **_k: helper.merge_authority.post(HEAD, "success", "valid")
    with pytest.raises(request.Refusal, match="request-interrupted"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert reentered == [True] and posts == [(HEAD, "success"), (HEAD, "failure")]
    assert original_mask(signal.SIG_BLOCK, set()) == before
    assert all(signal.getsignal(number) is handler for number, handler in handlers.items())


def test_signal_at_ordinary_exception_cleanup_entry_and_failed_compensation_restores_state(monkeypatch):
    helper = kernel()
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    handlers = {number: signal.getsignal(number) for number in request.catchable_signals()}
    original_mask = signal.pthread_sigmask
    posts, cleanup_delivery = [], []

    def mask(how, numbers):
        if how == signal.SIG_BLOCK and posts == ["success"] and not cleanup_delivery:
            cleanup_delivery.append(True)
            signal.getsignal(signal.SIGUSR1)(signal.SIGUSR1, None)
        return original_mask(how, numbers)

    def post(_head, conclusion, _summary):
        posts.append(conclusion)
        if conclusion == "failure":
            assert request.catchable_signals() <= original_mask(signal.SIG_BLOCK, set())
            raise OSError("transport unavailable")

    def merge(*_a, **_k):
        helper.merge_authority.post(HEAD, "success", "valid")
        raise ValueError("ordinary helper exception")

    helper.merge_authority.post, helper.merge = post, merge
    monkeypatch.setattr(signal, "pthread_sigmask", mask)
    with pytest.raises(request.Refusal, match="authorization-revocation-unavailable"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert cleanup_delivery == [True] and posts == ["success", "failure"]
    assert helper.merge_authority.post is post
    assert original_mask(signal.SIG_BLOCK, set()) == before
    assert all(signal.getsignal(number) is handler for number, handler in handlers.items())


def test_first_signal_before_cleanup_flag_cannot_escape_compensation():
    helper = kernel()
    posts = []
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    handlers = {number: signal.getsignal(number) for number in request.catchable_signals()}
    guard = request.cancellation_guard.__wrapped__
    source, first_line = inspect.getsourcelines(guard)
    cleanup_line = first_line + next(i for i, line in enumerate(source) if line.strip() == "cleaning = True")
    interrupted = []
    old_trace = sys.gettrace()

    def trace(frame, event, _arg):
        if frame.f_code is guard.__code__ and event == "line" and frame.f_lineno == cleanup_line and not interrupted:
            interrupted.append(True)
            # Dispatch just before the first cleanup flag assignment, rather
            # than after it inside pthread_sigmask as the other regression does.
            signal.getsignal(signal.SIGUSR1)(signal.SIGUSR1, None)
        return trace

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))

    def merge(*_a, **_k):
        helper.merge_authority.post(HEAD, "success", "valid")
        raise ValueError("ordinary helper exception")

    helper.merge_authority.post, helper.merge = post, merge
    try:
        sys.settrace(trace)
        with pytest.raises(request.Refusal, match="request-interrupted"):
            request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
        sys.settrace(old_trace)
        assert posts == [(HEAD, "success"), (HEAD, "failure")]
        assert helper.merge_authority.post is post
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
        assert all(signal.getsignal(number) is handler for number, handler in handlers.items())
    finally:
        sys.settrace(old_trace)
        for number, handler in handlers.items():
            signal.signal(number, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


@pytest.mark.parametrize("edited", ["2026-10-11T03:00:00Z", "2026-10-11T03:00:01Z", "invalid"])
def test_new_process_retry_refuses_a_body_edited_at_or_after_merge(edited):
    record = dict(kernel().pull_request(825), state="MERGED", mergedAt="2026-10-11T03:00:00Z", body="Closes #822")
    helper = kernel(record)
    metadata = {key: record[key] for key in ("number", "state", "headRefOid", "body", "mergedAt")}
    metadata["lastEditedAt"] = edited
    helper.run = lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": metadata}}}))
    with pytest.raises(request.Refusal, match="merged-body"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)
    assert helper.calls == []


@pytest.mark.parametrize("change", [{"body": "Closes #822"}, {"headRefOid": "b" * 40}, {"mergedAt": None}])
def test_merged_retry_requires_current_matching_github_metadata(change):
    record = dict(kernel().pull_request(825), state="MERGED", mergedAt="2026-10-11T03:00:00Z")
    helper = kernel(record)
    metadata = {key: record[key] for key in ("number", "state", "headRefOid", "body", "mergedAt")}
    metadata.update(lastEditedAt=None, **change)
    helper.run = lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": metadata}}}))
    with pytest.raises(request.Refusal, match="merged-body-evidence"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)
    assert helper.calls == []


def test_operator_package_reproduces_reviewed_objects_and_cli_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bundle, "git_object", lambda _s, _c, path: path.encode())
    binary = b"sealed-gh-binary"
    digest = hashlib.sha256(binary).hexdigest()
    args = (ROOT, HEAD, "b" * 40, binary, "gillella/Aru_Agentic_SDLC", [821, 822, 792])
    monkeypatch.setattr(zipfile.time, "localtime", lambda: (2026, 10, 11, 1, 2, 3, 0, 0, 0))
    raw = bundle.make_bundle(*args, gh_sha256=digest)
    monkeypatch.setattr(zipfile.time, "localtime", lambda: (2027, 1, 2, 4, 5, 6, 0, 0, 0))
    assert bundle.make_bundle(*args, gh_sha256=digest) == raw
    gh_file, package = tmp_path / "gh", tmp_path / "package.zip"
    gh_file.write_bytes(binary)
    package.write_bytes(raw)
    argv = ["bundle.py", "--source", str(ROOT), "--revision", HEAD, "--helper", "b" * 40,
            "--gh", str(gh_file), "--gh-sha256", digest, "--repository", "gillella/Aru_Agentic_SDLC",
            "--issue", "821", "--issue", "822", "--issue", "792", "--verify", str(package)]
    monkeypatch.setattr(sys, "argv", argv)
    bundle.main()
    assert json.loads(capsys.readouterr().out)["verified"] is True
    monkeypatch.setattr(bundle, "git_object", lambda _s, _c, path: (path + "tampered").encode())
    with pytest.raises(ValueError, match="does not reproduce"):
        bundle.main()
    with pytest.raises(ValueError, match="independently reviewed digest"):
        bundle.make_bundle(*args, gh_sha256="0" * 64)


@pytest.mark.parametrize("limit", list(request.LIMITS))
@pytest.mark.parametrize("side", [0, 1])
def test_caller_controlled_resource_limits_refuse_before_authority(monkeypatch, limit, side):
    approved = {getattr(request.resource, "RLIMIT_" + name.upper()):
                (request.resource.RLIM_INFINITY if value is None else value,) * 2
                for name, value in request.LIMITS.items()}
    monkeypatch.setattr(request.resource, "getrlimit", lambda number: approved[number])
    request.verify_resource_limits()
    number = getattr(request.resource, "RLIMIT_" + limit.upper())
    bad = list(approved[number])
    bad[side] = 1 if approved[number][side] != 1 else 2
    approved[number] = tuple(bad)
    with pytest.raises(request.Refusal, match="unapproved-resource-limits"):
        request.verify_resource_limits()


def test_resource_policy_pins_limits_for_only_the_digest_sealed_request():
    policy = installer.policy_text("gillella", "aru-merge", "a" * 64)
    assert "Defaults!ARU_TRUSTED_MERGE " in policy
    for name, value in request.LIMITS.items():
        assert f"rlimit_{name}={value if value is not None else 'infinity'}" in policy
    assert "Defaults " not in policy and "rlimit_cpu=user" not in policy


def test_installer_permissions_use_open_descriptor_despite_path_replacement(tmp_path, monkeypatch):
    target, moved, victim = (tmp_path / name for name in ("key", "original", "victim"))
    victim.write_bytes(b"preserve")
    victim.chmod(0o644)
    original_mode = victim.stat().st_mode
    checked = []

    def replace_during_chown(fd, _uid, _gid):
        target.rename(moved)
        target.symlink_to(victim)
        checked.append(os.fstat(fd).st_ino == moved.stat().st_ino)

    monkeypatch.setattr(installer.os, "fchown", replace_during_chown)
    monkeypatch.setattr(installer.os, "chown", lambda *_a: pytest.fail("path-based chown"))
    installer.write_file(target, b"fixture-private-key", 0o600, os.getuid(), os.getgid())
    assert checked == [True] and moved.read_bytes() == b"fixture-private-key"
    assert moved.stat().st_mode & 0o777 == 0o600
    assert victim.read_bytes() == b"preserve" and victim.stat().st_mode == original_mode


def test_confirmed_merge_closeout_error_preserved_without_authorization_revocation(monkeypatch):
    record = {"number": 825, "headRefOid": HEAD, "body": "Closes #821",
              "state": "OPEN", "isDraft": False, "reviewDecision": "APPROVED"}
    helper = kernel(record)
    posts = []
    helper.merge_authority.post = lambda _head, conclusion, _summary: posts.append(conclusion)

    def merge(pr, head, **_kwargs):
        helper.merge_authority.post(head, "success", "valid")
        record.update(state="MERGED", mergedAt="2026-10-11T00:00:00Z")
        helper.pull_request(pr)
        raise helper.KernelError("close-out-original-error")

    helper.merge = merge
    with pytest.raises(helper.KernelError, match="close-out-original-error"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert posts == ["success"]


@pytest.mark.parametrize("mask", [0o377, 0o777])
def test_main_normalizes_caller_umask_and_leaves_reopenable_lock(tmp_path, monkeypatch, mask):
    root = tmp_path / "installed"
    root.mkdir()
    (root / "runtime").mkdir(mode=0o700)
    monkeypatch.setattr(request, "ROOT", root)
    monkeypatch.setattr(request, "__file__", str(root / "request.py"))
    monkeypatch.setattr(request.sys, "platform", "darwin")
    monkeypatch.setattr(request.sys, "path", list(request.sys.path))
    monkeypatch.setattr(request, "verify_resource_limits", lambda: None)
    monkeypatch.setattr(request, "verify_installation", lambda _c: None)
    monkeypatch.setattr(request, "protected_bytes", lambda *_a: b"{}")
    monkeypatch.setattr(request, "validate_config", lambda _v: {
        "principal_uid": os.getuid(), "repository": "gillella/Aru_Agentic_SDLC", "issues": [821]})
    monkeypatch.setattr(request, "authority_environment", lambda _c: {})
    monkeypatch.setattr(request.os, "environ", {})
    monkeypatch.setattr(request.os, "chdir", lambda _p: None)
    monkeypatch.setitem(sys.modules, "merge_pr", kernel())
    before = os.umask(mask)
    try:
        for _attempt in range(2):
            assert request.main(["--pr", "825", "--expected-head", HEAD, "--check"]) == 0
            assert (root / "runtime/request.lock").stat().st_mode & 0o777 == 0o600
        assert os.umask(mask) == mask
    finally:
        os.umask(before)


def test_public_refusals_separate_canonical_and_authority_failures_without_raw_data():
    helper = kernel()
    assert request.public_failure(helper.KernelError("unaccepted criteria"), helper) == "kernel-refused"
    assert request.public_failure(helper.KernelError("private diagnostic token"), helper) == "kernel-refused"
    assert request.public_failure(OSError("private credential path"), helper) == "authority-unavailable"
    assert request.public_failure(request.Refusal("request-head"), helper) == "request-head"


def test_private_adapter_failure_has_authority_label_and_attempts_revocation():
    helper = kernel()
    posts = []
    def post(_head, conclusion, _summary):
        posts.append(conclusion)
        if conclusion == "success":
            raise helper.KernelError("private adapter failure")
    helper.merge_authority.post = post
    with pytest.raises(request.Refusal, match="authority-unavailable"):
        with request.cancellation_guard(helper, HEAD, request.scope_refusal(helper)):
            helper.merge_authority.post(HEAD, "success", "valid")
    assert posts == ["success", "failure"]


@pytest.mark.parametrize("evidence", ["absent", "malformed", "array", "version", "extra",
                                      "digest", "mismatched", "mutable", "parent", "owner", "symlink", "valid"])
def test_noncheck_requires_protected_config_bound_enablement_before_authority(tmp_path, monkeypatch, capsys, evidence):
    root = tmp_path / "installed"
    root.mkdir(mode=0o755)
    (root / "runtime").mkdir(mode=0o700)
    monkeypatch.setattr(request, "ROOT", root)
    monkeypatch.setattr(request, "__file__", str(root / "request.py"))
    monkeypatch.setattr(request.sys, "platform", "darwin")
    monkeypatch.setattr(request.sys, "path", list(request.sys.path))
    config = config_fixture()
    config["principal_uid"] = os.getuid()
    raw = json.dumps(config, sort_keys=True).encode()
    (root / "config.json").write_bytes(raw)
    marker = root / "enabled.json"
    enabled = {"version": 1, "config_sha256": hashlib.sha256(raw).hexdigest()}
    if evidence == "version":
        enabled["version"] = True
    elif evidence == "extra":
        enabled["accepted_issue"] = 821
    elif evidence == "digest":
        enabled["config_sha256"] = "not-a-digest"
    elif evidence == "mismatched":
        # Same allowlist but a changed installed App/CLI/helper configuration.
        enabled["config_sha256"] = hashlib.sha256(raw + b"\n").hexdigest()
    if evidence != "absent":
        marker.write_bytes(b"not-json" if evidence == "malformed" else b"[]" if evidence == "array"
                           else json.dumps(enabled).encode())
        marker.chmod(0o666 if evidence == "mutable" else 0o644)
    if evidence == "symlink":
        target = root / "other.json"
        marker.rename(target)
        marker.symlink_to(target)
    original_lstat, original_fstat = request.Path.lstat, os.fstat

    def metadata(path):
        info = original_lstat(path)
        mode = stat.S_IFDIR | 0o755 if stat.S_ISDIR(info.st_mode) else info.st_mode
        if path == root and evidence == "parent":
            mode = stat.S_IFDIR | 0o777
        return SimpleNamespace(st_uid=0, st_mode=mode)

    def file_metadata(fd):
        info = original_fstat(fd)
        if info.st_mode & 0o777 == 0o600:
            return info  # The real ephemeral lock remains owned by the test user.
        return SimpleNamespace(st_uid=os.getuid() if evidence == "owner" and info.st_ino == marker.stat().st_ino else 0,
                               st_mode=info.st_mode, st_nlink=info.st_nlink)

    monkeypatch.setattr(request.Path, "lstat", metadata)
    monkeypatch.setattr(request.os, "fstat", file_metadata)
    # Config loading is independent of marker ancestor denials in this fixture.
    original_bytes = request.protected_bytes
    monkeypatch.setattr(request, "protected_bytes", lambda path, owners, **kwargs:
                        raw if path == root / "config.json" else original_bytes(path, owners, **kwargs))
    monkeypatch.setattr(request, "verify_resource_limits", lambda: None)
    monkeypatch.setattr(request, "verify_installation", lambda _c: None)
    helper = kernel()
    monkeypatch.setitem(sys.modules, "merge_pr", helper)
    authority = []
    monkeypatch.setattr(request, "authority_environment", lambda _c: authority.append("environment") or {})
    monkeypatch.setattr(request.os, "environ", {})
    monkeypatch.setattr(request.os, "chdir", lambda _p: None)
    args = ["--pr", "825", "--expected-head", HEAD]
    if evidence == "valid":
        assert request.main(args) == 0
        assert helper.calls == [("merge", 825, HEAD, False)]
        assert authority == ["environment"]
    else:
        assert request.main(args) == 2
        assert json.loads(capsys.readouterr().out)["gate"] == "submissions-disabled"
        assert not authority and not helper.calls
        assert not (root / "runtime/request.lock").exists()
    # Check-only remains available regardless of enablement evidence.
    helper.calls.clear()
    assert request.main([*args, "--check"]) == 0
    assert helper.calls == [("merge", 825, HEAD, True)]


def test_installer_publishes_only_disabled_request_routing(tmp_path, monkeypatch):
    from test_trusted_merge import package_fixture, principal_account

    root, policy = tmp_path / "installed", tmp_path / "sudoers"
    python = tmp_path / "python"
    python.write_bytes(b"reviewed-interpreter")
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr(installer, "POLICY", policy)
    monkeypatch.setattr(installer, "root_chain", lambda _p: None)
    monkeypatch.setattr(installer, "system_interpreter", lambda: python)
    monkeypatch.setattr(installer.os, "fchown", lambda *_a: None)
    monkeypatch.setattr(installer, "install_credentials", lambda *_a: None)
    validated = []
    monkeypatch.setattr(installer.subprocess, "run", lambda argv, **_k: validated.append(argv))
    manifest, payload = installer.unpack(package_fixture(monkeypatch))
    installer.install_payload(manifest, payload, "gillella", principal_account(), 123, b"fixture-only")
    config = json.loads((root / "config.json").read_bytes())
    assert config["repository"] == "gillella/Aru_Agentic_SDLC"
    assert config["issues"] == [821, 822, 792]
    assert policy.is_file() and validated == [["/usr/sbin/visudo", "-cf", str(policy.with_name(".aru-trusted-merge-candidate"))]]
    assert not (root / "enabled.json").exists()
    assert "--check" not in policy.read_text()  # Both shapes route; request enforces enablement.
