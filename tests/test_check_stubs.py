"""The base-branch stub-origin check.

`hooks/check_stubs.py` never sees a checkout of the pull request: it reads the base
branch's stubs from disk and the head's through the GitHub contents API. So the suite
drives it through a faked `gh`, the same way `tests/test_check_manifest.py` does, and
asserts on what a head may and may not change.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

import init_project
import policy

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("aru_check_stubs", ROOT / "hooks" / "check_stubs.py")
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)

SLUG = "owner/repo"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
GOVERNED = ".github/workflows/governed-pr.yml"
POLICY = ".github/workflows/merge-policy.yml"


def stubs(profile: str = "self-hosted-mac") -> dict[str, str]:
    files = init_project.framework_files(profile)
    return {GOVERNED: files[GOVERNED], POLICY: files[POLICY]}


class Fake:
    """A `gh` stand-in keyed on argv; every head read goes through `contents`."""

    def __init__(self, head_files: dict[str, str], *, head: str = HEAD, reread_head: str | None = None):
        self.head_files = head_files
        self.head = head
        self.reread_head = reread_head
        self.calls = 0

    def __call__(self, argv: list[str]) -> str:
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": SLUG})
        if argv[:3] == ["gh", "pr", "view"]:
            self.calls += 1
            head = self.head
            if self.reread_head is not None and self.calls > 1:
                head = self.reread_head
            return json.dumps({"number": int(argv[3]), "state": "OPEN",
                               "isDraft": False, "headRefOid": head})
        if argv[1] == "api":
            path = argv[2].split("contents/", 1)[1].split("?", 1)[0].replace("%2F", "/")
            if path not in self.head_files:
                raise HOOK.Refusal("HTTP 404: Not Found")
            return json.dumps({
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(self.head_files[path].encode()).decode(),
            })
        raise AssertionError(f"unexpected gh call: {argv}")


def base_branch(tmp_path: Path, files: dict[str, str] | None = None) -> None:
    for relative, content in (files or stubs()).items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def invoke(monkeypatch, capsys, fake, expected_head: str = HEAD) -> tuple[int, str]:
    monkeypatch.setattr(HOOK, "run", fake)
    monkeypatch.setattr("sys.argv", ["check_stubs.py", "--pr", "7", "--expected-head", expected_head])
    code = HOOK.main()
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_an_unchanged_head_passes_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    code, out = invoke(monkeypatch, capsys, Fake(stubs()))
    assert code == 0
    assert "2 stubs at aaaaaaa call this Factory at the base branch's pinned tag" in out


def test_only_the_pinned_tag_may_move(tmp_path, monkeypatch, capsys):
    """An upgrade is exactly this: the same stubs at a newer release."""
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    head = {p: c.replace(f"@v{policy.version()}", "@v9.9.9") for p, c in stubs().items()}
    code, out = invoke(monkeypatch, capsys, Fake(head))
    assert code == 0
    assert f"{GOVERNED}: v{policy.version()} -> v9.9.9" in out
    assert f"{POLICY}: v{policy.version()} -> v9.9.9" in out


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c.replace(init_project.FACTORY_REPOSITORY, "attacker/aru"),
         "calls attacker/aru"),
        (lambda c: c.replace("/.github/actions/governed-pr@", "/.github/actions/merge-policy@"),
         "must call <owner>/<repo>/.github/actions/governed-pr@vX.Y.Z"),
        (lambda c: c.replace("runs-on: [self-hosted, macOS, ARM64, aru-ci]", "runs-on: ubuntu-latest"),
         "changes the runs-on"),
        (lambda c: c.replace("# aru-runner-profile: self-hosted-mac", "# aru-runner-profile: github-hosted"),
         "changes the runner profile"),
        (lambda c: c.replace("  contents: read", "  contents: write"),
         "grants a write permission"),
        (lambda c: c.replace("  pull_request:", "  pull_request_target:"),
         "triggers on pull_request_target"),
        (lambda c: c.replace("        uses: gillella", "        # uses: gillella"),
         "must contain exactly one Aru action reference, found 0"),
    ],
)
def test_a_head_that_changes_anything_else_refuses(tmp_path, monkeypatch, capsys, mutate, message):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    head = dict(stubs())
    head[GOVERNED] = mutate(head[GOVERNED])
    code, out = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "::error::" in out and message in out


def test_a_second_action_reference_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    head = dict(stubs())
    head[GOVERNED] += "        uses: other/repo/.github/actions/governed-pr@v1.0.0\n"
    code, out = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "must contain exactly one Aru action reference, found 2" in out


def test_a_missing_stub_at_the_head_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    head = {p: c for p, c in stubs().items() if p != POLICY}
    code, out = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert f"{POLICY} is unreadable at the head" in out


def test_a_base_branch_without_a_stub_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path, {GOVERNED: stubs()[GOVERNED]})
    code, out = invoke(monkeypatch, capsys, Fake(stubs()))
    assert code == 2
    assert f"base branch has no readable {POLICY}" in out


def test_a_head_that_moves_during_verification_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    code, out = invoke(monkeypatch, capsys, Fake(stubs(), reread_head=OTHER_HEAD))
    assert code == 2
    assert "pull request head changed during verification" in out


def test_a_mismatched_expected_head_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    code, out = invoke(monkeypatch, capsys, Fake(stubs()), expected_head=OTHER_HEAD)
    assert code == 2
    assert "expected head does not match" in out


def test_a_malformed_expected_head_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    code, out = invoke(monkeypatch, capsys, Fake(stubs()), expected_head="not-a-sha")
    assert code == 2
    assert "expected head is malformed" in out


def test_a_commented_reference_does_not_count(tmp_path, monkeypatch, capsys):
    """The stub's own explanatory comment names the action; only active lines count."""
    monkeypatch.chdir(tmp_path)
    base_branch(tmp_path)
    head = dict(stubs())
    head[GOVERNED] = head[GOVERNED].replace(
        "on:\n", "# uses: attacker/aru/.github/actions/governed-pr@v1.0.0\n\non:\n", 1
    )
    code, out = invoke(monkeypatch, capsys, Fake(head))
    assert code == 0, out
