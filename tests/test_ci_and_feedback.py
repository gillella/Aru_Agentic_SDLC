from __future__ import annotations

from copy import deepcopy

import pytest

import check_ci
import fetch_pr_feedback

HEAD = "a" * 40
SLUG = "owner/repo"
PR = {"number": 3, "headRefOid": HEAD, "createdAt": "2026-09-09T14:55:38Z",
      "headRefName": "fix/issue", "baseRefName": "main"}


def workflow_run(run_id=20, *, created="2026-09-09T14:55:42Z", state="success"):
    return {"id": run_id, "workflow_id": 99, "check_suite_id": run_id + 100,
            "path": ".github/workflows/governed-pr.yml", "event": "pull_request",
            "head_sha": HEAD, "head_branch": "fix/issue", "run_attempt": 1,
            "created_at": created, "status": "completed" if state != "pending" else "queued",
            "conclusion": state if state != "pending" else None,
            "repository": {"full_name": SLUG}, "head_repository": {"full_name": SLUG},
            "pull_requests": [{"number": 3, "head": {"sha": HEAD, "ref": "fix/issue",
                "repo": {"url": f"https://api.github.com/repos/{SLUG}"}},
                "base": {"ref": "main", "repo": {"url": f"https://api.github.com/repos/{SLUG}"}}}]}


def check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    app_id: int = check_ci.GITHUB_ACTIONS_APP_ID,
    head: str = HEAD,
    run_id: int = 20,
) -> dict:
    return {
        "id": run_id + 1000,
        "check_suite": {"id": run_id + 100},
        "details_url": f"https://github.com/{SLUG}/actions/runs/{run_id}/job/{run_id + 1000}",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app_id, "slug": "github-actions"},
        "head_sha": head,
    }


def install_checks(monkeypatch, runs: list[dict], *, head: str = HEAD, workflows=None) -> list[list[str]]:
    calls: list[list[str]] = []
    if workflows is None:
        workflows = [workflow_run()] if any(r["name"] == "aru-governed-pr" for r in runs) else []
        if workflows:
            workflows[0].update(status=runs[0]["status"].lower(), conclusion=runs[0]["conclusion"].lower() or None)
    monkeypatch.setattr(check_ci, "repo_slug", lambda: "owner/repo")

    def snapshot(argv):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return {**PR, "headRefOid": head}
        if argv[-1].endswith("/workflows/governed-pr.yml"):
            return {"id": 99, "path": ".github/workflows/governed-pr.yml", "state": "active"}
        if "/runs?" in argv[-1]:
            return {"total_count": len(workflows), "workflow_runs": workflows}
        if argv[0] == "api":
            return {"total_count": len(runs), "check_runs": runs}
        raise AssertionError(argv)

    monkeypatch.setattr(check_ci, "gh_json", snapshot)
    return calls


@pytest.mark.parametrize("extra", [[], [check_run("build", app_id=999)]])
def test_ci_authenticates_required_exact_head_check_only(monkeypatch, extra):
    calls = install_checks(monkeypatch, [check_run("aru-governed-pr"), *extra])
    assert check_ci.ci_verdict(3) == {
        "pr": 3, "head": HEAD, "state": "success", "checks": ["aru-governed-pr"],
    }
    assert calls[0] == ["pr", "view", "3", "--json", "number,headRefOid,createdAt,headRefName,baseRefName"]
    assert "/commits/" + HEAD + "/check-runs" in calls[1][1]


@pytest.mark.parametrize("record,expected", [
    (check_run("unrelated"), "pending"),
    (check_run("aru-governed-pr", status="IN_PROGRESS", conclusion=""), "pending"),
    *((check_run("aru-governed-pr", conclusion=value), "failure")
      for value in ("FAILURE", "NEUTRAL", "SKIPPED", "CANCELLED")),
])
def test_ci_requires_completed_success(monkeypatch, record, expected):
    install_checks(monkeypatch, [record])
    assert check_ci.ci_verdict(3)["state"] == expected


@pytest.mark.parametrize("changes,message", [({"app_id": 999}, "untrusted source"),
                                            ({"head": "b" * 40}, "exact head")])
def test_ci_rejects_spoofed_source_or_wrong_head(monkeypatch, changes, message):
    install_checks(monkeypatch, [check_run("aru-governed-pr", **changes)])
    with pytest.raises(check_ci.KernelError, match=message):
        check_ci.ci_verdict(3)


@pytest.mark.parametrize("truncated", [False, True])
def test_ci_fails_closed_on_ambiguous_or_truncated_inventory(monkeypatch, truncated):
    install_checks(monkeypatch, [check_run("aru-governed-pr"), check_run("aru-governed-pr")])
    if truncated:
        original = check_ci.gh_json
        def inventory(args):
            data = original(args)
            if "check_runs" in data:
                data["total_count"] += 1
            return data
        monkeypatch.setattr(check_ci, "gh_json", inventory)
    with pytest.raises(check_ci.KernelError, match="incomplete" if truncated else "ambiguous"):
        check_ci.ci_verdict(3)


@pytest.mark.parametrize("order", [False, True])
def test_replacement_pr_ignores_only_proven_history(monkeypatch, order):
    runs = [check_run("aru-governed-pr", run_id=10), check_run("aru-governed-pr")]
    workflows = [workflow_run(10, created="2026-09-09T11:01:14Z"), workflow_run()]
    # GitHub retroactively associates the old run with the new PR as well.
    if order:
        runs.reverse()
        workflows.reverse()
    install_checks(monkeypatch, runs, workflows=workflows)
    assert check_ci.ci_verdict(3)["state"] == "success"


@pytest.mark.parametrize("state", ["success", "pending", "failure"])
@pytest.mark.parametrize("historical", [False, True])
def test_all_current_governing_executions_contribute(monkeypatch, state, historical):
    old = workflow_run(10, created="2026-09-09T11:01:14Z" if historical else "2026-09-09T14:55:40Z")
    current = workflow_run(state=state)
    runs = [check_run("aru-governed-pr", run_id=10), check_run("aru-governed-pr",
        status=current["status"], conclusion=current["conclusion"] or "")]
    install_checks(monkeypatch, runs, workflows=[old, current])
    assert check_ci.ci_verdict(3)["state"] == state


def test_queued_workflow_without_job_cannot_hide_behind_green(monkeypatch):
    install_checks(monkeypatch, [check_run("aru-governed-pr")],
                   workflows=[workflow_run(), workflow_run(30, state="pending")])
    assert check_ci.ci_verdict(3)["state"] == "pending"


def test_only_historical_success_is_pending(monkeypatch):
    install_checks(monkeypatch, [check_run("aru-governed-pr")],
                   workflows=[workflow_run(created="2026-09-09T11:01:14Z")])
    assert check_ci.ci_verdict(3)["state"] == "pending"


@pytest.mark.parametrize(("field", "value"), [
    ("event", "push"), ("event", "pull_request_target"), ("event", "merge_group"),
    ("path", ".github/workflows/other.yml"), ("workflow_id", 100),
    ("head_sha", "b" * 40), ("head_repository", {"full_name": "evil/fork"}),
    ("repository", None), ("head_branch", "other"), ("check_suite_id", None),
    ("created_at", None), ("created_at", "invalid"), ("created_at", "2026-09-09"),
    ("created_at", "2999-01-01T00:00:00Z"), ("run_attempt", None),
    ("run_attempt", True), ("status", "unknown"), ("conclusion", "unknown"),
    ("status", {}), ("conclusion", {}), ("conclusion", []),
    ("pull_requests", []), ("pull_requests", [None]),
])
def test_workflow_provenance_fails_closed(monkeypatch, field, value):
    workflow = workflow_run()
    workflow[field] = value
    install_checks(monkeypatch, [check_run("aru-governed-pr")], workflows=[workflow])
    with pytest.raises(check_ci.KernelError):
        check_ci.ci_verdict(3)


@pytest.mark.parametrize("mutation", ["pr", "base", "repo", "head", "suite", "url", "duplicate", "missing"])
def test_conflicting_or_partial_binding_fails_closed(monkeypatch, mutation):
    workflow, check = workflow_run(), check_run("aru-governed-pr")
    workflows = [workflow]
    if mutation == "pr":
        workflow["pull_requests"][0]["number"] = 2
    elif mutation == "base":
        workflow["pull_requests"][0]["base"]["ref"] = "other"
    elif mutation == "repo":
        workflow["pull_requests"][0]["head"]["repo"] = {}
    elif mutation == "head":
        workflow["pull_requests"][0]["head"]["sha"] = "b" * 40
    elif mutation == "suite":
        check["check_suite"]["id"] = 999
    elif mutation == "url":
        check["details_url"] = "https://evil.test/actions/runs/20/job/1020"
    elif mutation == "duplicate":
        workflows.append(deepcopy(workflow))
    elif mutation == "missing":
        workflows.clear()
    install_checks(monkeypatch, [check], workflows=workflows)
    with pytest.raises(check_ci.KernelError):
        check_ci.ci_verdict(3)

@pytest.mark.parametrize("truncated", [False, True])
def test_feedback_requires_complete_unresolved_threads(monkeypatch, truncated):
    comment = {"author": {"login": "reviewer"}, "body": "fix this", "url": "https://example/thread"}
    thread = {"isResolved": False, "isOutdated": False, "path": "a.py", "line": 7,
              "comments": {"nodes": [comment], "pageInfo": {"hasNextPage": truncated}}}
    connection = {"nodes": [thread], "pageInfo": {"hasNextPage": False, "endCursor": None}}
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    def snapshot(_argv, *, auth):
        assert auth == fetch_pr_feedback.REPOSITORY_AUTH
        return {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}}
    monkeypatch.setattr(fetch_pr_feedback, "gh_json", snapshot)
    if truncated:
        with pytest.raises(fetch_pr_feedback.KernelError, match="truncated"):
            fetch_pr_feedback.fetch_feedback(9)
    else:
        feedback = fetch_pr_feedback.fetch_feedback(9)
        assert feedback[0]["path"] == "a.py" and feedback[0]["author"] == "reviewer"


@pytest.mark.parametrize("which", ["check", "workflow", "runs"])
@pytest.mark.parametrize("value", [None, {}, [], True])
def test_malformed_inventories_never_become_empty_or_success(monkeypatch, which, value):
    install_checks(monkeypatch, [check_run("aru-governed-pr")])
    original = check_ci.gh_json
    def inventory(args):
        if args[0] == "api":
            key = "check" if "/check-runs?" in args[-1] else "runs" if "/runs?" in args[-1] else "workflow"
            if key == which:
                return value
        return original(args)
    monkeypatch.setattr(check_ci, "gh_json", inventory)
    with pytest.raises(check_ci.KernelError):
        check_ci.ci_verdict(3)


@pytest.mark.parametrize("change", ["truncated", "bool-total", "success-pending", "head-race"])
def test_workflow_inventory_and_reread_conflicts_block(monkeypatch, change):
    install_checks(monkeypatch, [check_run("aru-governed-pr")])
    original, pr_reads = check_ci.gh_json, []
    def inventory(args):
        data = deepcopy(original(args))
        if "/runs?" in args[-1]:
            if change == "truncated":
                data["total_count"] += 1
            elif change == "bool-total":
                data["total_count"] = True
        if "/check-runs?" in args[-1] and change == "success-pending":
            data["check_runs"][0].update(status="queued", conclusion=None)
        if args[0] == "pr":
            pr_reads.append(1)
            if change == "head-race" and len(pr_reads) > 1:
                data["headRefOid"] = "b" * 40
        return data
    monkeypatch.setattr(check_ci, "gh_json", inventory)
    with pytest.raises(check_ci.KernelError):
        check_ci.ci_verdict(3)
