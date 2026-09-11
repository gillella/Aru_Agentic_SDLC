from __future__ import annotations

import json
import subprocess

import pytest

import init_project
import merge_authority
import merge_pr
from test_merge_gate import base_pr, install_happy_gate

HEAD = "a" * 40
SLUG = "gillella/consumer"
REAL_POST = merge_authority.post


@pytest.fixture
def runner(tmp_path, monkeypatch):
    path = tmp_path / "merge-app-run"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)
    monkeypatch.setenv(merge_authority.RUNNER_ENV, str(path))
    monkeypatch.setenv(merge_authority.APP_ID_ENV, "4242")
    monkeypatch.setattr(merge_authority, "repo_slug", lambda: SLUG)
    return str(path)


def fake_app(monkeypatch, record=None, error=None):
    calls = []

    def run(argv, *, input_text=None):
        calls.append((argv, json.loads(input_text) if input_text else None))
        if error:
            raise merge_authority.KernelError(error)
        body = record if record is not None else {
            "id": 99, "name": "aru-merge-authorized", "head_sha": HEAD,
            "conclusion": calls[-1][1]["conclusion"] if calls[-1][1] else None, "app": {"id": 4242},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(merge_authority, "run", run)
    return calls


def test_gate_is_off_when_unconfigured(monkeypatch):
    calls = fake_app(monkeypatch)
    assert merge_authority.configured() is None
    assert merge_authority.post(HEAD, "success", "ok") is None
    assert merge_authority.installed(SLUG) is False
    assert calls == []


@pytest.mark.parametrize("runner_set, app_id", [(True, None), (False, "4242"), (True, "12a"), (True, "0")])
def test_half_configured_gate_refuses(runner, monkeypatch, runner_set, app_id):
    if not runner_set:
        monkeypatch.delenv(merge_authority.RUNNER_ENV)
    if app_id is None:
        monkeypatch.delenv(merge_authority.APP_ID_ENV)
    else:
        monkeypatch.setenv(merge_authority.APP_ID_ENV, app_id)
    with pytest.raises(merge_authority.KernelError, match="needs both"):
        merge_authority.configured()


def test_runner_must_be_executable(runner, monkeypatch, tmp_path):
    plain = tmp_path / "not-executable"
    plain.write_text("", encoding="utf-8")
    monkeypatch.setenv(merge_authority.RUNNER_ENV, str(plain))
    with pytest.raises(merge_authority.KernelError, match="not executable"):
        merge_authority.configured()


def test_post_records_the_check_as_the_configured_app(runner, monkeypatch):
    calls = fake_app(monkeypatch)
    assert merge_authority.post(HEAD, "success", "gates passed") == 99
    [(argv, payload)] = calls
    assert argv == [runner, "--repo", SLUG, "--", "gh", "api", "--method", "POST",
                    f"repos/{SLUG}/check-runs", "--input", "-"]
    assert payload["name"] == "aru-merge-authorized"
    assert (payload["head_sha"], payload["status"], payload["conclusion"]) == (HEAD, "completed", "success")


@pytest.mark.parametrize("mutation", [
    {"app": {"id": 1}}, {"app": None}, {"head_sha": "b" * 40}, {"conclusion": "failure"},
    {"name": "aru-governed-pr"}, {"id": "99"},
])
def test_post_refuses_a_record_the_ruleset_would_not_count(runner, monkeypatch, mutation):
    record = {"id": 99, "name": "aru-merge-authorized", "head_sha": HEAD,
              "conclusion": "success", "app": {"id": 4242}, **mutation}
    fake_app(monkeypatch, record=record)
    with pytest.raises(merge_authority.KernelError, match="configured App at the exact head"):
        merge_authority.post(HEAD, "success", "gates passed")


def test_installed_reflects_whether_the_app_can_act_on_the_repository(runner, monkeypatch):
    calls = fake_app(monkeypatch, record={"full_name": SLUG})
    assert merge_authority.installed(SLUG) is True
    assert calls[0][0][-1] == f"repos/{SLUG}"
    fake_app(monkeypatch, error="no installation")
    assert merge_authority.installed(SLUG) is False


def test_blocked_is_mergeable_only_while_the_gate_is_on(monkeypatch):
    queue = {"configured": False, "entry": None, "auto_merge": None}
    blocked = base_pr(mergeStateStatus="BLOCKED")
    # BLOCKED passes the early state check so review reasons surface first; the
    # later stage still refuses it unless the merge-authority gate is on.
    merge_pr.require_mergeable(blocked, queue)
    with pytest.raises(merge_pr.KernelError, match="merge state is BLOCKED"):
        merge_pr.require_unblocked(blocked)
    monkeypatch.setattr(merge_authority, "configured", lambda: ("/runner", 4242))
    merge_pr.require_unblocked(blocked)
    with pytest.raises(merge_pr.KernelError, match="merge state is DIRTY"):
        merge_pr.require_mergeable(base_pr(mergeStateStatus="DIRTY"), queue)


def install_merge(monkeypatch, *, states=("CLEAN",), submit_error=None):
    pr = install_happy_gate(monkeypatch)
    gates = merge_pr.evaluate(10, HEAD)
    monkeypatch.setattr(merge_pr, "evaluate", lambda *_args: gates)
    events, snapshots = [], iter(states)

    def pull_request(_number):
        return {**pr, "mergeStateStatus": next(snapshots, states[-1]), "mergedAt": "now",
                "mergeCommit": {"oid": "c" * 40}}

    def submit(argv):
        events.append(("submit", argv[:3]))
        if submit_error:
            raise merge_pr.KernelError(submit_error)

    monkeypatch.setattr(merge_pr, "pull_request", pull_request)
    monkeypatch.setattr(merge_pr, "run", submit)
    monkeypatch.setattr(merge_pr.time, "sleep", lambda _s: events.append(("sleep",)))
    monkeypatch.setattr(merge_pr.merge_authority, "post",
                        lambda head, conclusion, _summary: events.append(("post", head, conclusion)) or 7)
    monkeypatch.setattr(merge_pr, "finalize_queued", lambda number, head: {"merged": True, "pr": number})
    return events


def test_merge_posts_authorization_before_submission(monkeypatch):
    events = install_merge(monkeypatch, states=("CLEAN", "BLOCKED", "CLEAN"))
    assert merge_pr.merge(10, HEAD)["merged"] is True
    assert events == [("post", HEAD, "success"), ("sleep",), ("submit", ["gh", "pr", "merge"])]


def test_failed_submission_supersedes_the_authorization(monkeypatch):
    events = install_merge(monkeypatch, submit_error="merge refused")
    with pytest.raises(merge_pr.KernelError, match="merge refused"):
        merge_pr.merge(10, HEAD)
    assert events[-1] == ("post", HEAD, "failure")


def test_still_blocked_after_authorization_is_revoked_without_submitting(monkeypatch):
    events = install_merge(monkeypatch, states=("CLEAN", "BLOCKED"))
    with pytest.raises(merge_pr.KernelError, match="still blocked after merge authorization"):
        merge_pr.merge(10, HEAD)
    assert ("submit", ["gh", "pr", "merge"]) not in events
    assert events[0] == ("post", HEAD, "success") and events[-1] == ("post", HEAD, "failure")


def test_failed_revocation_keeps_the_original_merge_failure(monkeypatch):
    install_merge(monkeypatch, submit_error="merge refused")

    def post(_head, conclusion, _summary):
        if conclusion == "failure":
            raise merge_pr.KernelError("runner down")
        return 7

    monkeypatch.setattr(merge_pr.merge_authority, "post", post)
    with pytest.raises(merge_pr.KernelError, match="runner down; original merge failure: merge refused"):
        merge_pr.merge(10, HEAD)


def test_unconfigured_merge_never_calls_the_app(monkeypatch):
    install_merge(monkeypatch)
    monkeypatch.setattr(merge_pr.merge_authority, "post", REAL_POST)
    fake_app(monkeypatch, error="must not run")
    assert merge_pr.merge(10, HEAD)["merged"] is True


def test_consumer_ruleset_allows_only_merge_commits_and_pins_the_app():
    for payload in (init_project.ruleset_payload(), init_project.ruleset_payload(4242)):
        rules = {rule["type"]: rule["parameters"] for rule in payload["rules"] if "parameters" in rule}
        assert rules["pull_request"]["allowed_merge_methods"] == ["merge"]
    checks = {rule["type"]: rule for rule in init_project.ruleset_payload(4242)["rules"]}
    assert checks["required_status_checks"]["parameters"]["required_status_checks"] == [
        {"context": "aru-governed-pr", "integration_id": init_project.GITHUB_ACTIONS_APP_ID},
        {"context": "aru-merge-authorized", "integration_id": 4242},
    ]


def setup_calls(monkeypatch):
    rulesets = []
    responses = {
        ("gh", "repo", "view"): {"nameWithOwner": SLUG},
        ("gh", "project", "create"): {"number": 5, "url": "https://example.test/project/5"},
        ("gh", "project", "field-list"): {"fields": [{"name": "Status", "id": "PVTSSF_1"}]},
    }

    def fake_command(argv, *, cwd, json_output=False, auth=None):
        if argv[:3] == ["gh", "api", f"repos/{SLUG}/rulesets"]:
            rulesets.append(json.loads(init_project.Path(argv[argv.index("--input") + 1]).read_text()))
            return {"_links": {"html": {"href": "https://example.test/rules/1"}}}
        return responses.get(tuple(argv[:3]), "")

    monkeypatch.setattr(init_project, "command", fake_command)
    return rulesets


@pytest.mark.parametrize("installed, expected", [(True, "required"), (False, "not-installed")])
def test_bootstrap_pins_the_check_only_once_the_app_is_installed(monkeypatch, tmp_path, installed, expected):
    rulesets = setup_calls(monkeypatch)
    monkeypatch.setattr(merge_authority, "configured", lambda: ("/runner", 4242))
    monkeypatch.setattr(merge_authority, "installed", lambda slug: slug == SLUG and installed)
    result = init_project.github_setup("consumer", tmp_path, private=True, owner="gillella",
                                       runner_profile="self-hosted-mac")
    assert result["merge_authority"] == expected
    assert rulesets == [init_project.ruleset_payload(4242 if installed else None)]


def test_half_configured_bootstrap_stops_before_creating_anything(monkeypatch, tmp_path):
    monkeypatch.setenv(merge_authority.APP_ID_ENV, "4242")
    monkeypatch.setattr(init_project, "command", lambda *a, **k: pytest.fail("no GitHub call expected"))
    with pytest.raises(init_project.BootstrapError, match="needs both"):
        init_project.github_setup("consumer", tmp_path, private=True, owner="gillella",
                                  runner_profile="self-hosted-mac")
