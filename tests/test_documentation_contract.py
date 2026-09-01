from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPERATING_GUIDANCE = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "KERNEL-CONTRACT.md",
    ROOT / "docs" / "ENFORCEMENT-REGISTER.md",
    ROOT / "docs" / "OPERATIONS.md",
    ROOT / "docs" / "DEGRADED-MODE.md",
    ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
    ROOT / "templates" / "AGENTS.md",
    ROOT / "templates" / "pull_request.md",
    *(ROOT / "skills").glob("*/SKILL.md"),
]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operating_guidance_does_not_route_to_retired_surfaces():
    retired = (
        "run-aru-factory",
        "code-review",
        "fetch_next_issue.py",
        "--refresh-verification",
        "--reconcile-epic",
        "epic-close-policy",
        "--agent codex-local --claim",
        "multiple explicit agents",
        "batch mode",
        "Backlog recovery",
    )
    for path in OPERATING_GUIDANCE:
        content = text(path)
        assert all(name not in content for name in retired), path


def test_reviewer_timer_is_defined_only_in_the_canonical_contract():
    timing_sources = [
        path
        for path in OPERATING_GUIDANCE
        if "15 minutes" in text(path) or "15-minute" in text(path)
    ]
    assert timing_sources == [ROOT / "docs" / "KERNEL-CONTRACT.md"]


def test_version_and_layer_truth_are_explicit():
    readme = text(ROOT / "README.md")
    changelog = text(ROOT / "CHANGELOG.md")
    contract = text(ROOT / "docs" / "KERNEL-CONTRACT.md")

    assert "v0.2.8" in readme
    assert "## v0.2.8" in changelog
    assert all(name in contract for name in ("Kernel", "External Driver", "Consumer policy"))
    assert "Tier 0-1 changes do not wait for an authoritative review" in contract


def test_governed_workflows_pin_touches_check_to_event_head():
    for path in (
        ROOT / ".github" / "workflows" / "governed-pr.yml",
        ROOT / "templates" / "governed-pr.yml",
    ):
        workflow = text(path)
        assert "ARU_EXPECTED_HEAD: ${{ github.event.pull_request.head.sha }}" in workflow
        assert '--expected-head "$ARU_EXPECTED_HEAD"' in workflow


def test_governed_workflows_use_only_budget_free_self_hosted_macs():
    for path in (
        ROOT / ".github" / "workflows" / "governed-pr.yml",
        ROOT / "templates" / "governed-pr.yml",
    ):
        workflow = text(path)
        assert "runs-on: [self-hosted, macOS, ARM64, aru-ci]" in workflow
        assert "ubuntu-latest" not in workflow
        assert "macos-latest" not in workflow
        assert "windows-latest" not in workflow
        assert "actions/upload-artifact" not in workflow
        assert "actions/setup-python" not in workflow
        assert "cache:" not in workflow
        assert "pull_request_target" not in workflow
