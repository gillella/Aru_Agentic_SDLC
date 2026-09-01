from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aru_enforce_touches",
    ROOT / "hooks" / "enforce_touches.py",
)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


def test_hook_allows_exact_and_recursive_paths():
    declared = HOOK.parse_touches("touches: scripts/a.py, docs/**")
    assert HOOK.allowed("scripts/a.py", declared)
    assert HOOK.allowed("docs/a/b.md", declared)
    assert not HOOK.allowed("scripts/b.py", declared)


def test_hook_uses_canonical_rendered_touches_section():
    declared = HOOK.parse_touches("### touches:\n\nscripts/a.py, docs/**\n")
    assert declared == ["scripts/a.py", "docs/**"]


@pytest.mark.parametrize("body", ["", "touches: ../x", "touches: /x", "touches: a\ntouches: b"])
def test_hook_rejects_missing_unsafe_or_duplicate_declarations(body):
    with pytest.raises(HOOK.Refusal):
        HOOK.parse_touches(body)


def test_hook_requires_one_claim_and_active_status(monkeypatch):
    monkeypatch.setattr(
        HOOK,
        "run",
        lambda _argv: (
            '{"state":"OPEN","body":"touches: a.py",'
            '"labels":[{"name":"status:ready"}]}'
        ),
    )
    with pytest.raises(HOOK.Refusal, match="In Progress"):
        HOOK.issue_body(1)


def test_explicit_pushed_branch_drives_issue_identity(monkeypatch):
    monkeypatch.setattr(HOOK, "issue_body", lambda number: f"touches: issue-{number}.txt")
    assert HOOK.check(
        ["issue-12.txt"], branch="feat/issue-12-change", default_branch="trunk"
    ) == []


def test_hook_refuses_the_actual_default_branch():
    with pytest.raises(HOOK.Refusal, match="default branch"):
        HOOK.check([], branch="develop", default_branch="develop")


def test_pr_mode_resolves_linked_issue_and_actual_changed_paths(monkeypatch):
    monkeypatch.setattr(
        HOOK,
        "pull_request",
        lambda _number: {
            "headRefOid": "a" * 40,
            "body": "## Summary\n\nCloses #12\n",
        },
    )
    monkeypatch.setattr(
        HOOK,
        "pull_changed_paths",
        lambda _number: ["scripts/a.py", "docs/guide.md"],
    )
    monkeypatch.setattr(
        HOOK,
        "issue_body",
        lambda number: "### touches:\n\nscripts/a.py, docs/**\n" if number == 12 else "",
    )

    violations, issue, head = HOOK.check_pull_request(9, "a" * 40)

    assert violations == []
    assert issue == 12
    assert head == "a" * 40


def test_pr_mode_refuses_actual_path_outside_linked_issue(monkeypatch):
    monkeypatch.setattr(
        HOOK,
        "pull_request",
        lambda _number: {
            "headRefOid": "a" * 40,
            "body": "Closes #12\n",
        },
    )
    monkeypatch.setattr(HOOK, "pull_changed_paths", lambda _number: ["prod.yml"])
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: scripts/a.py")
    assert HOOK.check_pull_request(9)[0] == ["prod.yml"]


@pytest.mark.parametrize("expected", ["b" * 40, "short"])
def test_pr_mode_fails_closed_on_wrong_or_malformed_expected_head(monkeypatch, expected):
    monkeypatch.setattr(
        HOOK,
        "pull_request",
        lambda _number: {
            "headRefOid": "a" * 40,
            "body": "Closes #12\n",
        },
    )
    with pytest.raises(HOOK.Refusal, match="expected head"):
        HOOK.check_pull_request(9, expected)


def test_pr_changed_paths_include_old_and_new_rename_names(monkeypatch):
    calls = []
    monkeypatch.setattr(HOOK, "repository_slug", lambda: "owner/repo")

    def command(argv):
        calls.append(argv)
        return (
            '[[{"filename":"src/new.py","previous_filename":"src/old.py"}],'
            '[{"filename":"tests/test_new.py"}]]'
        )

    monkeypatch.setattr(HOOK, "run", command)
    assert HOOK.pull_changed_paths(9) == [
        "src/new.py",
        "src/old.py",
        "tests/test_new.py",
    ]
    assert calls == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/owner/repo/pulls/9/files?per_page=100",
        ]
    ]


def test_hook_refuses_out_of_scope_deleted_path(tmp_path, monkeypatch, capsys):
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    deleted = tmp_path / "outside.txt"
    deleted.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "outside.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    deleted.unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: allowed.txt")
    monkeypatch.setattr(
        "sys.argv",
        [
            "enforce_touches.py",
            "--range",
            "HEAD",
            "--issue",
            "520",
            "--branch",
            "fix/issue-520-delete",
            "--default-branch",
            "main",
        ],
    )

    assert HOOK.main() == 2
    assert capsys.readouterr().out == "refused: outside.txt is outside touches:\n"


def test_range_changed_paths_include_both_rename_sides(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "old.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "old.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "add old"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "rename to new"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    paths = HOOK.changed_paths("HEAD~1..HEAD")
    assert paths == ["new.py", "old.py"]


def test_range_refuses_when_rename_source_is_outside_touches(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "legacy.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "legacy.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "mv", "legacy.py", "renamed.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "rename"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: renamed.py")
    monkeypatch.setattr(
        "sys.argv",
        [
            "enforce_touches.py",
            "--range",
            "HEAD~1..HEAD",
            "--issue",
            "548",
            "--branch",
            "fix/issue-548-rename",
            "--default-branch",
            "main",
        ],
    )

    assert HOOK.main() == 2
    assert capsys.readouterr().out == "refused: legacy.py is outside touches:\n"


def test_range_allows_when_both_rename_sides_are_within_touches(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "legacy.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "legacy.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "mv", "legacy.py", "renamed.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "rename"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: legacy.py, renamed.py")
    monkeypatch.setattr(
        "sys.argv",
        [
            "enforce_touches.py",
            "--range",
            "HEAD~1..HEAD",
            "--issue",
            "548",
            "--branch",
            "fix/issue-548-rename",
            "--default-branch",
            "main",
        ],
    )

    assert HOOK.main() == 0
    assert capsys.readouterr().out == ""
