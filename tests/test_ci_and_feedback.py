from __future__ import annotations

import json
import subprocess

import pytest

import check_ci
import fetch_pr_feedback
import local_verification


def verification_body(head: str, *commands: str, results: list[dict[str, object]] | None = None) -> str:
    payload = {
        "head": head,
        "commands": list(commands),
        "results": results
        if results is not None
        else [
            {"command": command, "argv": command.split(), "returncode": 0}
            for command in commands
        ],
    }
    return (
        "## Summary\n\nExample\n\n"
        "## Verification\n\n"
        + "\n".join(f"- `{command}`" for command in commands)
        + "\n\n"
        f"<!-- aru-local-verification:v1 {json.dumps(payload, sort_keys=True)} -->"
    )


def ok(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_ci_reads_exact_head_local_verification(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": verification_body("a" * 40, "python3 -m pytest tests/test_ci_and_feedback.py -q"),
        },
    )
    assert check_ci.ci_verdict(3) == {
        "pr": 3,
        "head": "a" * 40,
        "state": "success",
        "checks": ["python3 -m pytest tests/test_ci_and_feedback.py -q"],
    }


def test_ci_is_pending_when_local_verification_is_stale(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": verification_body("b" * 40, "python3 -m pytest tests/test_ci_and_feedback.py -q"),
        },
    )
    assert check_ci.ci_verdict(3)["state"] == "pending"


def test_ci_is_pending_when_local_verification_marker_is_missing(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": (
                "## Summary\n\nExample\n\n"
                "## Verification\n\n"
                "- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n"
            ),
        },
    )
    assert check_ci.ci_verdict(3)["state"] == "pending"


def test_ci_fails_closed_on_malformed_local_verification(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": "## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n\n"
            "<!-- aru-local-verification:v1 {not-json} -->",
        },
    )
    result = check_ci.ci_verdict(3)
    assert result["state"] == "failure"
    assert result["checks"] == []


def test_ci_fails_closed_when_marker_only_records_intent(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": "## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n\n"
            '<!-- aru-local-verification:v1 {"commands": ["python3 -m pytest tests/test_ci_and_feedback.py -q"], "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->',
        },
    )
    result = check_ci.ci_verdict(3)
    assert result["state"] == "failure"
    assert result["checks"] == []


def test_ci_rejects_prohibited_broad_verification_command(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": verification_body("a" * 40, "python3 -m pytest -q"),
        },
    )
    result = check_ci.ci_verdict(3)
    assert result["state"] == "failure"
    assert result["checks"] == ["python3 -m pytest -q"]


def test_ci_rejects_no_execution_pytest_verification_command(monkeypatch):
    monkeypatch.setattr(
        check_ci,
        "gh_json",
        lambda _argv: {
            "number": 3,
            "headRefOid": "a" * 40,
            "body": verification_body(
                "a" * 40,
                "python3 -m pytest tests/test_ci_and_feedback.py --collect-only",
            ),
        },
    )
    result = check_ci.ci_verdict(3)
    assert result["state"] == "failure"
    assert result["checks"] == [
        "python3 -m pytest tests/test_ci_and_feedback.py --collect-only"
    ]


@pytest.mark.parametrize(
    "command",
    [
        "echo ok",
        "python3 --version",
        "CI=1 pytest tests/test_ci_and_feedback.py -q",
        "env pytest tests/test_ci_and_feedback.py -q",
        "poetry run pytest tests/test_ci_and_feedback.py -q",
        "hatch run pytest tests/test_ci_and_feedback.py -q",
        "rspec spec/foo_spec.rb",
        "jest tests/foo.test.js",
        "vitest run tests/foo.test.ts",
        "mvn test -Dtest=FooTest",
        "gradle test --tests FooTest",
        "dotnet test tests/Project.Tests.csproj --filter FullyQualifiedName=Suite",
        "bazel test //...",
        "./scripts/test.sh",
        "bash scripts/test.sh",
    ],
)
def test_local_verification_rejects_unsupported_commands(command):
    with pytest.raises(local_verification.KernelError, match="unsupported"):
        local_verification.validate_local_verification_commands([command])


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest -q",
        "pytest .",
        "python3 -m pytest tests",
        "python3 -m pytest src",
        "python3 -m pytest backend",
        "python3 -m pytest tests/*.py",
        "python3 -m pytest tests/...",
        "python3 -m unittest discover",
        "python3 -m compileall .",
        "python3 -m compileall tests",
        "go test ./... -run TestFocused",
        "cargo test",
        "npm test -- .",
    ],
)
def test_local_verification_rejects_broad_targets(command):
    with pytest.raises(local_verification.KernelError, match="broad or full-suite"):
        local_verification.validate_local_verification_commands([command])


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest tests/test_ci_and_feedback.py --collect-only",
        "pytest tests/test_ci_and_feedback.py --co",
        "python3 -m pytest tests/test_ci_and_feedback.py --collect",
        "python3 -m pytest tests/test_ci_and_feedback.py --collect_onl",
        "python3 -m pytest tests/test_ci_and_feedback.py --fixtures",
        "python3 -m pytest tests/test_ci_and_feedback.py --fixture",
        "python3 -m pytest tests/test_ci_and_feedback.py --fixtures_per_test",
        "python3 -m pytest tests/test_ci_and_feedback.py --markers",
        "python3 -m pytest tests/test_ci_and_feedback.py --marker",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup-only",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup_only",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup-on",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup-plan",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup_plan",
        "python3 -m pytest tests/test_ci_and_feedback.py --setup-pl",
        "python3 -m pytest tests/test_ci_and_feedback.py --hel",
        "python3 -m pytest tests/test_ci_and_feedback.py --ver",
    ],
)
def test_local_verification_rejects_no_execution_pytest_flags(command):
    with pytest.raises(local_verification.KernelError, match="must execute tests"):
        local_verification.validate_local_verification_commands([command])


def test_bind_local_verification_executes_commands_before_binding():
    seen: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return ok(argv)

    body, commands = local_verification.bind_local_verification(
        "## Summary\n\nExample\n\n## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n",
        "a" * 40,
        runner=runner,
    )

    assert commands == ["python3 -m pytest tests/test_ci_and_feedback.py -q"]
    assert seen == [["python3", "-m", "pytest", "tests/test_ci_and_feedback.py", "-q"]]
    payload = json.loads(body.split("<!-- aru-local-verification:v1 ", 1)[1].split(" -->", 1)[0])
    assert payload["results"] == [
        {
            "command": "python3 -m pytest tests/test_ci_and_feedback.py -q",
            "argv": ["python3", "-m", "pytest", "tests/test_ci_and_feedback.py", "-q"],
            "returncode": 0,
        }
    ]


def test_bind_local_verification_fails_on_nonzero_runner():
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    with pytest.raises(local_verification.KernelError, match="failed"):
        local_verification.bind_local_verification(
            "## Summary\n\nExample\n\n## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n",
            "a" * 40,
            runner=runner,
        )


def test_refresh_verification_preserves_existing_closes_directive(monkeypatch):
    edits: list[list[str]] = []
    monkeypatch.setattr(
        local_verification,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "headRefOid": "b" * 40,
            "body": "## Summary\n\nLive body\n\nCloses #12\n",
        },
    )
    monkeypatch.setattr(local_verification, "run", lambda argv: edits.append(argv))

    result = local_verification.refresh_verification(
        12,
        "## Summary\n\nEdited body\n\n## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n",
        runner=ok,
    )

    assert result["head"] == "b" * 40
    body = edits[0][edits[0].index("--body") + 1]
    assert "Closes #12" in body


def test_refresh_verification_rejects_conflicting_closes_directive(monkeypatch):
    monkeypatch.setattr(
        local_verification,
        "gh_json",
        lambda _argv: {
            "number": 12,
            "headRefOid": "b" * 40,
            "body": "## Summary\n\nLive body\n\nCloses #12\n",
        },
    )

    with pytest.raises(local_verification.KernelError, match="closing directive"):
        local_verification.refresh_verification(
            12,
            "## Summary\n\nEdited body\n\nCloses #99\n\n## Verification\n\n- `python3 -m pytest tests/test_ci_and_feedback.py -q`\n",
            runner=ok,
        )


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
