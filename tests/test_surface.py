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


def test_hard_surface_budgets():
    tracked = tracked_paths()
    production = [
        path
        for path in tracked
        if path.suffix == ".py" and path.parent.name in {"scripts", "hooks"}
    ]
    tests = [path for path in tracked if path.suffix == ".py" and path.parent.name == "tests"]
    assert sum(lines(path) for path in production) <= 6000
    assert sum(lines(path) for path in tests) <= 9000
    assert all(lines(path) <= 800 for path in production + tests)


def test_supported_command_and_skill_budgets():
    tracked = tracked_paths()
    commands = [
        path
        for path in tracked
        if path.parent.name == "scripts"
        and path.name != "common.py"
        and path.suffix in {".py", ".sh"}
    ]
    skills = [
        path
        for path in tracked
        if path.name == "SKILL.md" and path.parent.parent.name == "skills"
    ]
    assert 12 <= len(commands) <= 14
    assert len(skills) == 6


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


def test_active_documents_are_exactly_the_kernel_set():
    tracked = tracked_paths()
    documents = {
        path.relative_to(ROOT).as_posix()
        for path in tracked
        if (path.parent == ROOT and path.name in {"README.md", "AGENTS.md", "CHANGELOG.md"})
        or (path.parent.name == "docs" and not is_non_operating_docs_file(path))
    }
    assert documents == OPERATING_DOCUMENTS


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
    assert "schedule:" not in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_one_state_authority_no_tracked_runtime_ledgers():
    tracked_state = [
        path
        for path in tracked_paths()
        if path.suffix in {".json", ".db", ".sqlite"}
    ]
    assert tracked_state == []
