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
    assert HOOK.check(["issue-12.txt"], branch="feat/issue-12-change") == []


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
        ],
    )

    assert HOOK.main() == 2
    assert capsys.readouterr().out == "refused: outside.txt is outside touches:\n"
