from __future__ import annotations

import os

from integrations.adoption import check
import init_project


def test_inspection_detects_unconfigured_consumer_without_executing_it(tmp_path):
    repo = tmp_path / "consumer"
    init_project.scaffold("consumer", repo, runner_profile="github-hosted")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    report = check.inspect(repo, "Unum-Inc")
    assert report["status"] == "attention"
    assert report["verification"]["status"] == "unconfigured"
    assert report["adoption_proven"] is False
    after = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert after == before


def test_inspection_reports_custom_checks_without_running_them(tmp_path):
    repo = tmp_path / "consumer"
    init_project.scaffold("consumer", repo, runner_profile="github-hosted")
    verifier = repo / ".aru/verify-project.sh"
    verifier.write_text("#!/bin/sh\ntouch should-not-exist\nexit 1\n")
    verifier.chmod(0o755)
    report = check.inspect(repo, "Unum-Inc")
    assert report["status"] == "local-files-match"
    assert report["verification"]["status"] == "configured-not-executed"
    assert not (repo / "should-not-exist").exists()
    (repo / "AGENTS.md").write_text("Consumer custom policy\n")
    report = check.inspect(repo, "Unum-Inc")
    assert report["status"] == "attention"
    assert report["files"][0]["status"] == "differs-review-customizations"


def test_inspection_refuses_symlinked_verifier(tmp_path):
    repo = tmp_path / "consumer"
    init_project.scaffold("consumer", repo, runner_profile="github-hosted")
    target = repo / ".aru/verify-project.sh"
    target.unlink()
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\nexit 0\n")
    target.symlink_to(outside)
    assert check.verification(repo)["status"] == "missing-or-unreadable"


def test_unknown_canonical_status_is_not_reported_clean(tmp_path, monkeypatch):
    repo = tmp_path / "consumer"
    init_project.scaffold("consumer", repo, runner_profile="github-hosted")
    original = check.command

    def unavailable_status(argv, cwd):
        return None if "status" in argv else original(argv, cwd)

    monkeypatch.setattr(check, "command", unavailable_status)
    assert check.inspect(repo, "Unum-Inc")["canonical_dirty"] is None


def test_inspection_refuses_fifo_and_oversized_verifier(tmp_path):
    repo = tmp_path / "consumer"
    init_project.scaffold("consumer", repo, runner_profile="github-hosted")
    target = repo / ".aru/verify-project.sh"
    target.unlink()
    os.mkfifo(target)
    assert check.verification(repo)["status"] == "missing-or-unreadable"
    target.unlink()
    target.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert check.verification(repo)["status"] == "missing-or-unreadable"
