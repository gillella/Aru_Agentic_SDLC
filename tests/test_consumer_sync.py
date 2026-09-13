"""Bringing an already-governed repository up to the current framework.

The helpers under `$ARU_SDLC_HOME/scripts` are read live, so improving them
reaches every governed repository at once. Everything GitHub executes is a copy
taken at bootstrap: the two workflows, `.aru/verify.sh` and `.aru/lib/touches.py`.
These tests cover the path that refreshes those copies without touching what the
consumer owns.
"""

from __future__ import annotations

import pytest

import init_project

PROFILE = "self-hosted-mac"


def governed(tmp_path):
    """A freshly scaffolded, therefore perfectly current, governed repository."""
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile=PROFILE, reviewers=["someone"])
    return target


def read(target, relative):
    return (target / relative).read_text(encoding="utf-8")


# --- refusing what is not ours ------------------------------------------------

def test_an_ungoverned_directory_is_refused_not_half_converted(tmp_path):
    plain = tmp_path / "someone-elses-repo"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="not a governed repository"):
        init_project.sync_report(plain)
    # Nothing was created on the way to refusing.
    assert [p.name for p in plain.iterdir()] == ["src"]


def test_a_repository_missing_one_marker_is_still_refused(tmp_path):
    target = governed(tmp_path)
    (target / ".aru" / "verify.sh").unlink()
    with pytest.raises(init_project.BootstrapError, match="not a governed repository"):
        init_project.sync_report(target)


# --- reporting ----------------------------------------------------------------

def test_a_freshly_scaffolded_repository_is_already_in_sync(tmp_path):
    report = init_project.sync_report(governed(tmp_path))
    assert report["in_sync"] is True
    assert report["stale"] == [] and report["missing"] == []
    assert report["current"], "a vacuous report would claim sync with nothing compared"
    assert report["runner_profile"] == PROFILE


def test_a_stale_copy_is_reported(tmp_path):
    target = governed(tmp_path)
    (target / ".aru" / "verify.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    report = init_project.sync_report(target)
    assert report["stale"] == [".aru/verify.sh"]
    assert report["in_sync"] is False


def test_a_diverged_touches_helper_is_reported(tmp_path):
    # The sharpest edge: a real copy of scripts/touches.py that nothing else
    # compares, so it drifts silently for as long as the repository lives.
    target = governed(tmp_path)
    (target / ".aru" / "lib" / "touches.py").write_text("# gutted\n", encoding="utf-8")
    assert ".aru/lib/touches.py" in init_project.sync_report(target)["stale"]


def test_a_deleted_framework_file_is_reported_missing(tmp_path):
    target = governed(tmp_path)
    (target / ".github" / "workflows" / "merge-policy.yml").unlink()
    report = init_project.sync_report(target)
    assert report["missing"] == [".github/workflows/merge-policy.yml"]


def test_reporting_writes_nothing(tmp_path):
    target = governed(tmp_path)
    gutted = "#!/bin/sh\nexit 0\n"
    (target / ".aru" / "verify.sh").write_text(gutted, encoding="utf-8")
    init_project.sync_report(target)
    assert read(target, ".aru/verify.sh") == gutted, "a report must not repair what it reports"


# --- applying -----------------------------------------------------------------

def test_sync_restores_a_stale_copy(tmp_path):
    target = governed(tmp_path)
    expected = read(target, ".aru/verify.sh")
    (target / ".aru" / "verify.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    result = init_project.sync_apply(target)
    assert result["rewritten"] == [".aru/verify.sh"]
    assert read(target, ".aru/verify.sh") == expected
    assert result["in_sync"] is True


def test_sync_restores_a_missing_file_and_keeps_it_executable(tmp_path):
    target = governed(tmp_path)
    (target / ".aru" / "verify.sh").unlink()
    (target / ".aru" / "hooks" / "pre-push").unlink()
    # verify.sh is a governed marker, so recreate it first: the refusal is
    # deliberate and covered above.
    init_project.write(target, ".aru/verify.sh", "stale\n", executable=True)
    result = init_project.sync_apply(target)
    assert ".aru/hooks/pre-push" in result["rewritten"]
    import os
    assert os.access(target / ".aru" / "hooks" / "pre-push", os.X_OK)


def test_syncing_an_already_current_repository_writes_nothing(tmp_path):
    target = governed(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    result = init_project.sync_apply(target)
    assert result["rewritten"] == []
    after = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    assert before == after, "a no-op sync must not rewrite identical content"


def test_consumer_owned_files_are_never_overwritten(tmp_path):
    target = governed(tmp_path)
    mine = '{"authority": "human", "reviewers": ["someone-else"]}\n'
    (target / ".aru" / "review.json").write_text(mine, encoding="utf-8")
    (target / ".aru" / "verify-project.sh").write_text("#!/bin/sh\nmy tests\n", encoding="utf-8")
    result = init_project.sync_apply(target)
    assert read(target, ".aru/review.json") == mine
    assert "my tests" in read(target, ".aru/verify-project.sh")
    for owned in init_project.CONSUMER_OWNED:
        assert owned not in result["rewritten"]
        assert owned in result["preserved"]


def test_sync_keeps_the_profile_the_repository_declares(tmp_path):
    # Resolving the account again would relocate where verification executes.
    # A refresh must not move a repository between runners.
    target = tmp_path / "hosted"
    init_project.scaffold("hosted", target, runner_profile="github-hosted")
    (target / ".aru" / "verify.sh").write_text("stale\n", encoding="utf-8")
    result = init_project.sync_apply(target)
    assert result["runner_profile"] == "github-hosted"
    assert "ubuntu-latest" in read(target, ".github/workflows/governed-pr.yml")
    assert "self-hosted" not in read(target, ".github/workflows/governed-pr.yml")


def test_an_unreadable_profile_marker_refuses(tmp_path):
    target = governed(tmp_path)
    workflow = target / ".github" / "workflows" / "governed-pr.yml"
    workflow.write_text(workflow.read_text(encoding="utf-8").replace(
        init_project.RUNNER_PROFILE_MARKER, "# nothing: "), encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="exactly one"):
        init_project.sync_report(target)


# --- the render path is shared -------------------------------------------------

def test_scaffold_and_sync_render_from_the_same_source(tmp_path):
    # If these ever diverge, a synced repository runs a gate the framework does
    # not ship -- the exact drift this feature exists to close.
    target = governed(tmp_path)
    rendered = init_project.framework_files(PROFILE, reviewers=["someone"])
    for relative in rendered:
        assert (target / relative).is_file(), relative
        if relative not in init_project.CONSUMER_OWNED:
            assert read(target, relative) == rendered[relative], relative


def test_every_framework_file_is_owned_by_exactly_one_side(tmp_path):
    rendered = set(init_project.framework_files(PROFILE))
    consumer = set(init_project.CONSUMER_OWNED)
    assert consumer <= rendered, f"consumer-owned names not rendered: {consumer - rendered}"
    report = init_project.sync_report(governed(tmp_path))
    compared = set(report["current"]) | set(report["stale"]) | set(report["missing"])
    assert compared == rendered - consumer, "every rendered file is compared or preserved, none dropped"


# --- the overwrite guard still holds ------------------------------------------
# sync gained the right to replace stale content. It gained no right to write
# through anything that is not a regular file, and bootstrap's refusal to
# clobber must stay the default.

def test_replace_still_refuses_anything_that_is_not_a_regular_file(tmp_path):
    target = tmp_path / "repo"
    (target / ".aru").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched\n", encoding="utf-8")
    (target / ".aru" / "verify.sh").symlink_to(outside)
    with pytest.raises(init_project.BootstrapError):
        init_project.write(target, ".aru/verify.sh", "new\n", replace=True)
    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_write_without_replace_still_refuses_to_overwrite(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    init_project.write(target, "a.txt", "first\n")
    with pytest.raises(init_project.BootstrapError, match="refusing to overwrite"):
        init_project.write(target, "a.txt", "second\n")
    assert read(target, "a.txt") == "first\n"


def test_identical_content_is_never_rewritten(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    init_project.write(target, "a.txt", "same\n")
    before = (target / "a.txt").stat().st_mtime_ns
    init_project.write(target, "a.txt", "same\n", replace=True)
    assert (target / "a.txt").stat().st_mtime_ns == before


def test_sync_refuses_a_symlinked_framework_file(tmp_path):
    target = governed(tmp_path)
    outside = tmp_path / "outside.sh"
    outside.write_text("untouched\n", encoding="utf-8")
    hook = target / ".aru" / "hooks" / "pre-push"
    hook.unlink()
    hook.symlink_to(outside)
    with pytest.raises(init_project.BootstrapError):
        init_project.sync_apply(target)
    assert outside.read_text(encoding="utf-8") == "untouched\n"


# --- ruleset ------------------------------------------------------------------

def test_reprovision_replaces_the_existing_ruleset_rather_than_adding_one(monkeypatch, tmp_path):
    calls = []

    def fake_command(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "repos/o/r/rulesets"] and "--method" not in argv:
            return [{"name": init_project.policy.ruleset_parameters()["name"], "id": 42},
                    {"name": "unrelated", "id": 7}]
        return {"id": 42}

    monkeypatch.setattr(init_project, "command", fake_command)
    init_project.reprovision_ruleset("o/r", tmp_path)
    methods = [a for a in calls if "--method" in a]
    assert len(methods) == 1
    assert methods[0][methods[0].index("--method") + 1] == "PUT"
    assert "repos/o/r/rulesets/42" in methods[0]


def test_reprovision_creates_one_when_the_repository_has_none(monkeypatch, tmp_path):
    calls = []

    def fake_command(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "repos/o/r/rulesets"] and "--method" not in argv:
            return []
        return {"id": 1}

    monkeypatch.setattr(init_project, "command", fake_command)
    init_project.reprovision_ruleset("o/r", tmp_path)
    methods = [a for a in calls if "--method" in a]
    assert methods and methods[0][methods[0].index("--method") + 1] == "POST"


def test_duplicate_rulesets_are_refused_rather_than_guessed(monkeypatch, tmp_path):
    name = init_project.policy.ruleset_parameters()["name"]
    monkeypatch.setattr(init_project, "command",
                        lambda argv, **kw: [{"name": name, "id": 1}, {"name": name, "id": 2}])
    with pytest.raises(init_project.BootstrapError, match="resolve by hand"):
        init_project.reprovision_ruleset("o/r", tmp_path)


def test_an_unreadable_inventory_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(init_project, "command", lambda argv, **kw: {"unexpected": True})
    with pytest.raises(init_project.BootstrapError, match="inventory is unreadable"):
        init_project.reprovision_ruleset("o/r", tmp_path)
