from __future__ import annotations

import pytest

import check_ci
import fetch_pr_feedback

HEAD = "a" * 40


def check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    app_id: int = check_ci.GITHUB_ACTIONS_APP_ID,
    head: str = HEAD,
) -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app_id, "slug": "github-actions"},
        "head_sha": head,
    }


def install_checks(monkeypatch, runs: list[dict], *, head: str = HEAD) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(check_ci, "repo_slug", lambda: "owner/repo")

    def snapshot(argv):
        calls.append(argv)
        if argv[:2] == ["pr", "view"]:
            return {"number": 3, "headRefOid": head}
        if argv[0] == "api":
            return {"total_count": len(runs), "check_runs": runs}
        raise AssertionError(argv)

    monkeypatch.setattr(check_ci, "gh_json", snapshot)
    return calls


def test_ci_reads_authenticated_required_check_from_exact_head(monkeypatch):
    calls = install_checks(monkeypatch, [check_run("aru-governed-pr")])
    assert check_ci.ci_verdict(3) == {
        "pr": 3,
        "head": HEAD,
        "state": "success",
        "checks": ["aru-governed-pr"],
    }
    assert calls[0] == ["pr", "view", "3", "--json", "number,headRefOid"]
    assert "/commits/" + HEAD + "/check-runs" in calls[1][1]


def test_ci_authenticates_only_the_kernel_check_and_ignores_other_apps(monkeypatch):
    install_checks(
        monkeypatch,
        [
            check_run("aru-governed-pr"),
            check_run("build", app_id=999),
        ],
    )
    verdict = check_ci.ci_verdict(3)
    assert verdict["state"] == "success"
    assert verdict["checks"] == ["aru-governed-pr"]


def test_ci_is_pending_when_required_check_is_missing_or_running(monkeypatch):
    install_checks(monkeypatch, [check_run("unrelated")])
    assert check_ci.ci_verdict(3)["state"] == "pending"
    install_checks(
        monkeypatch,
        [check_run("aru-governed-pr", status="IN_PROGRESS", conclusion="")],
    )
    assert check_ci.ci_verdict(3)["state"] == "pending"


@pytest.mark.parametrize("conclusion", ["FAILURE", "NEUTRAL", "SKIPPED", "CANCELLED"])
def test_ci_requires_success_conclusion(monkeypatch, conclusion):
    install_checks(monkeypatch, [check_run("aru-governed-pr", conclusion=conclusion)])
    assert check_ci.ci_verdict(3)["state"] == "failure"


def test_ci_rejects_spoofed_source_or_wrong_head(monkeypatch):
    install_checks(monkeypatch, [check_run("aru-governed-pr", app_id=999)])
    with pytest.raises(check_ci.KernelError, match="untrusted source"):
        check_ci.ci_verdict(3)
    install_checks(monkeypatch, [check_run("aru-governed-pr", head="b" * 40)])
    with pytest.raises(check_ci.KernelError, match="exact head"):
        check_ci.ci_verdict(3)


def test_ci_fails_closed_on_ambiguous_or_truncated_inventory(monkeypatch):
    install_checks(
        monkeypatch,
        [check_run("aru-governed-pr"), check_run("aru-governed-pr")],
    )
    with pytest.raises(check_ci.KernelError, match="ambiguous"):
        check_ci.ci_verdict(3)
    monkeypatch.setattr(check_ci, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda argv: {"number": 3, "headRefOid": HEAD}
        if argv[0] == "pr"
        else {"total_count": 2, "check_runs": [check_run("aru-governed-pr")]},
    )
    with pytest.raises(check_ci.KernelError, match="incomplete"):
        check_ci.ci_verdict(3)


def test_feedback_collects_unresolved_threads(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda _argv, *, auth: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "a.py",
                                    "line": 7,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "author": {"login": "reviewer"},
                                                "body": "fix this",
                                                "url": "https://example/thread",
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        if auth == fetch_pr_feedback.REPOSITORY_AUTH
        else pytest.fail("expected repository authority"),
    )
    feedback = fetch_pr_feedback.fetch_feedback(9)
    assert feedback[0]["path"] == "a.py"
    assert feedback[0]["author"] == "reviewer"


def test_feedback_fails_closed_on_comment_truncation(monkeypatch):
    monkeypatch.setattr(fetch_pr_feedback, "repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        fetch_pr_feedback,
        "gh_json",
        lambda _argv, *, auth: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "nodes": [{}],
                                        "pageInfo": {"hasNextPage": True},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        }
        if auth == fetch_pr_feedback.REPOSITORY_AUTH
        else pytest.fail("expected repository authority"),
    )
    with pytest.raises(fetch_pr_feedback.KernelError, match="truncated"):
        fetch_pr_feedback.fetch_feedback(9)
