from __future__ import annotations

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


PRODUCTION_LINE_BUDGET = 6000
TEST_LINE_BUDGET = 9000
FILE_LINE_BUDGET = 800


def surface_budget_violations(production_total: int, test_total: int, max_file: int) -> list[str]:
    violations: list[str] = []
    if production_total > PRODUCTION_LINE_BUDGET:
        violations.append("production")
    if test_total > TEST_LINE_BUDGET:
        violations.append("tests")
    if max_file > FILE_LINE_BUDGET:
        violations.append("file")
    return violations


def test_hard_surface_budgets():
    tracked = tracked_paths()
    production = [
        path
        for path in tracked
        if path.suffix == ".py" and path.parent.name in {"scripts", "hooks"}
    ]
    tests = [path for path in tracked if path.suffix == ".py" and path.parent.name == "tests"]
    production_lines = [lines(path) for path in production]
    test_lines = [lines(path) for path in tests]
    max_file = max(production_lines + test_lines, default=0)
    assert surface_budget_violations(sum(production_lines), sum(test_lines), max_file) == []


def test_hard_surface_budgets_deny_over_limit():
    assert surface_budget_violations(PRODUCTION_LINE_BUDGET + 1, 0, 0) == ["production"]
    assert surface_budget_violations(0, TEST_LINE_BUDGET + 1, 0) == ["tests"]
    assert surface_budget_violations(0, 0, FILE_LINE_BUDGET + 1) == ["file"]
    assert surface_budget_violations(PRODUCTION_LINE_BUDGET, TEST_LINE_BUDGET, FILE_LINE_BUDGET) == []


def test_supported_command_and_skill_budgets():
    commands = supported_command_paths()
    skills = [
        path
        for path in tracked_paths()
        if path.name == "SKILL.md" and path.parent.parent.name == "skills"
    ]
    assert 12 <= len(commands) <= 14
    assert len(skills) == 6


def supported_command_paths() -> list[Path]:
    return [
        path
        for path in tracked_paths()
        if path.parent.name == "scripts"
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
        ROOT / "scripts" / "review_risk.py",
        ROOT / "scripts" / "touches.py",
    }
    assert all(helper.exists() for helper in helpers)
    assert helpers.isdisjoint(supported_command_paths())
    assert len(supported_command_paths()) <= 14


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
    assert {path.name for path in workflows.glob("*.yml")} == {"governed-pr.yml"}


def test_one_state_authority_no_tracked_runtime_ledgers():
    tracked_state = [
        path
        for path in tracked_paths()
        if path.suffix in {".json", ".db", ".sqlite"}
    ]
    assert tracked_state == []
