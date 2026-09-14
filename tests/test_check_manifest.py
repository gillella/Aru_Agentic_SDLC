"""`hooks/check_manifest.py`: the base branch's judgement of the head's managed files.

The hook runs inside `aru-merge-policy`, which never checks the head out. Every case
here drives it through a faked `gh`, because it must read head bytes through the API
and hash them, never execute them.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aru_check_manifest", ROOT / "hooks" / "check_manifest.py"
)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)

SLUG = "owner/repo"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
MANAGED = {"AGENTS.md": b"agents\n", ".aru/verify.sh": b"verify\n",
           ".aru/factory-version": b"2.3.0\n"}


def manifest_document(files: dict[str, bytes], version: str = "2.3.0") -> str:
    return json.dumps(
        {
            "schema": HOOK.SCHEMA,
            "factory_version": version,
            "runner_profile": "self-hosted-mac",
            "files": {path: hashlib.sha256(body).hexdigest()
                      for path, body in sorted(files.items())},
        },
        indent=2, sort_keys=True,
    ) + "\n"


class Fake:
    """A `gh` stand-in keyed on argv; every head read goes through `contents`."""

    def __init__(self, head_files: dict[str, bytes], *, head=HEAD, reread_head=None):
        self.head_files = head_files
        self.head = head
        self.reread_head = reread_head
        self.payload_override: dict | None = None
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
        if argv[:3] == ["gh", "api", argv[2]] and argv[1] == "api":
            path = argv[2].split("contents/", 1)[1].split("?", 1)[0]
            path = path.replace("%2F", "/")
            if path not in self.head_files:
                raise HOOK.Refusal("HTTP 404: Not Found")
            if self.payload_override is not None:
                return json.dumps(self.payload_override)
            return json.dumps({
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(self.head_files[path]).decode(),
            })
        raise AssertionError(f"unexpected gh call: {argv}")


def scaffold_base(tmp_path: Path, manifest_text: str, version: str = "2.3.0\n") -> None:
    (tmp_path / ".aru").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".aru/manifest.json").write_text(manifest_text, encoding="utf-8")
    (tmp_path / ".aru/factory-version").write_text(version, encoding="utf-8")


def invoke(monkeypatch, capsys, fake, expected_head=HEAD) -> tuple[int, str, str]:
    monkeypatch.setattr(HOOK, "run", fake)
    monkeypatch.setattr(
        "sys.argv",
        ["check_manifest.py", "--pr", "7", "--expected-head", expected_head],
    )
    code = HOOK.main()
    captured = capsys.readouterr()
    # Refusals print their `::error::` annotation on stdout, where GitHub Actions
    # reads workflow commands; the combined stream is what a log shows.
    return code, captured.out, captured.out + captured.err


def test_matching_head_passes_and_reports_what_it_verified(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {**MANAGED, ".aru/manifest.json": document.encode()}
    code, out, _ = invoke(monkeypatch, capsys, Fake(head))
    assert code == 0
    assert "3 managed files at aaaaaaa match the base manifest" in out
    assert "factory 2.3.0, profile self-hosted-mac" in out


def test_a_differing_managed_file_refuses_and_names_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {**MANAGED, "AGENTS.md": b"rewritten\n", ".aru/manifest.json": document.encode()}
    code, _, err = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "::error::" in err
    assert "AGENTS.md: content at the head differs from the manifest" in err


def test_a_managed_file_absent_at_the_head_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {path: body for path, body in MANAGED.items() if path != ".aru/verify.sh"}
    head[".aru/manifest.json"] = document.encode()
    code, _, err = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "::error::" in err and ".aru/verify.sh is unreadable at the head" in err


def test_a_head_that_moves_during_verification_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {**MANAGED, ".aru/manifest.json": document.encode()}
    code, _, err = invoke(monkeypatch, capsys, Fake(head, reread_head=OTHER_HEAD))
    assert code == 2
    assert "pull request head changed during verification" in err


def test_an_upgrade_head_is_judged_against_its_own_manifest(tmp_path, monkeypatch, capsys):
    """The documented residual: only the Factory can authenticate that manifest."""
    monkeypatch.chdir(tmp_path)
    scaffold_base(tmp_path, manifest_document(MANAGED), version="2.3.0\n")
    upgraded = {**MANAGED, ".aru/factory-version": b"2.4.0\n", "AGENTS.md": b"new agents\n"}
    head = {**upgraded, ".aru/manifest.json": manifest_document(upgraded, "2.4.0").encode()}
    code, out, _ = invoke(monkeypatch, capsys, Fake(head))
    assert code == 0
    assert "upgrade pull request" in out and "2.3.0 -> 2.4.0" in out
    assert "verified by the Factory's merge_pr.py, not here" in out
    assert "match the head manifest" in out


def test_a_manifest_change_without_a_version_change_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scaffold_base(tmp_path, manifest_document(MANAGED))
    rewritten = {**MANAGED, "AGENTS.md": b"new agents\n"}
    head = {**rewritten, ".aru/manifest.json": manifest_document(rewritten).encode()}
    code, _, err = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "manifest changed without a factory-version change" in err


def test_a_version_change_without_a_manifest_change_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {**MANAGED, ".aru/factory-version": b"2.4.0\n",
            ".aru/manifest.json": document.encode()}
    code, _, err = invoke(monkeypatch, capsys, Fake(head))
    assert code == 2
    assert "factory-version changed without a manifest change" in err


@pytest.mark.parametrize("document", ["{not json", "{}\n"])
def test_a_malformed_base_manifest_refuses(tmp_path, monkeypatch, capsys, document):
    monkeypatch.chdir(tmp_path)
    scaffold_base(tmp_path, document)
    code, _, err = invoke(monkeypatch, capsys, Fake({**MANAGED,
                                                    ".aru/manifest.json": b"{}"}))
    assert code == 2
    assert "::error::" in err and "base branch manifest" in err


def test_a_missing_base_manifest_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, _, err = invoke(monkeypatch, capsys, Fake(dict(MANAGED)))
    assert code == 2
    assert "sync this repository from the Factory" in err


def test_a_contents_payload_without_base64_refuses(tmp_path, monkeypatch, capsys):
    """A file over 1 MiB comes back without `encoding`; a truncated body must not pass."""
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    fake = Fake({**MANAGED, ".aru/manifest.json": document.encode()})
    fake.payload_override = {"type": "file", "size": 2000000}
    code, _, err = invoke(monkeypatch, capsys, fake)
    assert code == 2
    assert "too large to read" in err


def test_an_expected_head_mismatch_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    document = manifest_document(MANAGED)
    scaffold_base(tmp_path, document)
    head = {**MANAGED, ".aru/manifest.json": document.encode()}
    code, _, err = invoke(monkeypatch, capsys, Fake(head), expected_head=OTHER_HEAD)
    assert code == 2
    assert "expected head does not match the current PR head" in err


def test_a_malformed_expected_head_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scaffold_base(tmp_path, manifest_document(MANAGED))
    code, _, err = invoke(monkeypatch, capsys, Fake(dict(MANAGED)), expected_head="short")
    assert code == 2
    assert "expected head is malformed" in err
