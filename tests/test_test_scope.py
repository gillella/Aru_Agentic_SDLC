from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import test_scope


@pytest.fixture(autouse=True)
def isolate_fixture_repository_from_ci_checkout(monkeypatch):
    """Temporary repositories have their own commits, not the server checkout head."""
    monkeypatch.delenv("ARU_TEST_BASE", raising=False)
    monkeypatch.delenv("ARU_EXPECTED_HEAD", raising=False)


def write(root: Path, path: str, content: str = "def test_ok(): pass\n") -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)


@pytest.fixture
def tree(tmp_path):
    for path in test_scope.INVARIANTS:
        write(tmp_path, path)
    write(tmp_path, "scripts/reporting.py", "def answer(): return 1\n")
    write(tmp_path, "scripts/caller.py", "import reporting\ndef call(): return reporting.answer()\n")
    write(tmp_path, "tests/test_reporting.py", "import reporting\ndef test_answer(): assert reporting.answer() == 1\n")
    write(tmp_path, "tests/test_caller.py", "import caller\ndef test_call(): assert caller.call() == 1\n")
    write(tmp_path, "tests/test_other.py")
    write(tmp_path, "Makefile", "TEST_SOURCE_ROOTS := scripts tests\n")
    return tmp_path


def test_changed_behavior_selects_transitive_caller_tests_and_invariants(tree):
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "focused"
    assert set(result["tests"]) == set(test_scope.INVARIANTS) | {
        "tests/test_reporting.py", "tests/test_caller.py"
    }
    assert result["changed_paths"] == ["scripts/reporting.py"]
    assert len(result["rationale"]) == 2


def test_changed_and_new_tests_are_selected(tree):
    write(tree, "tests/test_new.py")
    result = test_scope.select(tree, {"tests/test_reporting.py", "tests/test_new.py"})
    assert result["mode"] == "focused"
    assert {"tests/test_reporting.py", "tests/test_new.py"} <= set(result["tests"])


def test_literal_subprocess_or_dynamic_module_reference_selects_caller(tree):
    write(tree, "tests/test_invocation.py", 'command = "scripts/reporting.py"\n')
    assert "tests/test_invocation.py" in test_scope.select(tree, {"scripts/reporting.py"})["tests"]


@pytest.mark.parametrize("path", [
    "scripts/common.py", "scripts/merge_pr.py", "scripts/test_scope.py", "tests/conftest.py",
    ".github/workflows/governed-pr.yml", "Makefile", "scripts/policy.toml", "README.md",
    "CHANGELOG.md", "templates/manifests/self-hosted-mac.json", "integrations/auth.py",
    "unrecognized/file.py", "tests/test_secret_scan.py", "../escape.py", "-flag.py",
])
def test_shared_sensitive_release_and_unknown_paths_force_full(tree, path):
    result = test_scope.select(tree, {path})
    assert result["mode"] == "full"
    assert result["tests"] == ["tests"]


def test_empty_diff_runs_full(tree):
    assert test_scope.select(tree, set())["mode"] == "full"


@pytest.mark.parametrize("path", ["scripts/removed.py", "tests/test_removed.py"])
def test_deleted_and_renamed_away_files_require_full(tree, path):
    assert test_scope.select(tree, {path})["mode"] == "full"


def test_missing_behavior_mapping_cannot_be_waived_by_edited_test(tree):
    write(tree, "scripts/uncovered.py", "def work(): return 5\n")
    assert test_scope.select(tree, {"scripts/uncovered.py", "tests/test_other.py"})["mode"] == "full"


def test_sensitive_transitive_caller_requires_full(tree):
    write(tree, "scripts/merge_authority.py", "import reporting\n")
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_unreadable_python_dependency_map_falls_upward(tree):
    write(tree, "scripts/broken.py", "broken = (\n")
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "full"
    assert "unreadable dependency" in result["rationale"][0]


def test_missing_mandatory_test_falls_upward(tree):
    (tree / test_scope.INVARIANTS[0]).unlink()
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_explicit_release_forces_full_even_for_ordinary_story(tree):
    result = test_scope.select(tree, {"scripts/reporting.py"}, release=True)
    assert result["mode"] == "full"
    assert "release" in result["rationale"][0]


def test_nul_diff_retains_both_rename_ends():
    assert test_scope.changed_paths(b"R100\0scripts/old.py\0scripts/new.py\0M\0tests/test_x.py\0") == {
        "scripts/old.py", "scripts/new.py", "tests/test_x.py"
    }


@pytest.mark.parametrize("raw", [b"M\0file.py", b"BAD\0file.py\0", b"R100\0one.py\0",
                                       b"M\0../escape.py\0", b"M\0\xff.py\0"])
def test_malformed_diff_refuses(raw):
    with pytest.raises((ValueError, UnicodeError)):
        test_scope.changed_paths(raw)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root).decode().strip()


@pytest.fixture
def repository(tree):
    git(tree, "init", "-q")
    git(tree, "config", "user.email", "fixture@example.invalid")
    git(tree, "config", "user.name", "Fixture")
    git(tree, "add", ".")
    git(tree, "commit", "-qm", "base")
    return tree, git(tree, "rev-parse", "HEAD")


def test_actual_diff_includes_committed_staged_unstaged_and_untracked(repository):
    root, base = repository
    write(root, "scripts/reporting.py", "def answer(): return 2\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "change")
    write(root, "tests/test_caller.py", "import caller\ndef test_new(): pass\n")
    git(root, "add", "tests/test_caller.py")
    write(root, "tests/test_reporting.py", "import reporting\ndef test_changed(): pass\n")
    write(root, "tests/test_untracked.py")
    assert test_scope.collect_changes(root, base) == {
        "scripts/reporting.py", "tests/test_caller.py", "tests/test_reporting.py", "tests/test_untracked.py"
    }
    assert test_scope.plan(root, base, git(root, "rev-parse", "HEAD"), False)["mode"] == "focused"


def test_actual_rename_includes_old_path_and_forces_full(repository):
    root, base = repository
    git(root, "mv", "scripts/reporting.py", "scripts/renamed.py")
    changes = test_scope.collect_changes(root, base)
    assert {"scripts/reporting.py", "scripts/renamed.py"} <= changes
    assert test_scope.select(root, changes)["mode"] == "full"


def test_missing_base_falls_to_full_not_empty_success(repository):
    root, _base = repository
    result = test_scope.plan(root, "missing-ref", None, False)
    assert result["mode"] == "full" and result["tests"] == ["tests"]


def test_exact_head_mismatch_refuses_even_when_base_unreadable(repository):
    root, _base = repository
    with pytest.raises(ValueError, match="exact-head"):
        test_scope.plan(root, "missing-ref", "0" * 40, False)


def test_execution_uses_subprocess_arguments_not_shell(tree, monkeypatch):
    calls = []
    monkeypatch.setattr(test_scope.subprocess, "call", lambda argv, **kwargs: calls.append((argv, kwargs)) or 7)
    document = test_scope.select(tree, {"scripts/reporting.py"})
    assert test_scope.execute(document, tree) == 7
    assert calls[0][0][1:4] == ["-m", "pytest", "-q"]
    assert calls[0][0][4:] == document["tests"]
    assert calls[0][1] == {"cwd": tree}


@pytest.mark.parametrize("document", [
    {"mode": "focused", "tests": []}, {"mode": "full", "tests": ["tests/test_reporting.py"]},
    {"mode": "focused", "tests": ["tests/test_reporting.py"]},
    {"mode": "focused", "tests": [*test_scope.INVARIANTS, "../escape"]},
])
def test_execution_rejects_empty_narrowed_full_or_missing_invariants(tree, document):
    with pytest.raises(ValueError):
        test_scope.execute(document, tree)


def test_cli_records_selection_and_release_fixture_executes_every_test(repository, monkeypatch, capsys):
    root, base = repository
    monkeypatch.chdir(root)
    plan_file = root / "plan.json"
    assert test_scope.main(["--base", base, "--release", "--plan-file", str(plan_file)]) == 0
    document = json.loads(plan_file.read_text())
    assert document["mode"] == "full" and document["tests"] == ["tests"]
    assert "release" in capsys.readouterr().out
    # This real fixture suite exercises full execution, including unrelated regressions.
    write(root, "tests/conftest.py", "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))\n")
    write(root, "tests/test_rollback.py", "def test_rollback(): assert False, 'rollback regression'\n")
    assert test_scope.execute(document, root) == 1


def test_factory_local_ci_and_release_paths_preserve_required_checks():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/governed-pr.yml").read_text()
    makefile = (root / "Makefile").read_text()
    assert "test-story: prerequisites" in makefile and "scripts/test_scope.py --run" in makefile
    assert "verify-release: verify" in makefile and "$(PYTHON) -m pytest -q" in makefile
    assert "scripts/test_scope.py --plan-file" in workflow
    assert "scripts/test_scope.py --execute-plan" in workflow
    assert 'if [[ "$mode" == "full" ]]' in workflow and '" -m pytest -q' in workflow
    assert "ARU_TEST_BASE: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "ARU_EXPECTED_HEAD: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "hooks/enforce_touches.py" in workflow
    assert "persist-credentials: false" in workflow


def test_focused_fixture_executes_callers_and_invariants_without_unrelated_suite(tree):
    document = test_scope.select(tree, {"scripts/reporting.py"})
    assert document["mode"] == "focused"
    write(tree, "tests/conftest.py", "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))\n")
    write(tree, "tests/test_other.py", "def test_unrelated(): assert False\n")
    assert test_scope.execute(document, tree) == 0


def test_unknown_dynamic_import_cannot_prove_a_focused_map(tree):
    write(tree, "scripts/dynamic.py", "import importlib\nimportlib.import_module(target)\n")
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "full" and "unreadable dependency" in result["rationale"][0]


def test_symlinked_changed_source_falls_upward(tree):
    (tree / "scripts/link.py").symlink_to(tree / "scripts/reporting.py")
    assert test_scope.select(tree, {"scripts/link.py"})["mode"] == "full"


def test_unmerged_diff_status_is_not_focused_evidence():
    with pytest.raises(ValueError):
        test_scope.changed_paths(b"U\0scripts/reporting.py\0")


def test_saved_plan_cannot_waive_later_release_or_shared_changes(repository, monkeypatch):
    root, base = repository
    write(root, "scripts/reporting.py", "def answer(): return 2\n")
    document = test_scope.plan(root, base, None, False)
    assert document["mode"] == "focused"
    plan_file = root.parent / (root.name + "-selection.json")
    plan_file.write_text(json.dumps(document))
    write(root, "README.md", "release changes\n")
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc:
        test_scope.main(["--base", base, "--execute-plan", str(plan_file)])
    assert exc.value.code == 2


def test_real_factory_ordinary_test_module_has_a_focused_selection():
    root = Path(__file__).resolve().parents[1]
    result = test_scope.select(root, {"tests/test_triage_backlog.py"})
    assert result["mode"] == "focused"
    assert "tests/test_triage_backlog.py" in result["tests"]
    assert set(test_scope.INVARIANTS) <= set(result["tests"])
    assert "tests/test_consumer_sync.py" not in result["tests"]


@pytest.mark.parametrize("declaration", [
    "", "TEST_SOURCE_ROOTS = scripts tests", "TEST_SOURCE_ROOTS := scripts tests\nTEST_SOURCE_ROOTS := scripts tests",
    "TEST_SOURCE_ROOTS := scripts tests $(shell false)", "TEST_SOURCE_ROOTS := scripts tests ../outside",
    "TEST_SOURCE_ROOTS := scripts tests missing", "TEST_SOURCE_ROOTS := scripts scripts tests",
    "TEST_SOURCE_ROOTS := scripts", "override TEST_SOURCE_ROOTS := scripts tests",
    "TEST_SOURCE_ROOTS := scripts tests /absolute", "TEST_SOURCE_ROOTS := scripts tests hooks",
    "TEST_SOURCE_ROOTS := scripts tests scripts/nested",
])
def test_invalid_or_unknown_source_root_policy_falls_upward(tree, declaration):
    write(tree, "Makefile", declaration + "\n")
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "full" and result["tests"] == ["tests"]
    assert "source-root policy" in result["rationale"][0]


def test_unreadable_source_root_policy_falls_upward(tree):
    (tree / "Makefile").unlink()
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_declared_public_caller_tree_selects_behavior_and_caller_tests(tree):
    write(tree, "adapter/public.py", "import reporting\ndef call(): return reporting.answer()\n")
    write(tree, "tests/test_public.py", "from adapter import public\n")
    write(tree, "Makefile", "TEST_SOURCE_ROOTS := scripts tests adapter\n")
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "focused"
    assert "adapter/public.py" in result["affected_sources"]
    assert "tests/test_public.py" in result["tests"]
    assert result["source_roots"] == ["scripts", "tests", "adapter"]


def test_undeclared_or_sensitive_caller_cannot_be_waived_by_known_roots(tree):
    write(tree, "adapter/public.py", "import reporting\n")
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"
    write(tree, "Makefile", "TEST_SOURCE_ROOTS := scripts tests adapter\n")
    write(tree, "adapter/auth.py", "import reporting\n")
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_symlink_source_root_cannot_escape_repository_policy(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    write(outside, "module.py")
    (tree / "adapter").symlink_to(outside, target_is_directory=True)
    write(tree, "Makefile", "TEST_SOURCE_ROOTS := scripts tests adapter\n")
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_real_ordinary_production_story_selects_public_callers_and_behavior():
    root = Path(__file__).resolve().parents[1]
    result = test_scope.select(root, {"scripts/triage_backlog.py"})
    assert result["mode"] == "focused"
    assert {"scripts/triage_backlog.py", "integrations/mcp/tools.py", "integrations/mcp/server.py"} <= set(result["affected_sources"])
    assert {"tests/test_triage_backlog.py", "tests/test_mcp_server.py"} <= set(result["tests"])
    assert set(test_scope.INVARIANTS) <= set(result["tests"])
    assert "tests/test_consumer_sync.py" not in result["tests"]
    assert test_scope.select(root, {"scripts/merge_pr.py"})["mode"] == "full"
    assert test_scope.select(root, {"unknown/module.py"})["mode"] == "full"


def test_literal_verifier_test_list_does_not_create_a_business_caller(tree):
    write(tree, "scripts/test_scope.py", 'INVARIANTS = ["tests/test_reporting.py"]\n')
    result = test_scope.select(tree, {"scripts/reporting.py"})
    assert result["mode"] == "focused"
    assert "scripts/test_scope.py" not in result["affected_sources"]
    assert "tests/test_reporting.py" in result["tests"]


def test_explicit_sensitive_production_import_is_not_a_terminal_test_reference(tree):
    write(tree, "scripts/merge_authority.py", "from tests import test_reporting\n")
    assert test_scope.select(tree, {"scripts/reporting.py"})["mode"] == "full"


def test_cli_still_binds_ci_environment_to_the_actual_checkout(repository, monkeypatch, capsys):
    root, base = repository
    write(root, "scripts/reporting.py", "def answer(): return 2\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("ARU_TEST_BASE", base)
    monkeypatch.setenv("ARU_EXPECTED_HEAD", base)
    assert test_scope.main([]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "focused"
    assert document["changed_paths"] == ["scripts/reporting.py"]


@pytest.mark.parametrize("expected_head", ["0" * 40, "malformed-head"])
def test_cli_rejects_wrong_ci_environment_head(repository, monkeypatch, capsys, expected_head):
    root, base = repository
    monkeypatch.chdir(root)
    monkeypatch.setenv("ARU_TEST_BASE", base)
    monkeypatch.setenv("ARU_EXPECTED_HEAD", expected_head)
    with pytest.raises(SystemExit) as exc:
        test_scope.main([])
    assert exc.value.code == 2
    assert "exact-head checkout evidence failed" in capsys.readouterr().err
