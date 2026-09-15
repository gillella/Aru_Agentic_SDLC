from __future__ import annotations

from pathlib import Path

import init_project
import policy


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
        # The removed reviewer-policy machinery.
        "--refresh-reviewer",
        "--reviewer-status",
        "--probe-reviewers",
        "--recover-legacy",
        "--coding-reviewer-unavailable",
        "ARU_CODING_REVIEWERS",
        "review-policy:timeout",
        # The removed promotion scope pin.
        "ready:<digest>",
    )
    for path in OPERATING_GUIDANCE:
        content = text(path)
        assert all(name not in content for name in retired), path


def test_review_is_one_approval_from_another_account():
    for path in (ROOT / "AGENTS.md", ROOT / "docs" / "KERNEL-CONTRACT.md", ROOT / "templates" / "AGENTS.md"):
        content = " ".join(text(path).split())
        assert "account other than the" in content, path
        assert "Tier 2-3" not in content, path


def test_scope_is_judged_live_not_pinned_at_promotion():
    """The agent rules and the contract must agree on WHEN scope is judged.

    A pinned digest was removed from the kernel; guidance that still described a
    promotion pin sent agents to a gate that no longer exists.
    """
    for path in (ROOT / "AGENTS.md", ROOT / "docs" / "KERNEL-CONTRACT.md"):
        content = " ".join(text(path).split())
        assert "live at merge time" in content, path
        assert "not frozen at promotion" in content, path
        assert "Promotion pins" not in content, path


def test_version_and_layer_truth_are_explicit():
    readme = text(ROOT / "README.md")
    changelog = text(ROOT / "CHANGELOG.md")
    contract = text(ROOT / "docs" / "KERNEL-CONTRACT.md")
    enforcement = text(ROOT / "docs" / "ENFORCEMENT-REGISTER.md")

    release = policy.release()
    assert (
        f"**Project status: v{release['version']} is the current released version "
        f"(released {release['released']}).**"
    ) in readme
    assert "## v1.0.0 - Governed, Risk-Proportional Delivery" in changelog
    assert "v2 public API" in contract
    assert "Upgrade from v0.2.8" in changelog
    assert "aru-governed-pr" in enforcement
    assert "authenticated GitHub Actions App" in enforcement
    assert all(name in contract for name in ("Kernel", "External Driver", "Consumer policy"))
    assert "Every pull request, whatever it changes, needs one approval" in " ".join(contract.split())


def test_active_release_and_freeze_guidance_agree():
    agents = text(ROOT / "AGENTS.md")
    readme = text(ROOT / "README.md")
    assert "freeze has concluded" in agents
    assert "freeze period has concluded" in readme
    for relative in ("README.md", "docs/KERNEL-CONTRACT.md", "docs/OPERATIONS.md"):
        content = " ".join(text(ROOT / relative).split())
        assert "unreleased v2" not in content, relative
        assert "v2 source candidate" not in content, relative
        assert ".aru/verify-project.sh" in content, relative


def governed_workflows() -> dict[str, str]:
    """Aru's own live workflow plus the consumer template for every profile."""
    template = text(ROOT / "templates" / "governed-pr.yml")
    workflows = {"live": text(ROOT / ".github" / "workflows" / "governed-pr.yml")}
    for profile in init_project.RUNNER_PROFILES:
        workflows[profile] = init_project.render_profile(template, profile)
    return workflows


def test_governed_workflows_pin_touches_check_to_event_head():
    """Aru's own workflow plumbs the head itself; a consumer stub hands it to the
    action, which is where the `--expected-head` call now lives."""
    live = text(ROOT / ".github" / "workflows" / "governed-pr.yml")
    assert "ARU_EXPECTED_HEAD: ${{ github.event.pull_request.head.sha }}" in live
    assert '--expected-head "$ARU_EXPECTED_HEAD"' in live

    action = text(ROOT / ".github" / "actions" / "governed-pr" / "action.yml")
    assert '--expected-head "${ARU_EXPECTED_HEAD}"' in action
    assert "ARU_EXPECTED_HEAD: ${{ inputs.expected-head }}" in action
    for profile in init_project.RUNNER_PROFILES:
        stub = init_project.render_profile(text(ROOT / "templates" / "governed-pr.yml"), profile)
        assert "expected-head: ${{ github.event.pull_request.head.sha }}" in stub, profile


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
    # The interpreter and `gh` preconditions moved with the checks themselves: a
    # consumer stub carries no commands, so the action asserts them once.
    action = text(ROOT / ".github" / "actions" / "governed-pr" / "action.yml")
    assert "sys.version_info >= (3, 11)" in action
    assert "command -v gh" in action
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
        "skills/init-agent-project/SKILL.md",
        "skills/implement-next-issue/SKILL.md",
    ):
        content = text(ROOT / relative)
        assert "self-hosted-mac" in content, relative
        assert "github-hosted" in content, relative
