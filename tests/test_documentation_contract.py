from __future__ import annotations

import re
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
        f"**Project status: v{release['version']} is the declared release version "
        f"(dated {release['released']}).**"
    ) in readme
    assert "## v1.0.0 - Governed, Risk-Proportional Delivery" in changelog
    assert "v2 public API" in contract
    assert "Upgrade from v0.2.8" in changelog
    assert "aru-governed-pr" in enforcement
    assert "authenticated GitHub Actions App" in enforcement
    assert all(name in contract for name in ("Kernel", "External Driver", "Consumer policy"))
    assert "Every pull request, whatever it changes, needs one approval" in " ".join(contract.split())


def test_v3_release_documents_break_and_disabled_source():
    readme = text(ROOT / "README.md")
    changelog = text(ROOT / "CHANGELOG.md")
    operations = text(ROOT / "docs" / "OPERATIONS.md")
    for content in (readme, changelog, operations):
        assert "#783" in content
        assert "v3.0.0" in content
        assert "CODEOWNERS" in content
        assert "#830" in content
        assert "disabled" in content
    assert "unproven" in changelog
    assert "uninstalled and disabled" in operations


def test_human_only_migration_requires_server_enforced_review():
    sections = (
        text(ROOT / "README.md").split("### Upgrade from v2.5.0", 1)[1].split("\nAru helps", 1)[0],
        text(ROOT / "CHANGELOG.md").split("## v3.0.0", 1)[1].split("## v2.5.0", 1)[0],
        text(ROOT / "docs" / "OPERATIONS.md").split("For v2.5.0 to v3.0.0 migration", 1)[1].split("\nGitHub authenticates", 1)[0],
        text(ROOT / "docs" / "OPERATIONS.md").split("This file rollback does not undo", 1)[1].split("Do not sync a real", 1)[0],
    )
    for section in sections:
        assert "CODEOWNERS alone only requests reviews" in " ".join(section.split())
        assert "required code-owner review" in " ".join(section.split())
        assert "required human reviewers" in " ".join(section.split())


def test_optional_driver_source_boundary_is_documented_without_rollout_claims():
    contract = " ".join(text(ROOT / "docs" / "KERNEL-CONTRACT.md").split())
    agents = " ".join(text(ROOT / "AGENTS.md").split())
    readme = " ".join(text(ROOT / "README.md").split())
    operations = " ".join(text(ROOT / "docs" / "OPERATIONS.md").split())
    for content in (contract, agents, readme, operations):
        assert "driver/" in content
        assert "separately packaged" in content
        assert "outside Git" in content
    assert "Kernel remains independently usable" in contract
    assert "no verified maintained source or installer" in contract
    assert "operator-only `merge_pr.py --expected-head`" not in contract
    assert "#794" in operations


def test_driver_merge_boundary_agrees_between_contract_and_operations():
    """Both documents admit only a proven scoped entry point, never the raw key."""
    contract = " ".join(text(ROOT / "docs" / "KERNEL-CONTRACT.md").split())
    operations = " ".join(text(ROOT / "docs" / "OPERATIONS.md").split())
    for content in (contract, operations):
        for requirement in (
            "raw merge-authority credential", "proven, operator-installed protected entry point",
            "separate operator-controlled principal", "canonical helper with every gate",
            "no generic endpoint and never runs PR code", "An unproven entry point refuses",
        ):
            assert requirement in content, requirement
    assert ("its generic App runner and direct `merge_pr.py --expected-head` execution remain "
            "outside the Driver") in contract
    assert "merge-authority credential and operator-only" not in contract


def operations_section(heading: str) -> str:
    content = text(ROOT / "docs" / "OPERATIONS.md")
    marker = f"### {heading}\n"
    assert marker in content, f"missing OPERATIONS.md section: {heading}"
    return " ".join(content.split(marker, 1)[1].split("\n### ", 1)[0].split())


def test_trusted_merge_request_keeps_authority_outside_coding_and_ci():
    boundary = operations_section("Trusted callers and mechanical merge")
    for requirement in (
        "outside PR worktrees", "protected from coding/CI writes",
        "separate merge principal", "Strip caller-controlled tokens",
        "Never check out, import or execute PR-controlled code, verifier, hook or runtime",
        "approved repository, issue scope, PR and full expected SHA",
        "Reject extra arguments, caller-supplied paths, commands, environment",
        "raw keys, installation tokens, the generic App runner",
        "arbitrary API/check-posting endpoints inaccessible",
    ):
        assert requirement in boundary, requirement
    operations = " ".join(text(ROOT / "docs" / "OPERATIONS.md").split())
    assert "A proven restricted mechanical-merge route is the only automation exception" in operations
    assert "passing an agent these variables is not that route" in operations
    assert "Do not expose this session, its credentials or its generic App runner" in operations
    assert "an operator-controlled session alone runs" not in operations
    assert "operator alone invokes" not in operations


def test_trusted_mechanical_merge_retains_live_gates_and_close_out():
    boundary = operations_section("Trusted callers and mechanical merge")
    for requirement in (
        "canonical `merge_pr.py --expected-head`", "exact-head checks",
        "eligible non-author approval", "every finding/thread", "accepted criteria",
        "exclusive claim", "live touches", "dependency and base checks",
        "refusal of configured queues or pending queue/auto-merge requests",
        "changed or unreadable authority refuses", "Do not replace this route with GitHub auto-merge",
        "expected head", "merge commit", "linked issue CLOSED and Project Done",
        "`--finalize` recovery", "never submit the head again",
    ):
        assert requirement in boundary, requirement
    step = operations_section("Step 8: merge the exact head")
    assert "Apply **Trusted callers and mechanical merge** before unattended execution" in step
    assert '--expected-head "$head_sha"' in step


def test_external_continuation_is_owned_ready_first_and_explicitly_scoped():
    cycle = operations_section("Bounded external continuation")
    for requirement in (
        "Resume actionable work owned by that actual worker first",
        "only one exclusive active issue", "`fetch_next_work.py` for valid Ready work",
        "repository-wide candidates and has no scope flag",
        "restrict the live Ready candidate set", "canonical validation and ordering",
        "reject any out-of-scope result before claim", "report a scope block",
        "`triage_backlog.py --issue <authorized-number>`",
        "explicit authorized Backlog subset", "re-read the picker before claim",
        "another live read, never an unrelated fallback",
        "once per activation", "never poll inside a skill",
        "duplicate an accepted pending review", "inaccessible merge authority and genuine idle",
        "GitHub Issues/Project as the only lifecycle authority",
        "do not create a scheduler, private queue, handoff file or second lifecycle store in the Kernel",
    ):
        assert requirement in cycle, requirement


def test_operator_merge_evidence_does_not_claim_unattended_delivery():
    boundary = operations_section("Trusted callers and mechanical merge")
    assert "App **4921120**" in boundary
    assert "interactive operator route, not unattended delivery" in boundary
    assert "automatic merging remains unavailable until that boundary is proven" in boundary
    assert "does not install a service, activate a Driver, change schedules or modify a ruleset" in boundary


def test_restricted_package_requires_installation_and_actual_runtime_proof():
    integration = text(ROOT / "integrations/merge-authority/README.md")
    for requirement in (
        "GitHub Issues/Project remain the authority", "separate execution user",
        "does **not** establish that the capability is installed", "independently reviewed canonical",
        "Checks write and Metadata read only", "not** in sudoers", "No admin bypass",
        "verified in-memory", "Existing installation, sudo policy and staging paths are preserved",
        "Never send a PEM/token to an agent", "a generic shell, interpreter command or App runner",
        "one actual unattended governed merge", "changes no power settings or scheduled jobs",
    ):
        assert requirement in " ".join(integration.split()), requirement
    assert "restricted merge integration" in text(ROOT / "docs/OPERATIONS.md")


def test_import_chain_proof_is_limited_to_hashed_files_and_operator_audit():
    boundary = operations_section("Trusted callers and mechanical merge")
    for requirement in (
        "bundled helper modules, request/adapter code, sealed CLI and interpreter binary",
        "not individually inventoried", "`.pth`", "do not prove the whole import chain",
        "operator must audit", "runtime import chain remains unproven",
    ):
        assert requirement in boundary, requirement
    assert "verify their identities before execution" not in boundary
    integration = " ".join(text(ROOT / "integrations/merge-authority/README.md").split())
    assert "not individually inventoried" in integration
    assert "runtime import-chain proof remains unresolved" in integration


def test_human_enablement_is_config_bound_and_not_lifecycle_authority():
    boundary = operations_section("Trusted callers and mechanical merge")
    contract = " ".join(text(ROOT / "docs/KERNEL-CONTRACT.md").split())
    integration = " ".join(text(ROOT / "integrations/merge-authority/README.md").split())
    for requirement in ("submissions disabled", "root-owned", "installed configuration",
                        "before authority use", "host configuration, not lifecycle",
                        "human acceptance", "`--check`"):
        assert requirement in boundary, requirement
    for requirement in ("submissions disabled", "check-only evaluation available", "root-owned marker",
                        "denials, eligible preflight", "not lifecycle state or canonical gate evidence"):
        assert requirement in contract, requirement
    for requirement in ("installer never creates `enabled.json`", "separate human enablement",
                        "os.O_EXCL | os.O_NOFOLLOW", "config_sha256", "Existing markers are never overwritten",
                        "Rollback first removes", "keeping `--check` available", "already admitted/in-flight"):
        assert requirement in integration, requirement


def test_signal_documentation_describes_unwind_masking_and_pending_delivery_limits():
    integration = " ".join(text(ROOT / "integrations/merge-authority/README.md").split())
    for requirement in ("pthread_sigmask(SIG_BLOCK)", "reentrant Python handler delivery returns",
                        "blocked through unwind", "Ordinary exceptions also enter compensation",
                        "Original handlers are restored while blocked", "original mask is restored",
                        "pending signals", "SIGKILL/SIGSTOP", "host loss and transport outages",
                        "non-atomic authorization/submission boundary", "asynchronous",
                        "Intentionally ignored dispositions", "`SIG_IGN`", "`SIGPIPE`", "`SIGXFSZ`",
                        "`SIGSEGV`", "`SIGBUS`", "`SIGILL`", "`SIGFPE`",
                        "excluded from handler installation and masking", "faulting native instruction"):
        assert requirement in integration, requirement
    assert "every catchable" not in integration


def test_adapter_documents_compensation_and_its_honest_limit():
    integration = " ".join(text(ROOT / "integrations/merge-authority/README.md").split())
    for requirement in (
        "positive integer id, App id, head, name and conclusion",
        "at most two same-head `failure` runs through the same scoped token and check endpoint",
        "never repeats a success", "not an atomic revocation guarantee",
        "Token revocation is best effort", "never merge that head by hand",
    ):
        assert requirement in integration, requirement


def driver_activation_commands(content: str) -> list[str]:
    command = re.compile(
        r"^\s*(?:\$\s*)?(?:"
        r"python3?\s+(?:-\S+\s+)*(?:-m\s+driver(?:\.|\b)|[^\s]*\bdriver/)"
        r"|(?:pip3?|bash|sh|source|\.|launchctl|systemctl)\s+[^\n]*\bdriver(?:/|\.))",
        re.MULTILINE,
    )
    return command.findall(content)


def test_active_guidance_never_instructs_driver_install_or_activation():
    active = ("README.md", "AGENTS.md", "CHANGELOG.md", "docs/KERNEL-CONTRACT.md",
              "docs/ENFORCEMENT-REGISTER.md", "docs/OPERATIONS.md", "docs/DEGRADED-MODE.md")
    assert all(driver_activation_commands(text(ROOT / path)) == [] for path in active)
    for example in ("python3 driver/monitor.py", "python3 -m driver.monitor",
                    "python3 -u driver/monitor.py", "python3 -I -m driver.monitor",
                    "pip install driver/", "bash driver/install.sh"):
        assert driver_activation_commands(example), example
    assert driver_activation_commands("python3 -m pytest\nThe Driver stays optional.") == []
    assert driver_activation_commands("python3 -m pytest driver/tests/") == []
    assert driver_activation_commands("$ python3 -I -m pytest driver/tests/test_monitor.py") == []


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


def test_local_preflight_matches_the_governed_workflow_scope():
    """`make verify` must run what the server check runs, fail closed, and stay
    preflight: no installer, GitHub write or publish step hides in it."""
    live = text(ROOT / ".github" / "workflows" / "governed-pr.yml")
    makefile = text(ROOT / "Makefile")
    lint_paths = next(
        line.split(":=", 1)[1].strip()
        for line in makefile.splitlines() if line.startswith("LINT_PATHS")
    )
    assert f"-m ruff check {lint_paths}" in live
    assert "$(PYTHON) -m ruff check $(LINT_PATHS)" in makefile
    assert "-m pytest -q" in live
    assert "$(PYTHON) -m pytest -q" in makefile
    assert "verify: prerequisites lint test" in makefile
    assert "import pytest, ruff, yaml" in makefile
    for forbidden in ("pip install", "gh ", "git push", "curl", "|| true", "\t-", "-ignore", "--exit-zero"):
        assert forbidden not in makefile, forbidden

    readme = " ".join(text(ROOT / "README.md").split())
    assert "`make verify`" in readme
    assert "preflight evidence only" in readme
    assert "reviewable behavior change" in readme


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


def test_active_verification_guidance_rejects_retired_instructions():
    for relative in ("README.md", "docs/OPERATIONS.md"):
        content = text(ROOT / relative)
        assert "path-derived" not in content.casefold(), relative
        for line in content.splitlines():
            if ".aru/verify.sh" in line:
                assert any(label in line.casefold() for label in ("legacy", "retired")), (
                    relative, line,
                )


def test_documented_scaffold_matches_the_generator():
    operations = text(ROOT / "docs/OPERATIONS.md")
    tree = operations.split("The helper creates:\n\n```text\n", 1)[1].split("\n```", 1)[0]
    stack = []
    documented = set()
    for line in tree.splitlines()[1:]:
        prefix, name = line.split("── ", 1)
        depth = len(prefix) // 4
        stack = [*stack[:depth], name.rstrip("/")]
        if not name.endswith("/"):
            documented.add("/".join(stack))
    for profile in init_project.RUNNER_PROFILES:
        assert documented == set(init_project.framework_files(profile)), profile


def test_approval_guidance_matches_the_declared_rule():
    """Active prose must not promise a last-pusher gate absent from the policy."""
    declared = policy.ruleset_parameters()
    assert declared["require_last_push_approval"] is False
    assert declared["dismiss_stale_reviews_on_push"] is True
    assert declared["required_approving_review_count"] == 1
    contradictions = (
        r"(?:approval|review) of the (?:last|most recent) push",
        r"requires the most recent push",
        r"by someone other than its pusher",
        r"becomes (?:its|the) last pusher",
    )
    for relative in (
        "docs/KERNEL-CONTRACT.md", "README.md", "docs/OPERATIONS.md",
        "skills/address-pr-feedback/SKILL.md", "plugin/agents/aru-reviewer.md",
        "integrations/adoption/README.md",
    ):
        content = " ".join(text(ROOT / relative).split())
        assert ("account other than the author" in content
                or "account other than the PR author" in content), relative
        assert re.search(r"exact (?:current )?head", content), relative
        for pattern in contradictions:
            assert not re.search(pattern, content, re.IGNORECASE), (relative, pattern)


def test_operations_does_not_reactivate_legacy_reviewer_policy():
    """The compatibility stub cannot enforce the old eligibility or bypass flow."""
    operations = text(ROOT / "docs/OPERATIONS.md")
    approval = operations.split(
        "### Who may approve, and what to do when nobody can", 1,
    )[1].split("### Installed agent guidance", 1)[0]
    assert "have no merge or PR-creation effect" in " ".join(approval.split())
    for stale in ("`none` requires no approval", "twelve characters", "temporary bypass actor"):
        assert stale not in approval, stale
    creation = operations.split("### Step 6:", 1)[1].split("### Step 7:", 1)[0]
    assert "does not request reviewers" in " ".join(creation.split())
