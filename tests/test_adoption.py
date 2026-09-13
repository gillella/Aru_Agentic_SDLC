from __future__ import annotations

import json
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


def test_github_readiness_reports_whether_rules_enforce_the_approval_rule(tmp_path, monkeypatch):
    identity = json.dumps({"nameWithOwner": "Unum-Inc/consumer", "defaultBranchRef": {"name": "main"}})
    fresh = {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
             "require_last_push_approval": True}
    rules = {}

    def read(argv, cwd):
        if argv[:3] == ["gh", "repo", "view"]:
            return identity
        assert argv == ["gh", "api", "repos/Unum-Inc/consumer/rules/branches/main"]
        return rules["raw"]

    monkeypatch.setattr(check, "command", read)
    for inventory, expected in [
        ([{"type": "pull_request", "parameters": fresh}], "enforced"),
        ([{"type": "pull_request", "parameters": {**fresh, "require_last_push_approval": False}}], "not-enforced"),
        ([{"type": "pull_request", "parameters": {**fresh, "required_approving_review_count": True}}], "not-enforced"),
        ([{"type": "required_status_checks", "parameters": {}}], "not-enforced"),
        (None, "unknown"),
    ]:
        rules["raw"] = None if inventory is None else json.dumps(inventory)
        report = check.github_readiness(tmp_path, "Unum-Inc")
        assert report["approval_rule"] == expected
        assert "reviewer_configuration" not in report


# --- bringing an existing repository under the kernel -------------------------
# Adoption is not scaffolding. A repository with code and history already owns
# files the framework writes, and its .gitignore carries toolchain entries that
# must survive. Nothing existing is discarded.

import subprocess  # noqa: E402

import pytest  # noqa: E402

import consumer  # noqa: E402

PROFILE = "self-hosted-mac"


def ungoverned(tmp_path, *, files=None):
    """A real repository with history and its own files, but no governance."""
    repo = tmp_path / "existing"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.swift").write_text("print(\"hi\")\n", encoding="utf-8")
    for relative, content in (files or {}).items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
    return repo


def read(repo, relative):
    return (repo / relative).read_text(encoding="utf-8")


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    with pytest.raises(init_project.BootstrapError, match="not a git repository"):
        consumer.adopt_plan(plain, PROFILE)
    assert list(plain.iterdir()) == [], "nothing is written on the way to refusing"


def test_an_already_governed_repository_is_directed_to_sync(tmp_path):
    governed = tmp_path / "governed"
    init_project.scaffold("governed", governed, runner_profile=PROFILE)
    with pytest.raises(init_project.BootstrapError, match="already governed; use --sync"):
        consumer.adopt_plan(governed, PROFILE)


def test_the_plan_reports_everything_before_anything_is_written(tmp_path):
    repo = ungoverned(tmp_path, files={".gitignore": "*.xcuserstate\nDerivedData/\n"})
    before = sorted(p.name for p in repo.rglob("*") if p.is_file())
    plan = consumer.adopt_plan(repo, PROFILE)
    assert plan["write"], "a plan that writes nothing would be vacuous"
    assert sorted(p.name for p in repo.rglob("*") if p.is_file()) == before


def test_a_consumer_owned_file_is_kept_exactly_as_it_stands(tmp_path):
    # AruLifts' .gitignore carries Swift and Xcode entries. Replacing it with the
    # framework default would break the repository it is adopting.
    swift_ignore = "*.xcuserstate\nDerivedData/\n.build/\n"
    repo = ungoverned(tmp_path, files={".gitignore": swift_ignore})
    result = consumer.adopt(repo, PROFILE)
    assert read(repo, ".gitignore") == swift_ignore
    assert ".gitignore" in result["keep_untouched"]
    assert not (repo / ".gitignore.pre-aru").exists()


def test_an_existing_framework_file_is_preserved_beside_the_new_one(tmp_path):
    mine = "# My own agent instructions\nUse .codex and .claude.\n"
    repo = ungoverned(tmp_path, files={"AGENTS.md": mine})
    result = consumer.adopt(repo, PROFILE)
    assert "AGENTS.md" in result["preserve_existing_as"]
    assert read(repo, f"AGENTS.md{consumer.PRE_ADOPTION_SUFFIX}") == mine, "work must not be lost"
    assert read(repo, "AGENTS.md") != mine, "the framework copy is now in place"
    assert "aru-runner-profile" in read(repo, ".github/workflows/governed-pr.yml")


def test_the_repositorys_own_code_is_untouched(tmp_path):
    repo = ungoverned(tmp_path)
    consumer.adopt(repo, PROFILE)
    assert read(repo, "src/main.swift") == "print(\"hi\")\n"


def test_adoption_leaves_the_repository_in_sync(tmp_path):
    # The real proof: after adopting, the refresher must see nothing to do.
    repo = ungoverned(tmp_path, files={"AGENTS.md": "mine\n", ".gitignore": "DerivedData/\n"})
    consumer.adopt(repo, PROFILE)
    report = consumer.sync_report(repo)
    assert report["in_sync"] is True, f"stale={report['stale']} missing={report['missing']}"
    assert report["runner_profile"] == PROFILE


def test_adoption_keeps_the_profile_it_was_given(tmp_path):
    repo = ungoverned(tmp_path)
    consumer.adopt(repo, "github-hosted")
    assert "ubuntu-latest" in read(repo, ".github/workflows/governed-pr.yml")
    assert consumer.sync_report(repo)["runner_profile"] == "github-hosted"


def test_the_verifier_is_executable_after_adoption(tmp_path):
    repo = ungoverned(tmp_path)
    consumer.adopt(repo, PROFILE)
    assert os.access(repo / ".aru" / "verify.sh", os.X_OK)


def test_provisioning_is_shared_with_bootstrap_not_duplicated(monkeypatch, tmp_path):
    # Adoption and bootstrap must provision identically; a second copy of this
    # logic is how the two would drift.
    calls = []

    def fake_command(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["gh", "project"] and "create" in argv:
            return {"number": 9, "url": "u"}
        if "field-list" in argv:
            return {"fields": [{"name": "Status", "id": "F"}]}
        if argv[:3] == ["gh", "api", "repos/o/r/rulesets"]:
            return []
        return {}

    monkeypatch.setattr(init_project, "command", fake_command)
    monkeypatch.setattr(init_project.merge_authority, "configured", lambda: None)
    init_project.provision_github("o/r", "r", tmp_path, runner_profile=PROFILE, merge_app=None)
    created = [a for a in calls if a[:3] == ["gh", "label", "create"]]
    assert len(created) == len(init_project.LABELS), "every board label is created"
    assert any("project" in a and "link" in a for a in calls), "the board is linked to the repo"
