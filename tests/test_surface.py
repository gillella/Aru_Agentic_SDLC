from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [ROOT / raw.decode() for raw in result.stdout.split(b"\0") if raw]


FILE_LINE_BUDGET = 800


# Aggregate line volume is deliberately unbudgeted, for production and tests alike: a
# total ceiling blocked every small fix once reached and could only be met by deleting
# working code. The per-file cap still bounds growth, including for test files.
def surface_budget_violations(max_file: int) -> list[str]:
    return ["file"] if max_file > FILE_LINE_BUDGET else []


def budgeted_python_paths(paths: list[Path]) -> list[Path]:
    return [
        path for path in paths
        if path.suffix == ".py"
        and not path.is_relative_to(ROOT / "integrations")
        and (path.parent.name in {"scripts", "hooks", "tests"}
             or path.is_relative_to(ROOT / "driver"))
    ]


def test_hard_surface_budgets():
    max_file = max((lines(path) for path in budgeted_python_paths(tracked_paths())), default=0)
    assert surface_budget_violations(max_file) == []


def test_hard_surface_budgets_deny_over_limit():
    assert surface_budget_violations(FILE_LINE_BUDGET + 1) == ["file"]
    assert surface_budget_violations(FILE_LINE_BUDGET) == []


def test_optional_driver_python_has_the_same_file_budget(tmp_path):
    source = tmp_path / "driver" / "src" / "monitor.py"
    source.parent.mkdir(parents=True)
    source.write_text("pass\n" * FILE_LINE_BUDGET, encoding="utf-8")
    driver_source = ROOT / "driver" / "src" / "monitor.py"
    assert budgeted_python_paths([driver_source]) == [driver_source]
    assert surface_budget_violations(lines(source)) == []
    source.write_text("pass\n" * (FILE_LINE_BUDGET + 1), encoding="utf-8")
    assert surface_budget_violations(lines(source)) == ["file"]


def test_supported_command_and_skill_budgets():
    commands = supported_command_paths()
    skills = installed_skill_paths(tracked_paths())
    assert 12 <= len(commands) <= 14
    assert len(skills) == 6


def installed_skill_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths
            if path.name == "SKILL.md" and path.parent.parent == ROOT / "skills"]


def supported_command_paths(paths: list[Path] | None = None) -> list[Path]:
    # `verify_consumer.sh` is the governed-pr action's verifier, not an operator
    # command: it is the former `templates/verify.sh`, which this budget never
    # counted either. Moving a script into scripts/ must not read as new surface.
    internal_helpers = {"common.py", "local_verification.py", "verify_consumer.sh"}
    return [
        path
        for path in (tracked_paths() if paths is None else paths)
        if path.parent == ROOT / "scripts"
        and path.name not in internal_helpers
        and path.suffix in {".py", ".sh"}
        and (
            path.suffix == ".sh"
            or 'if __name__ == "__main__"' in path.read_text(encoding="utf-8")
        )
    ]


def test_library_modules_do_not_count_as_supported_commands():
    helpers = {
        ROOT / "scripts" / "common.py",
        ROOT / "scripts" / "merge_state.py",
        ROOT / "scripts" / "policy.py",
        ROOT / "scripts" / "touches.py",
    }
    assert all(helper.exists() for helper in helpers)
    assert helpers.isdisjoint(supported_command_paths())
    assert len(supported_command_paths()) <= 14


def test_driver_package_does_not_expand_kernel_commands_or_skills():
    command = ROOT / "driver" / "run.py"
    assert supported_command_paths([command]) == []
    assert installed_skill_paths([ROOT / "driver" / "skills" / "monitor" / "SKILL.md"]) == []


OPERATING_DOCUMENTS = {
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "docs/KERNEL-CONTRACT.md",
    "docs/ENFORCEMENT-REGISTER.md",
    "docs/OPERATIONS.md",
    "docs/DEGRADED-MODE.md",
}


def is_non_operating_docs_file(path: Path) -> bool:
    """Evidence and vision files under docs/ are not operating contracts."""
    name = path.name
    return name.startswith("AUDIT-") or name == "NORTH-STAR.md"


def counted_operating_documents(paths: list[Path]) -> set[str]:
    documents: set[str] = set()
    for path in paths:
        if path.parent == ROOT and path.name in {"README.md", "AGENTS.md", "CHANGELOG.md"}:
            documents.add(path.relative_to(ROOT).as_posix())
        elif path.parent == ROOT / "docs" and not is_non_operating_docs_file(path):
            documents.add(path.relative_to(ROOT).as_posix())
    return documents


def test_active_documents_are_exactly_the_kernel_set():
    assert counted_operating_documents(tracked_paths()) == OPERATING_DOCUMENTS


def test_nested_docs_are_not_counted_as_operating_documents():
    nested = ROOT / "examples" / "docs" / "README.md"
    assert counted_operating_documents([nested]) == set()
    assert counted_operating_documents([ROOT / "docs" / "NORTH-STAR.md"]) == set()
    assert counted_operating_documents([ROOT / "docs" / "KERNEL-CONTRACT.md"]) == {
        "docs/KERNEL-CONTRACT.md"
    }
    assert counted_operating_documents([ROOT / "driver" / "README.md"]) == set()


def driver_source_violations(paths: list[Path]) -> list[str]:
    """Driver source may be tracked, but host-local state and job definitions may not."""
    forbidden_names = {".env", "credentials", "credentials.json", "jobs.json", "receipts.json",
                       "binding.json", "coordination.lock"}
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3", ".jsonl", ".plist", ".service"}
    return [path.relative_to(ROOT).as_posix() for path in paths
            if path.is_relative_to(ROOT / "driver")
            and (path.name in forbidden_names or path.suffix.lower() in forbidden_suffixes
                 or any(part in {"receipts", "credentials", "runtime", "jobs"}
                        for part in path.relative_to(ROOT / "driver").parts[:-1]))]


def test_optional_driver_source_excludes_tracked_runtime_state():
    assert driver_source_violations(tracked_paths()) == []
    allowed = [ROOT / "driver" / "README.md", ROOT / "driver" / "pyproject.toml",
               ROOT / "driver" / "src" / "monitor.py"]
    assert driver_source_violations(allowed) == []
    for relative in ("driver/.env", "driver/credentials.json", "driver/receipts/run.json",
                     "driver/jobs/monitor.json", "driver/monitor.plist", "driver/state.sqlite"):
        assert driver_source_violations([ROOT / relative]) == [relative]


def kernel_driver_reference_violations(contents: dict[str, str]) -> list[str]:
    """No executable Kernel or installed-skill surface may load Driver source."""
    reference = re.compile(r"driver/|\b(?:import|from)\s+driver\b")
    return [path for path, source in contents.items() if reference.search(source)]


def test_kernel_execution_and_installed_skills_do_not_load_driver():
    tracked = tracked_paths()
    runtime = [path for path in tracked if path.is_relative_to(ROOT / "scripts")
               or path.is_relative_to(ROOT / "hooks")
               or path.is_relative_to(ROOT / "skills")
               or path.is_relative_to(ROOT / "templates")
               or path.is_relative_to(ROOT / ".github" / "workflows")
               or path.is_relative_to(ROOT / ".github" / "actions")
               or path == ROOT / "Makefile"]
    contents = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
                for path in runtime if path.is_file() and path.suffix in {".py", ".sh", ".md", ".yml", ".yaml", ".toml"}}
    assert kernel_driver_reference_violations(contents) == []
    for path, source in (("scripts/fetch_next_work.py", "import driver"),
                         ("scripts/init_project.py", "install driver/"),
                         ("scripts/install_agent_integration.sh", "driver/activate.sh"),
                         ("skills/implement-next-issue/SKILL.md", "run driver/monitor.py"),
                         (".github/workflows/governed-pr.yml", "python3 driver/monitor.py")):
        assert kernel_driver_reference_violations({path: source}) == [path]


def test_decision_records_are_not_operating_documents():
    """Rationale lives in docs/decisions/. Those are nested, so they are not
    counted against the seven-document budget, and the budget still binds."""
    records = sorted((ROOT / "docs" / "decisions").glob("*.md"))
    assert records, "the contract cites docs/decisions/; it must exist"
    assert counted_operating_documents(records) == set()
    assert counted_operating_documents(tracked_paths()) == OPERATING_DOCUMENTS


def test_north_star_is_vision_not_an_operating_document():
    north_star = ROOT / "docs" / "NORTH-STAR.md"
    assert north_star.is_file()
    assert is_non_operating_docs_file(north_star)
    assert is_non_operating_docs_file(Path("docs/AUDIT-2026-08-28.md"))
    assert not is_non_operating_docs_file(Path("docs/KERNEL-CONTRACT.md"))
    tracked = {path.relative_to(ROOT).as_posix() for path in tracked_paths()}
    assert "docs/NORTH-STAR.md" in tracked
    assert "docs/NORTH-STAR.md" not in OPERATING_DOCUMENTS


def test_wrong_layer_surfaces_are_absent():
    forbidden = {
        "agent_presence.py",
        "loop_control.py",
        "fleet_status.py",
        "reassign_review.py",
        "review_reassignment_lock.py",
        "release.py",
        "sync_spec.py",
        "worker_handoff.py",
        "fleet-worker.md",
        "continuity.json",
    }
    present = {path.name for path in tracked_paths()}
    assert forbidden.isdisjoint(present)
    workflows = ROOT / ".github/workflows"
    # Two, and only two: the check that runs from the pull-request head, and the one
    # that runs from the base branch so a pull request cannot rewrite its own gate.
    assert {path.name for path in workflows.glob("*.yml")} == {
        "governed-pr.yml",
        "merge-policy.yml",
    }


def test_one_state_authority_no_tracked_runtime_ledgers():
    # GitHub is the only lifecycle store: no tracked database, ledger or runtime lock.
    ledgers = {".jsonl", ".db", ".sqlite", ".sqlite3"}
    runtime = {"binding.json", "jobs.json", "coordination.lock", "continuity.json"}
    assert not [path for path in tracked_paths() if path.suffix.lower() in ledgers or path.name in runtime]


def test_repository_ships_no_agent_framework_integration():
    # Aru stays agent-independent: any agent enforces these rules, so no framework adapter is tracked.
    removed = ("integrations/hermes/", "integrations/chopin/", "integrations/personas/")
    assert not [path for path in tracked_paths() if path.relative_to(ROOT).as_posix().startswith(removed)]
