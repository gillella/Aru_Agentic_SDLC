from __future__ import annotations

import hashlib
import json
import signal
import sys
import zipfile
from types import SimpleNamespace

import pytest

from test_trusted_merge import HEAD, ROOT, bundle, kernel, request


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
        request.execute_request(merge_pr, {"issues": [821]}, 825, HEAD, False)




@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("stage", ["post", "await"])
def test_catchable_interruption_cancels_authorization_and_restores_handlers(monkeypatch, signum, stage):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    reads, posts = [], []
    handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

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
        request.execute_request(merge_pr, {"issues": [821]}, 825, HEAD, False)
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
        request.execute_request(helper, {"issues": [821]}, 825, HEAD, False)
    assert posts == [(HEAD, "success"), (HEAD, "failure")]
    assert helper.merge_authority.post is post


@pytest.mark.parametrize("edited", ["2026-10-11T03:00:00Z", "2026-10-11T03:00:01Z", "invalid"])
def test_new_process_retry_refuses_a_body_edited_at_or_after_merge(edited):
    record = dict(kernel().pull_request(825), state="MERGED", mergedAt="2026-10-11T03:00:00Z", body="Closes #822")
    helper = kernel(record)
    metadata = {key: record[key] for key in ("number", "state", "headRefOid", "body", "mergedAt")}
    metadata["lastEditedAt"] = edited
    helper.run = lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": metadata}}}))
    with pytest.raises(request.Refusal, match="merged-body"):
        request.execute_request(helper, {"issues": [821, 822]}, 825, HEAD, False)
    assert helper.calls == []


@pytest.mark.parametrize("change", [{"body": "Closes #822"}, {"headRefOid": "b" * 40}, {"mergedAt": None}])
def test_merged_retry_requires_current_matching_github_metadata(change):
    record = dict(kernel().pull_request(825), state="MERGED", mergedAt="2026-10-11T03:00:00Z")
    helper = kernel(record)
    metadata = {key: record[key] for key in ("number", "state", "headRefOid", "body", "mergedAt")}
    metadata.update(lastEditedAt=None, **change)
    helper.run = lambda *_a, **_k: SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": metadata}}}))
    with pytest.raises(request.Refusal, match="merged-body-evidence"):
        request.execute_request(helper, {"issues": [821, 822]}, 825, HEAD, False)
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
