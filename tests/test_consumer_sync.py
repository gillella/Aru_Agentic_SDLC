"""Bringing an already-governed repository up to the current framework.

A consumer is thin: it carries no verification logic of its own. The two
workflows are stubs calling the Factory's composite actions, and the only copies
left in the repository are those stubs, `.aru/factory-version`,
`.aru/manifest.json` and the consumer's own files. These tests cover the path
that refreshes the framework's copies, deletes the ones a consumer used to carry
and no longer needs, and touches nothing the consumer owns.
"""

from __future__ import annotations

import os

import pytest

import consumer
import init_project
import manifest

PROFILE = "self-hosted-mac"


def governed(tmp_path):
    """A freshly scaffolded, therefore perfectly current, governed repository."""
    target = tmp_path / "consumer"
    init_project.scaffold("consumer", target, runner_profile=PROFILE, reviewers=["someone"])
    return target


def with_retired_copies(tmp_path):
    """A governed repository still carrying every file the thin consumer retired.

    This is what a repository scaffolded by an older Factory looks like after the
    framework stopped shipping its verifier, touches parser and hooks.
    """
    target = governed(tmp_path)
    for relative in init_project.RETIRED:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# stale copy of {relative}\n", encoding="utf-8")
    return target


def read(target, relative):
    return (target / relative).read_text(encoding="utf-8")


# --- refusing what is not ours ------------------------------------------------

def test_an_ungoverned_directory_is_refused_not_half_converted(tmp_path):
    plain = tmp_path / "someone-elses-repo"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="not a governed repository"):
        consumer.sync_report(plain)
    # Nothing was created on the way to refusing.
    assert [p.name for p in plain.iterdir()] == ["src"]


def test_a_repository_missing_one_marker_is_still_refused(tmp_path):
    target = governed(tmp_path)
    (target / ".aru" / "verify-project.sh").unlink()
    with pytest.raises(init_project.BootstrapError, match="not a governed repository"):
        consumer.sync_report(target)


def test_both_markers_are_files_the_framework_still_ships(tmp_path):
    # A marker naming a retired path would refuse every governed repository.
    target = governed(tmp_path)
    for marker in consumer.GOVERNED_MARKERS:
        assert (target / marker).is_file(), marker
        assert marker not in init_project.RETIRED


# --- reporting ----------------------------------------------------------------

def test_a_freshly_scaffolded_repository_is_already_in_sync(tmp_path):
    report = consumer.sync_report(governed(tmp_path))
    assert report["in_sync"] is True
    assert report["stale"] == [] and report["missing"] == []
    assert report["current"], "a vacuous report would claim sync with nothing compared"
    assert report["runner_profile"] == PROFILE


def test_a_stale_copy_is_reported(tmp_path):
    target = governed(tmp_path)
    (target / ".github" / "workflows" / "merge-policy.yml").write_text(
        "name: gutted\non: pull_request\n", encoding="utf-8")
    report = consumer.sync_report(target)
    assert report["stale"] == [".github/workflows/merge-policy.yml"]
    assert report["in_sync"] is False


def test_a_diverged_factory_version_is_reported(tmp_path):
    # The sharpest edge: a copy that nothing else compares, so it drifts silently
    # for as long as the repository lives. The pinned version is what tells the
    # stubs which release of the composite actions to call.
    target = governed(tmp_path)
    (target / ".aru" / "factory-version").write_text("0.0.1\n", encoding="utf-8")
    assert ".aru/factory-version" in consumer.sync_report(target)["stale"]


def test_a_deleted_framework_file_is_reported_missing(tmp_path):
    target = governed(tmp_path)
    (target / ".github" / "workflows" / "merge-policy.yml").unlink()
    report = consumer.sync_report(target)
    assert report["missing"] == [".github/workflows/merge-policy.yml"]


def test_reporting_writes_nothing(tmp_path):
    target = with_retired_copies(tmp_path)
    gutted = "name: gutted\non: pull_request\n"
    (target / ".github" / "workflows" / "merge-policy.yml").write_text(gutted, encoding="utf-8")
    consumer.sync_report(target)
    assert read(target, ".github/workflows/merge-policy.yml") == gutted, (
        "a report must not repair what it reports")
    for relative in init_project.RETIRED:
        assert (target / relative).is_file(), "nor delete what it reports as retired"


# --- what a thin consumer no longer carries ------------------------------------
# The verifier, the touches parser and the hooks now run from the Factory. A
# repository scaffolded before that still has copies of them; two answers to
# "what does this check do" is exactly the drift sync exists to close.

def test_no_retired_path_is_rendered_or_scaffolded_any_more(tmp_path):
    rendered = set(init_project.framework_files(PROFILE))
    assert rendered.isdisjoint(init_project.RETIRED), "a retired path must not be re-rendered"
    target = governed(tmp_path)
    for relative in init_project.RETIRED:
        assert not (target / relative).exists(), relative


def test_every_retired_copy_still_present_is_reported(tmp_path):
    target = with_retired_copies(tmp_path)
    report = consumer.sync_report(target)
    assert report["retired"] == sorted(init_project.RETIRED)
    assert report["stale"] == [] and report["missing"] == [], (
        "the framework's own files are current; only the retired copies are wrong")
    assert report["in_sync"] is False, "a repository carrying a retired copy is not in sync"


def test_sync_deletes_the_retired_copies_and_prunes_what_they_emptied(tmp_path):
    target = with_retired_copies(tmp_path)
    result = consumer.sync_apply(target)
    assert result["removed"] == sorted(init_project.RETIRED)
    for relative in init_project.RETIRED:
        assert not (target / relative).exists(), relative
    assert not (target / ".aru" / "lib").exists(), "the emptied directory goes too"
    assert not (target / ".aru" / "hooks").exists()
    assert (target / ".aru" / "manifest.json").is_file(), "only what they emptied is pruned"
    assert (target / ".aru").is_dir()
    assert result["in_sync"] is True
    assert consumer.sync_report(target)["in_sync"] is True, "and it stays in sync"


def test_a_directory_at_a_retired_path_is_refused_not_deleted(tmp_path):
    # `.aru/hooks/pre-push` as a directory is a repository that reorganised its
    # own hooks. Removing it would delete consumer work, so sync refuses.
    target = governed(tmp_path)
    mine = target / ".aru" / "hooks" / "pre-push" / "mine.sh"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\nmy hook\n", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="remove it by hand"):
        consumer.sync_apply(target)
    assert read(target, ".aru/hooks/pre-push/mine.sh") == "#!/bin/sh\nmy hook\n"


def test_a_symlink_at_a_retired_path_is_refused_not_followed(tmp_path):
    target = governed(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("someone else's file\n", encoding="utf-8")
    link = target / ".aru" / "lib" / "touches.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    with pytest.raises(init_project.BootstrapError):
        consumer.sync_apply(target)
    assert outside.read_text(encoding="utf-8") == "someone else's file\n", (
        "a sync must never follow a link out of the repository"
    )
    assert link.is_symlink(), "and the link itself is left for the operator"


# --- applying -----------------------------------------------------------------

def test_sync_restores_a_stale_copy(tmp_path):
    target = governed(tmp_path)
    expected = read(target, ".github/workflows/merge-policy.yml")
    (target / ".github" / "workflows" / "merge-policy.yml").write_text(
        "name: gutted\non: pull_request\n", encoding="utf-8")
    result = consumer.sync_apply(target)
    assert result["rewritten"] == [".github/workflows/merge-policy.yml"]
    assert read(target, ".github/workflows/merge-policy.yml") == expected
    assert result["in_sync"] is True


def test_sync_restores_a_missing_file(tmp_path):
    target = governed(tmp_path)
    expected = read(target, ".aru/factory-version")
    (target / ".aru" / "factory-version").unlink()
    (target / ".github" / "workflows" / "merge-policy.yml").unlink()
    result = consumer.sync_apply(target)
    assert result["rewritten"] == [".aru/factory-version",
                                   ".github/workflows/merge-policy.yml"]
    assert read(target, ".aru/factory-version") == expected
    assert result["in_sync"] is True


def test_sync_writes_each_file_with_the_mode_the_framework_declares(tmp_path):
    # Only the project verifier is executable now. A restored workflow must not
    # come back with the bit set, and the one executable file must keep it.
    assert init_project.EXECUTABLE == (".aru/verify-project.sh",)
    target = governed(tmp_path)
    (target / ".github" / "workflows" / "merge-policy.yml").unlink()
    consumer.sync_apply(target)
    assert not os.access(target / ".github" / "workflows" / "merge-policy.yml", os.X_OK)
    assert os.access(target / ".aru" / "verify-project.sh", os.X_OK)


def test_syncing_an_already_current_repository_writes_nothing(tmp_path):
    target = governed(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    result = consumer.sync_apply(target)
    assert result["rewritten"] == []
    after = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    assert before == after, "a no-op sync must not rewrite identical content"


def test_consumer_owned_files_are_never_overwritten(tmp_path):
    target = governed(tmp_path)
    mine = '{"authority": "human", "reviewers": ["someone-else"]}\n'
    (target / ".aru" / "review.json").write_text(mine, encoding="utf-8")
    (target / ".aru" / "verify-project.sh").write_text("#!/bin/sh\nmy tests\n", encoding="utf-8")
    result = consumer.sync_apply(target)
    assert read(target, ".aru/review.json") == mine
    assert "my tests" in read(target, ".aru/verify-project.sh")
    for owned in init_project.CONSUMER_OWNED:
        assert owned not in result["rewritten"]
        assert owned in result["preserved"]


def test_the_github_templates_a_consumer_edited_are_never_rewritten(tmp_path):
    # The issue form and the pull request template became consumer-owned with the
    # thin consumer: a repository tailors them, and a sync that reverted the edit
    # every release would train the operator to stop syncing.
    target = governed(tmp_path)
    my_pr_template = "## What changed\n\n## Screenshots\n"
    my_issue_form = "name: Task\ndescription: ours\nbody: []\n"
    (target / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(
        my_pr_template, encoding="utf-8")
    (target / ".github" / "ISSUE_TEMPLATE" / "governed-task.yml").write_text(
        my_issue_form, encoding="utf-8")
    result = consumer.sync_apply(target)
    assert read(target, ".github/PULL_REQUEST_TEMPLATE.md") == my_pr_template
    assert read(target, ".github/ISSUE_TEMPLATE/governed-task.yml") == my_issue_form
    assert result["rewritten"] == [], "neither edit makes the repository stale"
    assert result["in_sync"] is True
    for owned in (".github/PULL_REQUEST_TEMPLATE.md",
                  ".github/ISSUE_TEMPLATE/governed-task.yml"):
        assert owned in result["preserved"]


def test_sync_restores_the_governance_block_and_keeps_the_consumers_own_text(tmp_path):
    # AGENTS.md is managed as a marked block, not as a file. A consumer writes its
    # own instructions around the markers and a sync must return with them intact.
    target = governed(tmp_path)
    begin, end = manifest.BLOCKS["AGENTS.md"]
    mine_before = "# AruLifts agent notes\n\n"
    mine_after = "\n## House rules\n\nSwiftLint must pass before review.\n"
    (target / "AGENTS.md").write_text(
        f"{mine_before}{begin}\ngutted by hand\n{end}\n{mine_after}", encoding="utf-8")
    result = consumer.sync_apply(target)
    assert result["rewritten"] == ["AGENTS.md"], "the block itself was stale"
    after = read(target, "AGENTS.md")
    assert after.startswith(mine_before), "text before the BEGIN marker is the consumer's"
    assert after.endswith(mine_after), "text after the END marker is the consumer's"
    assert "gutted by hand" not in after, "the managed block was restored"
    assert consumer.managed_block(after, "AGENTS.md") == \
        init_project.framework_files(PROFILE, reviewers=["someone"])["AGENTS.md"]
    assert consumer.sync_report(target)["in_sync"] is True


def test_a_consumers_text_outside_the_markers_never_makes_agents_md_stale(tmp_path):
    target = governed(tmp_path)
    mine = "\n## House rules\n\nSwiftLint must pass before review.\n"
    (target / "AGENTS.md").write_text(read(target, "AGENTS.md") + mine, encoding="utf-8")
    report = consumer.sync_report(target)
    assert "AGENTS.md" in report["current"] and report["in_sync"] is True
    assert read(target, "AGENTS.md").endswith(mine)


def test_agents_md_without_its_markers_is_refused_rather_than_guessed(tmp_path):
    # Guessing which lines the Factory owns is how a sync deletes a consumer's
    # own instructions.
    target = governed(tmp_path)
    (target / "AGENTS.md").write_text("all mine now\n", encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="managed block"):
        consumer.sync_report(target)
    assert read(target, "AGENTS.md") == "all mine now\n"


def test_sync_keeps_the_profile_the_repository_declares(tmp_path):
    # Resolving the account again would relocate where verification executes.
    # A refresh must not move a repository between runners.
    target = tmp_path / "hosted"
    init_project.scaffold("hosted", target, runner_profile="github-hosted")
    (target / ".github" / "workflows" / "merge-policy.yml").write_text(
        "name: gutted\non: pull_request\n", encoding="utf-8")
    result = consumer.sync_apply(target)
    assert result["runner_profile"] == "github-hosted"
    assert "ubuntu-latest" in read(target, ".github/workflows/governed-pr.yml")
    assert "self-hosted" not in read(target, ".github/workflows/governed-pr.yml")


def test_an_unreadable_profile_marker_refuses(tmp_path):
    target = governed(tmp_path)
    workflow = target / ".github" / "workflows" / "governed-pr.yml"
    workflow.write_text(workflow.read_text(encoding="utf-8").replace(
        consumer.RUNNER_PROFILE_MARKER, "# nothing: "), encoding="utf-8")
    with pytest.raises(init_project.BootstrapError, match="exactly one"):
        consumer.sync_report(target)


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
    owned = set(init_project.CONSUMER_OWNED)
    assert owned <= rendered, f"consumer-owned names not rendered: {owned - rendered}"
    report = consumer.sync_report(governed(tmp_path))
    compared = set(report["current"]) | set(report["stale"]) | set(report["missing"])
    assert compared == rendered - owned, "every rendered file is compared or preserved, none dropped"


# --- the overwrite guard still holds ------------------------------------------
# sync gained the right to replace stale content. It gained no right to write
# through anything that is not a regular file, and bootstrap's refusal to
# clobber must stay the default.

def test_replace_still_refuses_anything_that_is_not_a_regular_file(tmp_path):
    target = tmp_path / "repo"
    (target / ".aru").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched\n", encoding="utf-8")
    (target / ".aru" / "factory-version").symlink_to(outside)
    with pytest.raises(init_project.BootstrapError):
        init_project.write(target, ".aru/factory-version", "new\n", replace=True)
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
    outside = tmp_path / "outside.yml"
    outside.write_text("untouched\n", encoding="utf-8")
    workflow = target / ".github" / "workflows" / "merge-policy.yml"
    workflow.unlink()
    workflow.symlink_to(outside)
    with pytest.raises(init_project.BootstrapError):
        consumer.sync_apply(target)
    assert outside.read_text(encoding="utf-8") == "untouched\n"


# --- ruleset ------------------------------------------------------------------

def ruleset_api(monkeypatch, rulesets):
    """Model GitHub's two-step ruleset API and record every call.

    The list endpoint returns no rules at all, so identifying a ruleset by what
    it requires costs one fetch each. A stub that served rules from the list
    would let the code pass while doing something GitHub does not support.

    rulesets: [(id, name, [required contexts])]
    """
    calls: list[list[str]] = []

    def fake_command(argv, **kw):
        calls.append(argv)
        if "--method" in argv:
            return {"id": 999}
        path = argv[2]
        if path.endswith("/rulesets"):
            return [{"id": i, "name": n, "target": "branch"} for i, n, _ in rulesets]
        wanted = int(path.rsplit("/", 1)[1])
        contexts = next(c for i, _, c in rulesets if i == wanted)
        return {"rules": [
            {"type": "pull_request", "parameters": {}},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": c} for c in contexts]}},
        ]}

    monkeypatch.setattr(init_project, "command", fake_command)
    return calls


def writes(calls):
    return [a for a in calls if "--method" in a]


def method_of(argv):
    return argv[argv.index("--method") + 1]


def test_the_matching_context_is_one_the_policy_actually_declares():
    assert init_project.GOVERNED_CONTEXT in init_project.policy.GOVERNED_CHECKS


def test_a_ruleset_from_an_earlier_declared_name_is_updated_not_duplicated(monkeypatch, tmp_path):
    # The v2.2.0 defect, reproduced from gillella/aru-golden-path-demo: scaffolded
    # 2026-09-10 under the old name, requiring only the one context. Matching on
    # the declared name found nothing and the create path added a second ruleset
    # enforcing beside the first.
    calls = ruleset_api(monkeypatch, [(22611728, "aru-protect-default", ["aru-governed-pr"])])
    init_project.reprovision_ruleset("o/r", tmp_path)
    assert len(writes(calls)) == 1, "a second ruleset must never be created"
    assert method_of(writes(calls)[0]) == "PUT"
    assert "repos/o/r/rulesets/22611728" in writes(calls)[0]


def test_the_current_ruleset_is_still_matched(monkeypatch, tmp_path):
    calls = ruleset_api(monkeypatch, [
        (42, "aru-protect-main", ["aru-governed-pr", "aru-merge-policy", "aru-merge-authorized"]),
    ])
    init_project.reprovision_ruleset("o/r", tmp_path)
    assert method_of(writes(calls)[0]) == "PUT"
    assert "repos/o/r/rulesets/42" in writes(calls)[0]


def test_an_unrelated_ruleset_is_never_touched(monkeypatch, tmp_path):
    calls = ruleset_api(monkeypatch, [(7, "release-protection", ["build", "lint"])])
    init_project.reprovision_ruleset("o/r", tmp_path)
    assert method_of(writes(calls)[0]) == "POST", "ours is absent, so it is created"
    assert not any("rulesets/7" in a for a in writes(calls)), "someone else's rule stays untouched"


def test_reprovision_creates_one_when_the_repository_has_none(monkeypatch, tmp_path):
    calls = ruleset_api(monkeypatch, [])
    init_project.reprovision_ruleset("o/r", tmp_path)
    assert writes(calls) and method_of(writes(calls)[0]) == "POST"


def test_two_governed_rulesets_are_refused_rather_than_guessed(monkeypatch, tmp_path):
    ruleset_api(monkeypatch, [(1, "aru-protect-default", ["aru-governed-pr"]),
                              (2, "aru-protect-main", ["aru-governed-pr"])])
    with pytest.raises(init_project.BootstrapError, match="resolve by hand"):
        init_project.reprovision_ruleset("o/r", tmp_path)


def test_a_tag_ruleset_is_not_mistaken_for_the_branch_boundary(monkeypatch, tmp_path):
    def fake_command(argv, **kw):
        if "--method" in argv:
            return {"id": 999}
        if argv[2].endswith("/rulesets"):
            return [{"id": 5, "name": "tags", "target": "tag"}]
        raise AssertionError("a tag ruleset must not be fetched")

    monkeypatch.setattr(init_project, "command", fake_command)
    assert init_project.governed_ruleset_id("o/r", tmp_path) is None


def test_an_unreadable_inventory_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(init_project, "command", lambda argv, **kw: {"unexpected": True})
    with pytest.raises(init_project.BootstrapError, match="inventory is unreadable"):
        init_project.reprovision_ruleset("o/r", tmp_path)


def test_an_unreadable_ruleset_refuses(monkeypatch, tmp_path):
    def fake_command(argv, **kw):
        if argv[2].endswith("/rulesets"):
            return [{"id": 3, "name": "x", "target": "branch"}]
        return {"no": "rules"}

    monkeypatch.setattr(init_project, "command", fake_command)
    with pytest.raises(init_project.BootstrapError, match="is unreadable"):
        init_project.reprovision_ruleset("o/r", tmp_path)
