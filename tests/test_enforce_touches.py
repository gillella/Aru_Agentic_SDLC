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
        lambda _argv: '{"state":"OPEN","body":"touches: a.py","labels":[{"name":"status:ready"}]}',
    )
    with pytest.raises(HOOK.Refusal, match="In Progress"):
        HOOK.issue_body(1)


def test_explicit_pushed_branch_drives_issue_identity(monkeypatch):
    monkeypatch.setattr(HOOK, "issue_body", lambda number: f"touches: issue-{number}.txt")
    assert HOOK.check(["issue-12.txt"], branch="feat/issue-12-change", default_branch="trunk") == []


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


def test_pr_mode_accepts_stable_head_across_file_collection(monkeypatch):
    calls = []

    def fake_pull_request(number):
        calls.append(number)
        return {
            "headRefOid": "a" * 40,
            "body": "## Summary\n\nCloses #12\n",
        }

    monkeypatch.setattr(HOOK, "pull_request", fake_pull_request)
    monkeypatch.setattr(HOOK, "pull_changed_paths", lambda _number: ["scripts/a.py"])
    monkeypatch.setattr(
        HOOK,
        "issue_body",
        lambda _number: "touches: scripts/a.py",
    )

    violations, issue, head = HOOK.check_pull_request(9, "a" * 40)
    assert violations == []
    assert issue == 12
    assert head == "a" * 40
    assert len(calls) == 2


def test_pr_mode_refuses_when_head_changes_during_file_collection(monkeypatch):
    calls = []

    def fake_pull_request(number):
        calls.append(number)
        if len(calls) == 1:
            return {"headRefOid": "a" * 40, "body": "Closes #12\n"}
        return {"headRefOid": "b" * 40, "body": "Closes #12\n"}

    monkeypatch.setattr(HOOK, "pull_request", fake_pull_request)
    monkeypatch.setattr(HOOK, "pull_changed_paths", lambda _number: ["scripts/a.py"])
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: scripts/a.py")

    with pytest.raises(HOOK.Refusal, match="pull request head changed"):
        HOOK.check_pull_request(9, "a" * 40)


def test_pr_mode_refuses_changed_head_without_explicit_expected_head(monkeypatch):
    calls = []

    def fake_pull_request(number):
        calls.append(number)
        if len(calls) == 1:
            return {"headRefOid": "a" * 40, "body": "Closes #12\n"}
        return {"headRefOid": "b" * 40, "body": "Closes #12\n"}

    monkeypatch.setattr(HOOK, "pull_request", fake_pull_request)
    monkeypatch.setattr(HOOK, "pull_changed_paths", lambda _number: ["scripts/a.py"])
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: scripts/a.py")

    with pytest.raises(HOOK.Refusal, match="pull request head changed"):
        HOOK.check_pull_request(9)


def test_pr_mode_revalidates_head_immediately_before_return_after_eval(monkeypatch):
    events = []

    def fake_pull_request(number):
        events.append("pull_request")
        if len([e for e in events if e == "pull_request"]) == 1:
            return {"headRefOid": "a" * 40, "body": "Closes #12\n"}
        return {"headRefOid": "c" * 40, "body": "Closes #12\n"}

    def fake_pull_changed_paths(number):
        events.append("pull_changed_paths")
        return ["scripts/a.py"]

    def fake_issue_body(number):
        events.append("issue_body")
        return "touches: scripts/a.py"

    def fake_parse_touches(body):
        events.append("parse_touches")
        return ["scripts/a.py"]

    def fake_allowed(path, declared):
        events.append("allowed")
        return path in declared

    monkeypatch.setattr(HOOK, "pull_request", fake_pull_request)
    monkeypatch.setattr(HOOK, "pull_changed_paths", fake_pull_changed_paths)
    monkeypatch.setattr(HOOK, "issue_body", fake_issue_body)
    monkeypatch.setattr(HOOK, "parse_touches", fake_parse_touches)
    monkeypatch.setattr(HOOK, "allowed", fake_allowed)

    with pytest.raises(HOOK.Refusal, match="pull request head changed during file collection"):
        HOOK.check_pull_request(9, "a" * 40)

    assert events == [
        "pull_request",
        "pull_changed_paths",
        "issue_body",
        "parse_touches",
        "allowed",
        "pull_request",
    ]


def test_canonical_touches_requires_safe_declared_path_and_refuses_incomplete_candidate(
    tmp_path, monkeypatch
):
    hook_file = tmp_path / "enforce_touches.py"
    hook_file.write_text(
        Path(HOOK.__file__).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "touches.py").write_text(
        "class TouchesError(Exception):\n"
        "    pass\n\n"
        "def parse_touches(body):\n"
        "    return []\n\n"
        "def path_allowed(path, declared):\n"
        "    return True\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ARU_SDLC_HOME", raising=False)
    spec = importlib.util.spec_from_file_location("test_enforce_touches_incomplete", hook_file)
    assert spec and spec.loader
    test_hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_hook)

    with pytest.raises(test_hook.Refusal, match="canonical Aru touches parser is unavailable"):
        test_hook._canonical_touches()

    with pytest.raises(test_hook.Refusal, match="canonical Aru touches parser is unavailable"):
        test_hook.safe_path("src/foo.py")

    valid_dir = tmp_path / "valid_home"
    (valid_dir / "scripts").mkdir(parents=True)
    (valid_dir / "scripts" / "touches.py").write_text(
        "class TouchesError(Exception):\n"
        "    pass\n\n"
        "def safe_declared_path(value):\n"
        "    return value == 'valid.py'\n\n"
        "def parse_touches(body):\n"
        "    return ['valid.py']\n\n"
        "def path_allowed(path, declared):\n"
        "    return path in declared\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARU_SDLC_HOME", str(valid_dir))
    test_hook._TOUCHES = None
    assert test_hook.safe_path("valid.py") is True
    assert test_hook.safe_path("other.py") is False


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
    subprocess.run(
        ["git", "commit", "-m", "add old"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "rename to new"], cwd=tmp_path, check=True, capture_output=True
    )

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


def test_range_changed_paths_include_both_copy_sides(tmp_path, monkeypatch):
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
    (tmp_path / "original.py").write_text("def run():\n    return 100\n", encoding="utf-8")
    subprocess.run(["git", "add", "original.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add original"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "copied.py").write_text("def run():\n    return 100\n", encoding="utf-8")
    subprocess.run(["git", "add", "copied.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "copy to copied"], cwd=tmp_path, check=True, capture_output=True
    )

    monkeypatch.chdir(tmp_path)
    paths = HOOK.changed_paths("HEAD~1..HEAD")
    assert paths == ["copied.py", "original.py"]


def test_range_refuses_when_copy_source_is_outside_touches(tmp_path, monkeypatch, capsys):
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
    (tmp_path / "legacy_source.py").write_text("def compute():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "legacy_source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "copied_dest.py").write_text("def compute():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "copied_dest.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "copy"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(HOOK, "issue_body", lambda _number: "touches: copied_dest.py")
    monkeypatch.setattr(
        "sys.argv",
        [
            "enforce_touches.py",
            "--range",
            "HEAD~1..HEAD",
            "--issue",
            "548",
            "--branch",
            "fix/issue-548-copy",
            "--default-branch",
            "main",
        ],
    )

    assert HOOK.main() == 2
    assert capsys.readouterr().out == "refused: legacy_source.py is outside touches:\n"


def test_range_allows_when_both_copy_sides_are_within_touches(tmp_path, monkeypatch, capsys):
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
    (tmp_path / "legacy_source.py").write_text("def compute():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "legacy_source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "copied_dest.py").write_text("def compute():\n    return 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "copied_dest.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "copy"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        HOOK, "issue_body", lambda _number: "touches: legacy_source.py, copied_dest.py"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "enforce_touches.py",
            "--range",
            "HEAD~1..HEAD",
            "--issue",
            "548",
            "--branch",
            "fix/issue-548-copy",
            "--default-branch",
            "main",
        ],
    )

    assert HOOK.main() == 0
    assert capsys.readouterr().out == ""


def test_changed_paths_decodes_nul_diff_with_special_characters_and_renames(tmp_path, monkeypatch):
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
    (tmp_path / "old\tname.txt").write_text("old content\n", encoding="utf-8")
    (tmp_path / "file\nwith\nnewlines.txt").write_text("newline content\n", encoding="utf-8")
    (tmp_path / "café_🚀.txt").write_text("unicode content\n", encoding="utf-8")
    (tmp_path / "plain.txt").write_text("plain content\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "old\tname.txt",
            "file\nwith\nnewlines.txt",
            "café_🚀.txt",
            "plain.txt",
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)

    subprocess.run(["git", "mv", "old\tname.txt", "new\tname.txt"], cwd=tmp_path, check=True)
    (tmp_path / "file\nwith\nnewlines.txt").write_text("modified newline\n", encoding="utf-8")
    (tmp_path / "café_🚀.txt").write_text("modified unicode\n", encoding="utf-8")
    (tmp_path / "plain.txt").unlink()
    subprocess.run(
        ["git", "commit", "-a", "-m", "rename, modify, and delete"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    monkeypatch.chdir(tmp_path)
    paths = HOOK.changed_paths("HEAD~1..HEAD")
    assert paths == [
        "café_🚀.txt",
        "file\nwith\nnewlines.txt",
        "new\tname.txt",
        "old\tname.txt",
        "plain.txt",
    ]


def test_changed_paths_empty_output(monkeypatch):
    monkeypatch.setattr(HOOK, "run", lambda _argv: "")
    assert HOOK.changed_paths("HEAD~1..HEAD") == []


@pytest.mark.parametrize(
    "bad_output",
    [
        "M\x00foo.py",
        "X\x00foo.py\x00",
        "R100\x00old.py\x00",
        "C100\x00old.py\x00",
        "M\x00",
        "Rbad\x00old.py\x00new.py\x00",
        "Mfoo\x00file.py\x00",
        "\x00",
        "M\x00\x00",
        "R100\x00\x00new.py\x00",
        "R100\x00old.py\x00\x00",
        "M\x00file.py\x00extra\x00",
        "M\tfoo.py\n",
    ],
)
def test_changed_paths_malformed_nul_evidence_fails_closed(monkeypatch, bad_output):
    monkeypatch.setattr(HOOK, "run", lambda _argv: bad_output)
    with pytest.raises(HOOK.Refusal, match="changed-path evidence is malformed"):
        HOOK.changed_paths("HEAD~1..HEAD")


@pytest.mark.parametrize(
    "final_body,refused",
    [
        ("Closes #999", True),
        ("Evidence only; closing directive removed", True),
        ("Closes #12\nCloses #12", True),
        ("Closes #12\nCloses #999", True),
        ("## Evidence\nUpdated verification details\n\nCloses #12", False),
        ("Fixes #12", False),
    ],
)
def test_final_hook_semantic_authorization(monkeypatch, final_body, refused):
    import json

    reads = []

    def fake_github(argv):
        reads.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            count = sum(call[:3] == ["gh", "pr", "view"] for call in reads)
            return json.dumps({
                "number": 9, "state": "OPEN", "isDraft": False,
                "headRefOid": "a" * 40,
                "body": "Closes #12" if count == 1 else final_body,
            })
        if argv[:3] == ["gh", "repo", "view"]:
            return '{"nameWithOwner":"isolated/repo"}'
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            return '[[{"filename":"scripts/a.py"}]]'
        if argv[:3] == ["gh", "issue", "view"]:
            return json.dumps({
                "state": "OPEN",
                "body": "touches: scripts/a.py" if argv[3] == "12" else "touches: other.py",
                "labels": [{"name": "agent:writer"}, {"name": "status:in-review"}],
            })
        pytest.fail(f"unexpected command: {argv}")

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    monkeypatch.setattr(HOOK, "run", fake_github)
    if refused:
        with pytest.raises(HOOK.Refusal, match="closing issue"):
            HOOK.check_pull_request(9, "a" * 40)
    else:
        assert HOOK.check_pull_request(9, "a" * 40) == ([], 12, "a" * 40)
    assert sum(call[:3] == ["gh", "pr", "view"] for call in reads) == 2


@pytest.mark.parametrize("mutation,refused", [
    ("excluded", True), ("expanded", True), ("missing", True),
    ("duplicate", True), ("unsafe", True), ("unreadable", True),
    ("prose", False), ("format-order", False), ("stable", False),
])
def test_final_hook_issue_scope_reread(monkeypatch, mutation, refused):
    import json

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external call"))
    events = []
    body = "touches: scripts/a.py, docs/**"

    def read(argv):
        nonlocal body
        if argv[:3] == ["gh", "pr", "view"]:
            events.append("pr")
            if events.count("pr") == 2:
                body = {
                    "excluded": "touches: other.py",
                    "expanded": "touches: scripts/**, docs/**",
                    "missing": "No scope",
                    "duplicate": body + "\ntouches: other.py",
                    "unsafe": "touches: ../outside",
                    "prose": body + "\n\n## Evidence\nMore detail.",
                    "format-order": "### touches:\n\ndocs/**, scripts/a.py\n",
                }.get(mutation, body)
            return json.dumps(dict(number=9, state="OPEN", isDraft=False,
                                   headRefOid="a" * 40, body="Closes #12"))
        if argv[:3] == ["gh", "repo", "view"]:
            return '{"nameWithOwner":"owner/repo"}'
        if argv[:2] == ["gh", "api"]:
            events.append("files")
            assert argv[-1] == "repos/owner/repo/pulls/9/files?per_page=100"
            return '[[{"filename":"scripts/a.py"}]]'
        assert argv[:4] == ["gh", "issue", "view", "12"]
        events.append("issue")
        if mutation == "unreadable" and events.count("issue") == 2:
            raise HOOK.Refusal("issue is unavailable")
        return json.dumps(dict(state="OPEN", body=body, labels=[
            {"name": "status:in-review"}, {"name": "agent:codex-1"},
        ]))

    monkeypatch.setattr(HOOK, "run", read)
    if refused:
        with pytest.raises(HOOK.Refusal):
            HOOK.check_pull_request(9, "a" * 40)
    else:
        assert HOOK.check_pull_request(9, "a" * 40) == ([], 12, "a" * 40)
    assert events == ["pr", "files", "issue", "pr", "issue"]
