from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "trusted_merge_request", ROOT / "integrations/merge-authority/request.py",
)
request = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(request)
HEAD = "a" * 40


def closing_effect_fixture(record):
    from merge_state import linked_issues
    record.setdefault("title", "Implement trusted requests")
    record.setdefault("baseRefOid", "b" * 40)
    record.setdefault("headRefName", "feat/issue-821")
    if record["state"] == "MERGED":
        record.setdefault("mergeCommit", {"oid": "c" * 40})
    def connection(nodes):
        return {"totalCount": len(nodes), "nodes": nodes, "pageInfo": {"hasNextPage": False}}
    repository = {"nameWithOwner": "gillella/Aru_Agentic_SDLC"}
    return {**{key: record[key] for key in
               ("number", "title", "body", "state", "headRefOid", "baseRefOid", "headRefName")},
            "repository": repository, "headRepository": repository,
            "mergeCommit": None if record["state"] != "MERGED" else {
                "oid": record["mergeCommit"]["oid"],
                "message": f"Merge pull request #825 from gillella/{record['headRefName']}\n\n{record['title']}"},
            "closingIssuesReferences": connection([
                {"number": linked_issues(record["body"])[0], "repository": repository}]),
            "commits": connection([{"commit": {"oid": HEAD, "message": "fix: protect requests"}}])}


@pytest.fixture(autouse=True)
def mock_closing_effects(monkeypatch):
    # Existing merge/signal cases mock the read, while retaining effect validation.
    monkeypatch.setattr(request, "read_closing_effects",
                        lambda _kernel, record, _pr: closing_effect_fixture(record))


@pytest.mark.parametrize("arguments", [
    [], ["--pr", "825"], ["--pr", "0", "--expected-head", HEAD],
    ["--pr", "825;sh", "--expected-head", HEAD],
    ["--pr", "825", "--expected-head", "0" * 40],
    ["--pr", "825", "--expected-head", "A" * 40],
    ["--pr", "825", "--expected-head", HEAD, "--repo=other/repo"],
    ["--pr", "825", "--expected-head", HEAD, "--check", "--admin"],
    ["--pr", "825", "--expected-head", HEAD, "--finalize"],
    ["--expected-head", HEAD, "--pr", "825"],
])
def test_caller_cannot_expand_the_fixed_request(arguments):
    with pytest.raises(request.Refusal):
        request.parse_request(arguments)


class FakeKernelError(RuntimeError):
    """Stands in for the sealed helper's KernelError in fake-kernel tests."""


def kernel(record=None, failure=None):
    calls = []
    if record and record.get("state") == "MERGED":
        record.setdefault("mergedAt", "2026-10-11T03:00:00Z")
    def metadata(*_args, **_kwargs):
        value = record or {}
        live = {key: value.get(key) for key in ("number", "state", "headRefOid", "body", "mergedAt")}
        live["lastEditedAt"] = None
        return SimpleNamespace(stdout=json.dumps({"data": {"repository": {"pullRequest": live}}}))
    def merge(pr, head, dry_run=False):
        calls.append(("merge", pr, head, dry_run))
        if failure:
            raise failure
        return {"merged": not dry_run, "finalized": not dry_run}
    def finalize(pr, head):
        calls.append(("finalize", pr, head))
        return {"merged": True, "finalized": True}
    return SimpleNamespace(
        calls=calls, merge=merge, finalize_queued=finalize, KernelError=FakeKernelError,
        run=metadata, repo_slug=lambda: "gillella/Aru_Agentic_SDLC",
        merge_authority=SimpleNamespace(post=lambda *_a: None),
        issue_gate=lambda numbers, *_args, **_kwargs: [{"issue": numbers[0]}],
        close_out=lambda numbers, *_args, **_kwargs: [{"issue": numbers[0]}],
        linked_issues=lambda body: [int(body.split("#")[1])],
        pull_request=lambda pr: record or {
            "number": pr, "headRefOid": HEAD, "body": "Closes #821",
            "state": "OPEN", "isDraft": False, "reviewDecision": "APPROVED",
        },
    )


def test_trusted_request_delegates_merge_and_check_to_canonical_helper():
    for check in (False, True):
        helper = kernel()
        result = request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, check)
        assert helper.calls == [("merge", 825, HEAD, check)]
        assert result["merged"] is not check
    assert request.parse_request(["--pr", "825", "--expected-head", HEAD, "--check"]) == (825, HEAD, True)


@pytest.mark.parametrize("change,gate", [
    ({"headRefOid": "b" * 40}, "current-head"),
    ({"number": 999}, "current-head"),
    ({"body": "Closes #793"}, "authorized-issue-scope"),
    ({"reviewDecision": "REVIEW_REQUIRED"}, "github-counted-approval"),
    ({"reviewDecision": "CHANGES_REQUESTED"}, "github-counted-approval"),
    ({"isDraft": True}, "open-ready-pr"),
    ({"state": "CLOSED"}, "open-ready-pr"),
])
def test_untrusted_live_authority_never_reaches_submission(change, gate):
    record = kernel().pull_request(825)
    record.update(change)
    helper = kernel(record)
    with pytest.raises(request.Refusal, match=gate):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert helper.calls == []


def test_scope_refusals_are_also_the_sealed_helpers_error_type():
    record = dict(kernel().pull_request(825), reviewDecision="REVIEW_REQUIRED")
    helper = kernel(record)
    with pytest.raises(FakeKernelError) as refused:
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    # The public gate is unchanged while canonical KernelError handlers catch it.
    assert isinstance(refused.value, request.Refusal)
    assert str(refused.value) == "github-counted-approval"
    assert refused.value.__cause__ is None


@pytest.mark.parametrize("contract", [None, object, BaseException, "KernelError", request.Refusal])
def test_helper_without_verified_kernel_error_refuses_before_any_read(contract):
    helper = kernel()
    helper.pull_request = lambda _pr: pytest.fail("read without a verified KernelError")
    if contract is None:
        del helper.KernelError
    else:
        helper.KernelError = contract
    with pytest.raises(request.Refusal, match="kernel-error-contract"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert helper.calls == []


@pytest.mark.parametrize("gate", [
    "exact-head-consumer-verification", "unresolved-findings", "acceptance-criteria",
    "exclusive-claim", "live-touches", "dependencies", "base-head-race",
    "unsupported-merge-queue", "unsupported-auto-merge",
])
def test_request_propagates_canonical_gate_refusals_without_fallback(gate):
    helper = kernel(failure=RuntimeError(gate))
    with pytest.raises(RuntimeError, match=gate):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)
    assert helper.calls == [("merge", 825, HEAD, False)]


def test_confirmed_merge_retry_only_finalizes_exact_head():
    record = kernel().pull_request(825)
    record["state"] = "MERGED"
    helper = kernel(record)
    assert request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)["finalized"]
    assert helper.calls == [("finalize", 825, HEAD)]
    with pytest.raises(request.Refusal, match="already-merged"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, True)


@pytest.mark.parametrize("state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("numbers", [[822], [821, 822], []])
def test_close_out_outside_admitted_issue_refuses_before_status_or_close(monkeypatch, state, numbers):
    import merge_state

    monkeypatch.setattr(merge_state, "issue", lambda *_a: pytest.fail("close-out read an issue"))
    monkeypatch.setattr(merge_state, "set_status", lambda *_a, **_k: pytest.fail("close-out changed status"))
    monkeypatch.setattr(merge_state, "run", lambda *_a: pytest.fail("close-out closed an issue"))
    helper = kernel(dict(kernel().pull_request(825), state=state))
    helper.close_out = merge_state.close_out

    def stray_close_out(*_args, **_kwargs):
        helper.calls.append("helper")
        return helper.close_out(numbers, ["scripts/merge_pr.py"])

    # Open requests reach merge(); confirmed-merge retries reach finalize_queued().
    helper.merge = helper.finalize_queued = stray_close_out
    with pytest.raises(request.Refusal, match="authorized-issue-changed"):
        request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)
    assert helper.calls == ["helper"]
    assert helper.close_out is merge_state.close_out


def test_admitted_close_out_calls_canonical_close_out_unchanged():
    helper = kernel(dict(kernel().pull_request(825), state="MERGED"))
    seen = []

    def canonical(numbers, changed_paths):
        seen.append((numbers, changed_paths))
        return [{"issue": numbers[0]}]

    helper.close_out = canonical
    helper.finalize_queued = lambda _pr, _head: {"finalized": bool(helper.close_out([821], ["README.md"]))}
    assert request.execute_request(helper, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False) == {"finalized": True}
    assert seen == [([821], ["README.md"])]
    assert helper.close_out is canonical


def test_authority_environment_has_no_caller_credentials_or_executable_inputs(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "caller-secret")
    monkeypatch.setenv("PYTHONPATH", "/caller/module")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    env = request.authority_environment({"principal_home": "/merge", "repository": "owner/repo",
                                         "authority_app_id": 42, "authority_runner": "/protected/runner"})
    assert "caller-secret" not in env.values()
    assert not {"GH_TOKEN", "PYTHONPATH", "ARU_GITHUB_APP_RUNNER"} & env.keys()
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(request.ROOT / "checkout")
    assert env["GH_REPO"] == "owner/repo"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_overlapping_requests_refuse_instead_of_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(request, "ROOT", tmp_path)
    (tmp_path / "runtime").mkdir(mode=0o700)
    first = request.execution_lock(os.getuid())
    try:
        with pytest.raises(request.Refusal, match="merge-context-busy"):
            request.execution_lock(os.getuid())
    finally:
        os.close(first)


def test_uninstalled_program_refuses_without_importing_privileged_helper(capsys):
    assert request.main(["--pr", "825", "--expected-head", HEAD]) == 2
    assert '"gate": "not-installed"' in capsys.readouterr().out


def integration_module(spec):
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = integration_module(importlib.util.spec_from_file_location(
    "merge_integration_app", ROOT / "integrations/merge-authority/app.py",
))
bundle = integration_module(importlib.util.spec_from_file_location(
    "merge_integration_bundle", ROOT / "integrations/merge-authority/bundle.py",
))
installer = integration_module(importlib.util.spec_from_file_location(
    "merge_integration_install", ROOT / "integrations/merge-authority/install.py",
))


def config_fixture():
    files = {name: "a" * 64 for name in (
        "request.py", "app.py", "bin/authority", "bin/gh", "checkout/.git/config", "checkout/.git/HEAD",
        *("lib/" + name for name in request.KERNEL_FILES),
    )}
    return {"version": 1, "repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822, 792],
            "principal_uid": 502, "principal_gid": 502, "principal_home": "/Users/aru-merge",
            "helper_commit": HEAD, "files": files, "authority_app_id": 4921120,
            "authority_installation_id": 123, "authority_runner": str(request.ROOT / "bin/authority"),
            "authority_files": {str(request.ROOT / name): files[name] for name in ("app.py", "bin/authority")},
            "python": "/Library/Developer/CommandLineTools/usr/bin/python3", "python_sha256": "c" * 64}


@pytest.mark.parametrize("name,value", [
    ("version", True), ("repository", "owner/../other"), ("issues", [821, 821]),
    ("issues", [True]), ("principal_uid", 0), ("principal_home", "/Users/../coding"),
    ("authority_runner", "/generic/runner"), ("authority_files", {}),
    ("files", {"../request.py": "a" * 64}), ("python_sha256", "wrong"),
])
def test_invalid_or_expanded_operator_config_refuses(name, value):
    config = config_fixture()
    config[name] = value
    with pytest.raises(request.Refusal):
        request.validate_config(config)


def test_valid_config_seals_complete_authority_inventory():
    config = config_fixture()
    assert request.validate_config(config) == config
    config["authority_files"][str(request.ROOT / "app.py")] = "b" * 64
    with pytest.raises(request.Refusal, match="authority-file-inventory"):
        request.validate_config(config)


def protected_fixture(tmp_path, monkeypatch):
    path = tmp_path / "guarded"
    path.write_bytes(b"reviewed-code")
    path.chmod(0o644)
    original = Path.lstat
    ancestors = set(path.parents)
    def metadata(item):
        if item in ancestors:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return original(item)
    monkeypatch.setattr(Path, "lstat", metadata)
    return path


def test_protected_file_rejects_writable_or_linked_code(tmp_path, monkeypatch):
    path = protected_fixture(tmp_path, monkeypatch)
    assert request.protected_bytes(path, {0, os.getuid()}) == b"reviewed-code"
    path.chmod(0o666)
    with pytest.raises(request.Refusal, match="mutable-installation-file"):
        request.protected_bytes(path, {0, os.getuid()})
    path.unlink()
    path.symlink_to(tmp_path / "unreviewed")
    with pytest.raises(OSError):
        request.protected_bytes(path, {0, os.getuid()})


def test_mutable_ancestor_is_rejected_before_read(tmp_path, monkeypatch):
    path = protected_fixture(tmp_path, monkeypatch)
    original = Path.lstat
    def metadata(item):
        if item == path.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        return original(item)
    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(request.Refusal, match="mutable-installation-chain"):
        request.protected_bytes(path, {0, os.getuid()})


def principal_account(**changes):
    account = {"pw_name": "aru-merge", "pw_uid": 502, "pw_gid": 502,
               "pw_dir": "/Users/aru-merge", "pw_shell": "/usr/bin/false"}
    return SimpleNamespace(**{**account, **changes})


def test_changed_content_or_wrong_principal_is_rejected(monkeypatch):
    config = config_fixture()
    monkeypatch.setattr(request.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(request.os, "geteuid", lambda: 501)
    with pytest.raises(request.Refusal, match="wrong-execution-principal"):
        request.verify_installation(config)
    monkeypatch.setattr(request.os, "geteuid", lambda: 502)
    monkeypatch.setattr(request.os, "getegid", lambda: 502)
    monkeypatch.setattr(request.pwd, "getpwuid", lambda _uid: principal_account())
    monkeypatch.setattr(request, "protected_bytes", lambda *_args: b"tampered")
    with pytest.raises(request.Refusal, match="installation-content-changed"):
        request.verify_installation(config)


@pytest.mark.parametrize("change,gate", [
    ({"pw_shell": "/bin/zsh"}, "login-capable-principal"),
    ({"pw_shell": "/sbin/nologin"}, "login-capable-principal"),
    ({"pw_shell": ""}, "login-capable-principal"),
    ({"pw_name": "gillella"}, "wrong-execution-principal"),
    ({"pw_dir": "/Users/gillella"}, "wrong-execution-principal"),
    (None, "wrong-execution-principal"),
])
def test_login_capable_or_changed_principal_refuses_before_code_or_credentials(monkeypatch, change, gate):
    def account(uid):
        if change is None:
            raise KeyError(uid)
        return principal_account(**change)

    monkeypatch.setattr(request.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(request.os, "geteuid", lambda: 502)
    monkeypatch.setattr(request.os, "getegid", lambda: 502)
    monkeypatch.setattr(request.pwd, "getpwuid", account)
    monkeypatch.setattr(request, "protected_bytes", lambda *_a, **_k: pytest.fail("read code or credentials"))
    with pytest.raises(request.Refusal, match=gate):
        request.verify_installation(config_fixture())


@pytest.mark.parametrize("read_number", [2, 3, 4])
@pytest.mark.parametrize("changed_issue", [793, 822])
def test_canonical_rereads_cannot_escape_or_switch_admitted_issue(monkeypatch, read_number, changed_issue):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    reads = []

    def live_pull(_number):
        reads.append(True)
        return dict(record, body=f"Closes #{changed_issue if len(reads) >= read_number else 821}")

    def gate(numbers, *_args, **_kwargs):
        return [{"issue": numbers[0], "criteria": 1}]

    monkeypatch.setattr(merge_pr, "pull_request", live_pull)
    monkeypatch.setattr(merge_pr, "issue_gate", gate)
    monkeypatch.setattr(merge_pr.merge_authority, "post", lambda *_a: pytest.fail("race posted authorization"))
    monkeypatch.setattr(merge_pr, "run", lambda *_a: pytest.fail("race submitted merge"))
    with pytest.raises(request.Refusal, match="authorized-issue"):
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)
    assert len(reads) == read_number
    assert merge_pr.pull_request is live_pull
    assert merge_pr.issue_gate is gate


def test_counted_approval_withdrawal_on_canonical_reread_refuses(monkeypatch):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    reads = []

    def live_pull(_number):
        reads.append(True)
        return dict(record, reviewDecision="APPROVED" if len(reads) == 1 else "REVIEW_REQUIRED")

    monkeypatch.setattr(merge_pr, "pull_request", live_pull)
    monkeypatch.setattr(merge_pr.merge_authority, "post", lambda *_a: pytest.fail("race posted authorization"))
    monkeypatch.setattr(merge_pr, "run", lambda *_a: pytest.fail("race submitted merge"))
    with pytest.raises(request.Refusal, match="github-counted-approval"):
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821]}, 825, HEAD, False)


def test_confirmed_merge_retry_cannot_close_out_a_reassigned_issue(monkeypatch):
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", state="MERGED", mergedAt="2026-10-11T03:00:00Z")
    monkeypatch.setattr(request, "merged_body_unchanged", lambda *_a: None)
    reads = []

    def live_pull(_number):
        reads.append(True)
        return dict(record, body="Closes #821" if len(reads) == 1 else "Closes #822")

    monkeypatch.setattr(merge_pr, "pull_request", live_pull)
    monkeypatch.setattr(merge_pr, "close_out", lambda *_a: pytest.fail("retry closed another issue"))
    monkeypatch.setattr(merge_pr.merge_authority, "post", lambda *_a: pytest.fail("retry posted authorization"))
    monkeypatch.setattr(merge_pr, "run", lambda *_a: pytest.fail("retry submitted merge"))
    with pytest.raises(request.Refusal, match="authorized-issue-changed"):
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)


def package_fixture(monkeypatch):
    monkeypatch.setattr(bundle, "git_object", lambda _source, _commit, path: path.encode())
    return bundle.make_bundle(ROOT, HEAD, "b" * 40, b"sealed-gh-binary", "gillella/Aru_Agentic_SDLC",
                              [821, 822, 792], gh_sha256=hashlib.sha256(b"sealed-gh-binary").hexdigest())


def test_package_preserves_reviewed_revisions_and_contains_no_credentials(monkeypatch):
    manifest, payload = installer.unpack(package_fixture(monkeypatch))
    assert manifest["source_commit"] == HEAD
    assert manifest["helper_commit"] == "b" * 40
    assert manifest["issues"] == [821, 822, 792]
    assert not any("credentials" in name or name.endswith(".pem") for name in payload)
    assert manifest["files"]["bin/gh"] == hashlib.sha256(b"sealed-gh-binary").hexdigest()


@pytest.mark.parametrize("malformation", ["tamper", "extra", "duplicate", "symlink"])
def test_package_rejects_tampering_and_archive_traversal(monkeypatch, malformation):
    raw = package_fixture(monkeypatch)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    if malformation == "tamper":
        payload["request.py"] = b"unreviewed-code"
    if malformation == "extra":
        payload["../escape"] = b"escape"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in payload.items():
            if malformation == "symlink" and name == "request.py":
                entry = zipfile.ZipInfo(name)
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, data)
            else:
                archive.writestr(name, data)
        if malformation == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("request.py", b"replacement")
    with pytest.raises(ValueError):
        installer.unpack(stream.getvalue())


def test_sudo_capability_has_no_generic_runner_shell_or_environment():
    policy = installer.policy_text("gillella", "aru-merge", "a" * 64)
    assert "NOPASSWD: NOSETENV: ARU_TRUSTED_MERGE" in policy
    assert "Cmnd_Alias ARU_TRUSTED_MERGE = sha256:" + "a" * 64 in policy
    assert f"/usr/bin/python3 -I {request.ROOT}/request.py --pr * --expected-head *" in policy
    assert "app.py" not in policy and "authority-run" not in policy
    with pytest.raises(ValueError):
        installer.policy_text("gillella;ALL", "aru-merge", "a" * 64)


def test_app_adapter_only_accepts_exact_head_authorization_check():
    repository = "gillella/Aru_Agentic_SDLC"
    args = ["--repo", repository, "--", "gh", "api", "--method", "POST",
            f"repos/{repository}/check-runs", "--input", "-"]
    payload = {"name": "aru-merge-authorized", "head_sha": HEAD, "status": "completed",
               "conclusion": "success", "output": {"title": "Passed", "summary": "Passed"}}
    assert app.operation(args, repository, HEAD, payload) == "check"
    for name, value in [("head_sha", "b" * 40), ("name", "other"), ("conclusion", "neutral")]:
        changed = {**payload, name: value}
        with pytest.raises(app.Refusal):
            app.operation(args, repository, HEAD, changed)
    for endpoint in (f"repos/{repository}/pulls/825/merge", "user", "installation/repositories"):
        with pytest.raises(app.Refusal):
            app.operation(["--repo", repository, "--", "gh", "api", endpoint], repository, HEAD, None)


@pytest.mark.parametrize("wrong_scope", [False, True])
def test_app_uses_single_repo_minimal_token_and_revokes_without_returning_secret(monkeypatch, wrong_scope):
    config = config_fixture()
    calls = []
    monkeypatch.setattr(app, "jwt", lambda _c: "jwt-secret")
    def api(method, endpoint, token, payload=None):
        calls.append((method, endpoint, token, payload))
        if endpoint == "app":
            return {"id": 4921120}
        if endpoint == "app/installations/123":
            return {"app_id": 4921120, "account": {"login": "gillella"}}
        if endpoint.endswith("/access_tokens"):
            return {"token": "installation-secret", "permissions": payload["permissions"],
                    "repositories": [{"full_name": "other/repo" if wrong_scope else config["repository"]}]}
        if endpoint.startswith("repos/"):
            return {"id": 1, "full_name": config["repository"], "private-data": "ignored"}
        return None
    monkeypatch.setattr(app, "api", api)
    if wrong_scope:
        with pytest.raises(app.Refusal, match="app-token-scope"):
            app.perform(config, "repository", None)
    else:
        assert app.perform(config, "repository", None) == {"id": 1, "full_name": config["repository"]}
    assert calls[-1][:2] == ("DELETE", "installation/token")
    assert calls[2][3] == {"repositories": ["Aru_Agentic_SDLC"],
                            "permissions": {"checks": "write", "metadata": "read"}}


def test_installation_never_executes_from_agent_or_unverified_mutable_source():
    with pytest.raises(ValueError, match="human root TTY"):
        installer.main()


@pytest.mark.parametrize("shell,audit", [("/bin/zsh", "AUDITED"), ("/usr/bin/false", "skipped")])
def test_installer_refuses_login_capable_or_unaudited_principal_before_key_or_writes(monkeypatch, shell, audit):
    accounts = {
        "gillella": SimpleNamespace(pw_uid=501, pw_gid=20, pw_name="gillella", pw_shell="/bin/zsh"),
        "aru-merge": SimpleNamespace(pw_uid=502, pw_gid=502, pw_name="aru-merge", pw_shell=shell),
    }
    answers = iter(["INSTALL", "gillella", audit, "123", "/operator/merge-app.pem"])
    monkeypatch.setattr(installer, "_BUNDLE_BYTES", package_fixture(monkeypatch), raising=False)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(installer, "system_interpreter", lambda: Path("/usr/bin/python3"))
    monkeypatch.setattr(installer, "root_chain", lambda _p: None)
    monkeypatch.setattr(installer.pwd, "getpwnam", accounts.__getitem__)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(installer, "private_key", lambda _p: pytest.fail("PEM read before principal checks"))
    monkeypatch.setattr(installer, "install_payload", lambda *_a: pytest.fail("installation wrote files"))
    with pytest.raises(ValueError, match="access audit" if shell == "/usr/bin/false" else "non-login shell"):
        installer.main()


def test_installation_transaction_refuses_login_capable_principal_before_writes(tmp_path, monkeypatch):
    manifest, payload = installer.unpack(package_fixture(monkeypatch))
    monkeypatch.setattr(installer, "ROOT", tmp_path / "installation")
    monkeypatch.setattr(installer, "POLICY", tmp_path / "aru-trusted-merge")
    monkeypatch.setattr(installer.subprocess, "run", lambda *_a, **_k: pytest.fail("staged sudo policy"))
    principal = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_name="aru-merge",
                                pw_dir="/merge", pw_shell="/bin/zsh")
    with pytest.raises(ValueError, match="non-login shell"):
        installer.install_payload(manifest, payload, "gillella", principal, 123, b"fixture-only-key")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid_policy", [False, True])
def test_installation_transaction_seals_payload_before_publishing_capability(tmp_path, monkeypatch, invalid_policy):
    manifest, payload = installer.unpack(package_fixture(monkeypatch))
    destination = tmp_path / "installation"
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    policy = policy_dir / "aru-trusted-merge"
    monkeypatch.setattr(installer, "ROOT", destination)
    monkeypatch.setattr(installer, "POLICY", policy)
    monkeypatch.setattr(installer.os, "chown", lambda *_a: None)
    monkeypatch.setattr(installer.os, "fchown", lambda *_a: None)
    # The transaction is exercised without root. Ownership refusal itself has
    # separate metadata tests; this fixture never edits a system location.
    monkeypatch.setattr(installer, "root_chain", lambda _p: None)
    monkeypatch.setattr(installer, "system_interpreter", lambda: Path(request.sys.executable).resolve())
    principal = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_name="aru-merge", pw_dir="/merge",
                                pw_shell="/usr/bin/false")
    calls = []
    def validate_policy(argv, **_kwargs):
        assert not policy.exists()
        assert (destination / "config.json").is_file()
        assert (destination / "bin/authority").is_file()
        calls.append(argv)
        if invalid_policy:
            raise ValueError("invalid policy")
    monkeypatch.setattr(installer.subprocess, "run", validate_policy)
    if invalid_policy:
        with pytest.raises(ValueError, match="invalid policy"):
            installer.install_payload(manifest, payload, "gillella", principal, 123, b"fixture-only-key")
        assert not destination.exists() and not policy.exists()
        assert list(policy_dir.iterdir()) == []
    else:
        installer.install_payload(manifest, payload, "gillella", principal, 123, b"fixture-only-key")
        assert policy.is_file() and policy.stat().st_mode & 0o777 == 0o440
        assert (destination / "credentials").stat().st_mode & 0o777 == 0o700
        assert (destination / "credentials/merge-app.pem").stat().st_mode & 0o777 == 0o600
        assert not (destination / "install.py").exists()
        for name, digest in manifest["files"].items():
            if name != "install.py":
                assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
        with pytest.raises(ValueError, match="Existing installation"):
            installer.install_payload(manifest, payload, "gillella", principal, 123, b"other-fixture")
        assert policy.is_file()
    assert calls[0][:2] == ["/usr/sbin/visudo", "-cf"]


def test_installation_preserves_existing_operator_staging(tmp_path, monkeypatch):
    manifest, payload = installer.unpack(package_fixture(monkeypatch))
    monkeypatch.setattr(installer, "ROOT", tmp_path / "new-installation")
    monkeypatch.setattr(installer, "POLICY", tmp_path / "aru-trusted-merge")
    staging = tmp_path / ".aru-trusted-merge-candidate"
    staging.write_bytes(b"another-operator")
    with pytest.raises(ValueError, match="staging file is preserved"):
        installer.install_payload(manifest, payload, "gillella", SimpleNamespace(), 123, b"fixture")
    assert staging.read_bytes() == b"another-operator"
    assert not installer.ROOT.exists()


@pytest.mark.parametrize("module", [request, installer])
def test_os_interpreter_alias_seals_resolved_binary_without_allowing_application_links(tmp_path, monkeypatch, module):
    binary = tmp_path / "actual-python"
    binary.write_bytes(b"system-interpreter-fixture")
    alias = tmp_path / "python3"
    alias.symlink_to(binary)
    monkeypatch.setattr(module.sys, "executable", str(alias))
    def metadata(path):
        mode = stat.S_IFLNK | 0o777 if path == alias else stat.S_IFDIR | 0o755
        return SimpleNamespace(st_uid=0, st_mode=mode)
    monkeypatch.setattr(Path, "lstat", metadata)
    # resolve() uses stat/readlink; only public metadata validation is replaced.
    checked = []
    if module is request:
        monkeypatch.setattr(module, "protected_bytes", lambda path, owners: checked.append((path, owners)) or b"fixture")
    else:
        monkeypatch.setattr(module, "root_chain", lambda path: checked.append((path, {0})))
    assert module.system_interpreter() == binary
    assert checked == [(binary, {0})]
    monkeypatch.setattr(Path, "lstat", lambda _p: SimpleNamespace(st_uid=501, st_mode=stat.S_IFLNK | 0o777))
    with pytest.raises((request.Refusal, ValueError), match="alias"):
        module.system_interpreter()


@pytest.mark.parametrize("change,gate", [
    ({"body": "Closes #822"}, "authorized-issue-changed"),
    ({"body": "Closes #793"}, "authorized-issue-scope"),
    ({"reviewDecision": "REVIEW_REQUIRED"}, "github-counted-approval"),
])
def test_drift_after_posted_authorization_supersedes_it_in_the_pinned_helper(monkeypatch, change, gate):
    """Read 5 is canonical await_unblocked(): outer 1, evaluate 2/3, final 4."""
    import merge_pr
    from test_merge_gate import install_happy_gate

    record = install_happy_gate(monkeypatch)
    record.update(number=825, body="Closes #821", reviewDecision="APPROVED")
    reads, posts = [], []

    def live_pull(_number):
        reads.append(True)
        return dict(record, **(change if len(reads) >= 5 else {}))

    def gate_evidence(numbers, *_args, **_kwargs):
        return [{"issue": numbers[0], "criteria": 1}]

    def post(head, conclusion, _summary):
        posts.append((head, conclusion))
        return len(posts)

    def no_close_out(*_args, **_kwargs):
        pytest.fail("refused request closed out an issue")

    monkeypatch.setattr(merge_pr, "pull_request", live_pull)
    monkeypatch.setattr(merge_pr, "issue_gate", gate_evidence)
    monkeypatch.setattr(merge_pr, "close_out", no_close_out)
    monkeypatch.setattr(merge_pr.merge_authority, "post", post)
    monkeypatch.setattr(merge_pr, "run", lambda *_a, **_k: pytest.fail("refused request submitted a merge"))
    with pytest.raises(request.Refusal, match=gate) as refused:
        request.execute_request(merge_pr, {"repository": "gillella/Aru_Agentic_SDLC", "issues": [821, 822]}, 825, HEAD, False)
    # Canonical merge() caught it as KernelError and posted the same-head failure.
    assert isinstance(refused.value, merge_pr.KernelError)
    assert len(reads) == 5
    assert posts == [(HEAD, "success"), (HEAD, "failure")]
    assert merge_pr.pull_request is live_pull
    assert merge_pr.issue_gate is gate_evidence
    assert merge_pr.close_out is no_close_out


def check_payload(conclusion):
    return {"name": "aru-merge-authorized", "head_sha": HEAD, "status": "completed",
            "conclusion": conclusion, "output": {"title": "Verdict", "summary": "Verdict"}}


def recorded(body):
    return {"id": 7, "app": {"id": 4921120, "slug": "merge-authority"}, "head_sha": body["head_sha"],
            "name": body["name"], "conclusion": body["conclusion"], "output": body["output"]}


def app_authority(monkeypatch, respond, *, revoke_fails=False):
    config = config_fixture()
    calls = []
    monkeypatch.setattr(app, "jwt", lambda _c: "jwt-secret")

    def api(method, endpoint, token, payload=None):
        calls.append((method, endpoint, token, payload))
        if endpoint == "app":
            return {"id": 4921120}
        if endpoint == "app/installations/123":
            return {"app_id": 4921120, "account": {"login": "gillella"}}
        if endpoint.endswith("/access_tokens"):
            return {"token": "installation-secret", "permissions": payload["permissions"],
                    "repositories": [{"full_name": config["repository"]}]}
        if endpoint == f"repos/{config['repository']}/check-runs":
            return respond(payload)
        if revoke_fails:
            raise OSError("revocation failed for installation-secret")
        return None

    monkeypatch.setattr(app, "api", api)
    return config, calls


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_token_cleanup_failure_never_masks_a_confirmed_check(monkeypatch, conclusion):
    config, calls = app_authority(monkeypatch, recorded, revoke_fails=True)
    result = app.perform(config, "check", check_payload(conclusion))
    assert result == {"id": 7, "app": {"id": 4921120}, "head_sha": HEAD,
                      "name": "aru-merge-authorized", "conclusion": conclusion}
    assert [call[:2] for call in calls[3:]] == [
        ("POST", f"repos/{config['repository']}/check-runs"), ("DELETE", "installation/token"),
    ]
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("response", [
    {"id": True}, {"id": 0}, {"id": "7"}, {"app": {"id": 4740358}}, {"app": None},
    {"head_sha": "b" * 40}, {"name": "other"}, {"conclusion": "failure"}, "timeout", "list",
])
def test_unconfirmed_success_is_superseded_at_same_head_then_refused(monkeypatch, response):
    posts = []

    def respond(body):
        posts.append(body)
        if len(posts) > 1:
            return recorded(body)
        if response == "timeout":
            raise TimeoutError("read timed out after installation-secret was sent")
        if response == "list":
            return [recorded(body)]
        return {**recorded(body), **response}

    config, calls = app_authority(monkeypatch, respond)
    with pytest.raises(app.Refusal, match="app-check-unconfirmed") as refused:
        app.perform(config, "check", check_payload("success"))
    # No transport detail or token reaches the refusal; canonical post() sees only it.
    assert "secret" not in str(refused.value) and refused.value.__cause__ is None
    assert [body["conclusion"] for body in posts] == ["success", "failure"]
    assert all(body["head_sha"] == HEAD and body["name"] == "aru-merge-authorized" for body in posts)
    assert {call[:3] for call in calls[3:-1]} == {
        ("POST", f"repos/{config['repository']}/check-runs", "installation-secret"),
    }
    assert calls[-1][:3] == ("DELETE", "installation/token", "installation-secret")


def test_failed_compensation_is_bounded_and_still_refuses(monkeypatch):
    conclusions = []

    def respond(body):
        conclusions.append(body["conclusion"])
        raise OSError("connection reset")

    config, calls = app_authority(monkeypatch, respond, revoke_fails=True)
    with pytest.raises(app.Refusal, match="app-check-unconfirmed"):
        app.perform(config, "check", check_payload("success"))
    assert conclusions == ["success"] + ["failure"] * app.COMPENSATION_ATTEMPTS
    assert calls[-1][:2] == ("DELETE", "installation/token")
