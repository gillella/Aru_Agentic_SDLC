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
    # Looked up by path: the managed set is sorted now, so position is not stable.
    entry = next(item for item in report["files"] if item["path"] == "AGENTS.md")
    assert entry["status"] == "differs-review-customizations"


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

import argparse  # noqa: E402
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


# --- the adoption commit reaches the default branch first ---------------------
# aru-merge-policy runs the base branch's copy of a workflow that adoption is
# itself installing. Provisioning a ruleset before that commit lands seals the
# repository against the very change that would govern it -- observed on
# gillella/AruLifts, which needed the ruleset disabled and a --no-verify push
# to recover.

def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def with_remote(tmp_path):
    """An ungoverned repository with history and a real bare origin."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    repo = ungoverned(tmp_path)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"),
                       ("commit.gpgsign", "false")):
        git(repo, "config", key, value)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "existing work")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "HEAD:main")
    return repo, bare


def test_adoption_commits_what_it_wrote(tmp_path):
    repo, _ = with_remote(tmp_path)
    result = consumer.adopt(repo, PROFILE)
    assert result["commit"], "adoption must not leave the tree dirty"
    assert git(repo, "status", "--porcelain") == "", "nothing may be left uncommitted"
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert ".aru/verify.sh" in committed
    assert ".github/workflows/merge-policy.yml" in committed


def test_only_the_adopted_paths_are_committed(tmp_path):
    # The repository may have unrelated work in progress. Sweeping it into the
    # governance commit would be a surprising thing to do to someone's tree.
    repo, _ = with_remote(tmp_path)
    (repo / "src" / "wip.swift").write_text("// mine, not yours\n", encoding="utf-8")
    consumer.adopt(repo, PROFILE)
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    # Both halves matter: without the first this passes against code that never
    # commits at all, which is the defect being fixed.
    assert ".aru/verify.sh" in committed, "the adoption files must be in this commit"
    assert "src/wip.swift" not in committed
    assert "src/wip.swift" in git(repo, "status", "--porcelain")


def test_nothing_to_write_means_nothing_to_commit(tmp_path):
    repo, _ = with_remote(tmp_path)
    consumer.adopt(repo, PROFILE)
    before = git(repo, "rev-parse", "HEAD")
    # Adoption refuses a second time, but the underlying commit step must be a
    # no-op rather than an empty commit.
    plan = {"write": [], "preserve_existing_as": []}
    assert consumer.commit_adoption(repo, plan) is None
    assert git(repo, "rev-parse", "HEAD") == before


def test_the_freshly_installed_hook_does_not_refuse_the_adoption_push(tmp_path):
    # adopt() installs a pre-push hook that refuses direct pushes to the default
    # branch. This is the commit that installs it, so it must still get through.
    repo, bare = with_remote(tmp_path)
    consumer.adopt(repo, PROFILE)
    consumer.push_adoption(repo)
    remote_head = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=bare,
                                 capture_output=True, text=True).stdout.strip()
    assert remote_head == git(repo, "rev-parse", "HEAD")
    assert ".aru/hooks/pre-push" in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_without_github_the_commit_is_left_unpushed_for_inspection(tmp_path):
    repo, bare = with_remote(tmp_path)
    before = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=bare,
                            capture_output=True, text=True).stdout.strip()
    consumer.adopt(repo, PROFILE)
    after = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=bare,
                           capture_output=True, text=True).stdout.strip()
    assert after == before, "adopt() alone must not push"
    assert git(repo, "status", "--porcelain") == "", "but it must still commit"


def test_the_push_happens_before_provisioning(monkeypatch, tmp_path):
    # The defect was ordering, so the test is about ordering.
    repo, _ = with_remote(tmp_path)
    order = []
    monkeypatch.setattr(consumer, "adopt",
                        lambda d, p: {"directory": str(repo), "runner_profile": p,
                                      "write": [], "preserve_existing_as": [],
                                      "keep_untouched": [], "commit": "abc1234"})
    monkeypatch.setattr(consumer, "push_adoption",
                        lambda d: (order.append("push"), "main")[1])
    monkeypatch.setattr(consumer, "provision_github",
                        lambda *a, **k: (order.append("provision"), {"repository": "o/r"})[1])
    monkeypatch.setattr(consumer, "checkout_repository", lambda d: "o/r")
    monkeypatch.setattr(consumer.merge_authority, "configured", lambda: None)
    args = argparse.Namespace(name=None, sync=False, owner=None, runner_profile=PROFILE,
                              directory=repo, check=False, github=True, json=False,
                              ruleset=False, merge_app_id=None)
    consumer.run_adopt(args)
    assert order == ["push", "provision"], f"provisioning must come second, got {order}"


def test_a_provisioning_failure_names_the_recovery(monkeypatch, tmp_path):
    repo, _ = with_remote(tmp_path)
    monkeypatch.setattr(consumer, "adopt",
                        lambda d, p: {"directory": str(repo), "runner_profile": p,
                                      "write": [], "preserve_existing_as": [],
                                      "keep_untouched": [], "commit": "abc1234"})
    monkeypatch.setattr(consumer, "push_adoption", lambda d: "main")
    monkeypatch.setattr(consumer, "checkout_repository", lambda d: "o/r")
    monkeypatch.setattr(consumer.merge_authority, "configured", lambda: None)

    def boom(*a, **k):
        raise init_project.BootstrapError("403 not accessible")

    monkeypatch.setattr(consumer, "provision_github", boom)
    args = argparse.Namespace(name=None, sync=False, owner=None, runner_profile=PROFILE,
                              directory=repo, check=False, github=True, json=False,
                              ruleset=False, merge_app_id=None)
    with pytest.raises(init_project.BootstrapError, match=r"--sync --ruleset"):
        consumer.run_adopt(args)
