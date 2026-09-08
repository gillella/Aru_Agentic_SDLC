from __future__ import annotations

from pathlib import Path

import init_project


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


def test_reviewer_timer_is_configurable_not_hard_coded():
    timing_sources = [
        path
        for path in OPERATING_GUIDANCE
        if "15 minutes" in text(path) or "15-minute" in text(path)
    ]
    assert timing_sources == []
    assert "configured timeout" in text(ROOT / "docs" / "KERNEL-CONTRACT.md")


def test_version_and_layer_truth_are_explicit():
    readme = text(ROOT / "README.md")
    changelog = text(ROOT / "CHANGELOG.md")
    contract = text(ROOT / "docs" / "KERNEL-CONTRACT.md")
    enforcement = text(ROOT / "docs" / "ENFORCEMENT-REGISTER.md")

    assert "**Project status: v2.0.0 reviewer-policy source candidate" in readme
    assert "## v1.0.0 - Governed, Risk-Proportional Delivery" in changelog
    assert "v2 public API" in contract
    assert "Upgrade from v0.2.8" in changelog
    assert "aru-governed-pr" in enforcement
    assert "authenticated GitHub Actions App" in enforcement
    assert all(name in contract for name in ("Kernel", "External Driver", "Consumer policy"))
    assert "Tier 0-1 changes do not wait for an authoritative review" in contract


def governed_workflows() -> dict[str, str]:
    """Aru's own live workflow plus the consumer template for every profile."""
    template = text(ROOT / "templates" / "governed-pr.yml")
    workflows = {"live": text(ROOT / ".github" / "workflows" / "governed-pr.yml")}
    for profile in init_project.RUNNER_PROFILES:
        workflows[profile] = init_project.render_profile(template, profile)
    return workflows


def test_governed_workflows_pin_touches_check_to_event_head():
    for workflow in governed_workflows().values():
        assert "ARU_EXPECTED_HEAD: ${{ github.event.pull_request.head.sha }}" in workflow
        assert '--expected-head "$ARU_EXPECTED_HEAD"' in workflow


def test_every_runner_profile_stays_budget_free_and_account_bound():
    budget_free = ("macos-latest", "windows-latest", "actions/upload-artifact",
                   "actions/setup-python", "cache:", "pull_request_target")
    workflows = governed_workflows()
    for name, workflow in workflows.items():
        for pattern in budget_free:
            assert pattern not in workflow, (name, pattern)
        assert workflow.index("trust boundary") < workflow.index(
            "Check out the exact pull-request head"
        ), name
        assert "Only verified pull_request events from this repository may execute" in workflow
        assert "sys.version_info >= (3, 11)" in workflow, name
        assert "command -v gh" in workflow, name
    assert init_project.ACCOUNT_RUNNER_PROFILES == {
        "gillella": "self-hosted-mac", "unum-inc": "github-hosted",
    }
    # Aru's own repository is a personal gillella repository, so it keeps the Macs.
    for name in ("live", "self-hosted-mac"):
        assert "runs-on: [self-hosted, macOS, ARM64, aru-ci]" in workflows[name], name
        assert "ubuntu-latest" not in workflows[name], name
    assert "runs-on: ubuntu-latest" in workflows["github-hosted"]
    assert "self-hosted" not in workflows["github-hosted"]


def test_operating_documents_record_the_account_runner_split():
    for relative in (
        "AGENTS.md", "README.md", "docs/KERNEL-CONTRACT.md", "docs/OPERATIONS.md",
        "docs/DEGRADED-MODE.md", "docs/ENFORCEMENT-REGISTER.md",
        "integrations/hermes/README.md", "skills/init-agent-project/SKILL.md",
        "skills/implement-next-issue/SKILL.md",
    ):
        content = text(ROOT / relative)
        assert "self-hosted-mac" in content, relative
        assert "github-hosted" in content, relative
