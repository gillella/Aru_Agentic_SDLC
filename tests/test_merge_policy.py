"""The merge-policy workflow: the check a pull request cannot rewrite.

`aru-governed-pr` runs the workflow file that is in the pull request, so a pull request
that edits it keeps the check name and can go green without running anything. This
workflow runs the copy on the base branch instead. These tests pin the properties that
make that true, and the safety rule that makes `pull_request_target` acceptable here.

A consumer's copy is now a stub: it owns the trigger, the trust guard, the base
checkout, the check name and the runner, and calls the Factory's composite action at a
pinned release for everything else. The steps that used to run inline therefore live in
`.github/actions/merge-policy/action.yml`, and are asserted there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import init_project
import policy

ROOT = Path(__file__).resolve().parents[1]
# Aru's own live workflow is NOT a rendering of the template any more: the Factory runs
# its checks from its own checkout, consumers call the released action. The properties
# below that hold for any merge-policy gate are still asserted against all three.
WORKFLOW_SOURCES = ["live", "self-hosted-mac", "github-hosted"]
CONSUMER_SOURCES = ["self-hosted-mac", "github-hosted"]
ACTION_PATH = ROOT / ".github/actions/merge-policy/action.yml"
PROFILE_MARKER = "# aru-runner-profile:"


def policy_workflows() -> dict[str, str]:
    """Aru's own merge-policy workflow plus the template rendered for every profile."""
    template = (ROOT / "templates/merge-policy.yml").read_text(encoding="utf-8")
    return {
        "live": (ROOT / ".github/workflows/merge-policy.yml").read_text(encoding="utf-8"),
        **{name: init_project.render_profile(template, name) for name in init_project.RUNNER_PROFILES},
    }


def governed_workflows() -> dict[str, str]:
    template = (ROOT / "templates/governed-pr.yml").read_text(encoding="utf-8")
    return {
        "live": (ROOT / ".github/workflows/governed-pr.yml").read_text(encoding="utf-8"),
        **{name: init_project.render_profile(template, name) for name in init_project.RUNNER_PROFILES},
    }


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_runs_the_base_branch_copy_of_itself(source):
    """The point of the workflow: a pull request cannot edit the check that judges it."""
    workflow = yaml.safe_load(policy_workflows()[source])
    # PyYAML resolves the bare `on:` key to the boolean True.
    # pull_request_target ALONE. GitHub runs the pull request's own copy of a workflow
    # for pull_request_review, so that trigger would surrender the judge to the judged.
    assert list(workflow[True]) == ["pull_request_target"]
    job = workflow["jobs"]["merge-policy"]
    assert job["name"] == "aru-merge-policy"
    # Distinct from the head-run check, so the two cannot be confused in a ruleset.
    other = yaml.safe_load(governed_workflows()[source])["jobs"]["governed-pr"]["name"]
    assert job["name"] != other


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_never_reads_or_runs_pull_request_code(source):
    """pull_request_target carries the base repository's token, so the head stays untouched."""
    raw = policy_workflows()[source]
    steps = yaml.safe_load(raw)["jobs"]["merge-policy"]["steps"]
    checkout = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")]
    assert len(checkout) == 1
    assert checkout[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout[0]["with"]["persist-credentials"] is False
    # The head sha reaches the checker as a value to verify against, never as a ref to read.
    assert "github.event.pull_request.head.sha" in raw
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in raw
    for forbidden in ("pip install", "pytest", "ruff check", "requirements-dev.txt"):
        assert forbidden not in raw


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_publishes_its_verdict_exactly_once(source):
    """The job's own check run attaches to the head, so it is the required check.

    This was originally built the other way round, on the belief that a
    pull_request_target job's check run lands on the base commit and could never be
    required. Observed on PR #678: the head carried both a check run and a commit status
    of the same name. The explicit status step was therefore a duplicate, and the
    permission it needed is not requested any more.
    """
    raw = policy_workflows()[source]
    workflow = yaml.safe_load(raw)
    assert "statuses" not in workflow["permissions"]
    assert "statuses/" not in raw
    # One job, so the required context `aru-merge-policy` is published exactly once.
    assert list(workflow["jobs"]) == ["merge-policy"]
    assert workflow["jobs"]["merge-policy"]["name"] == "aru-merge-policy"


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_asks_for_read_only_permissions(source):
    """`check_stubs.py` refuses a head that grants itself a write scope while keeping the
    required check's name, so the base copy must never hold one to begin with."""
    permissions = yaml.safe_load(policy_workflows()[source])["permissions"]
    assert permissions, "an empty permissions block would inherit the workflow default"
    assert set(permissions.values()) == {"read"}, permissions


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_declares_one_runner_profile_matching_its_runner(source):
    """The marker is what `check_stubs.py` compares across base and head; a head that
    moved the check to another pool while keeping the name would be caught by it only
    if the marker and the runner agree here."""
    raw = policy_workflows()[source]
    declared = [line.split(":", 1)[1].strip() for line in raw.splitlines()
                if line.strip().startswith(PROFILE_MARKER)]
    assert len(declared) == 1, declared
    runs_on = yaml.safe_load(raw)["jobs"]["merge-policy"]["runs-on"]
    assert runs_on == yaml.safe_load(init_project.profile_spec(declared[0])["runs_on"])


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_refuses_events_it_cannot_trust(source):
    """A fork head, or any event other than pull_request_target, must not be judged here."""
    steps = yaml.safe_load(policy_workflows()[source])["jobs"]["merge-policy"]["steps"]
    guard = steps[0]["run"]
    assert '"$ARU_EVENT_NAME" != "pull_request_target"' in guard
    assert '"$ARU_HEAD_REPOSITORY" != "$ARU_REPOSITORY"' in guard
    assert "exit 1" in guard


@pytest.mark.parametrize("source", CONSUMER_SOURCES)
def test_the_consumer_stub_calls_exactly_one_aru_action_at_the_pinned_release(source):
    """A stub carries no verification of its own: one action reference, this Factory,
    this release. `check_stubs.py` enforces the same count at the head, and an upgrade
    is allowed to change nothing in the reference but the tag."""
    raw = policy_workflows()[source]
    steps = yaml.safe_load(raw)["jobs"]["merge-policy"]["steps"]
    uses = [str(step["uses"]) for step in steps if "uses" in step]
    aru = [reference for reference in uses if not reference.startswith("actions/checkout@")]
    assert aru == [
        f"{init_project.FACTORY_REPOSITORY}/.github/actions/merge-policy@v{policy.version()}"
    ]
    # Nothing is executed by the stub itself except the trust guard.
    runs = [step for step in steps if "run" in step]
    assert len(runs) == 1 and "ARU_EVENT_NAME" in runs[0]["run"]
    call = [step for step in steps if step.get("uses") == aru[0]][0]
    assert call["with"]["expected-head"] == "${{ github.event.pull_request.head.sha }}"
    assert call["with"]["pr-number"] == "${{ github.event.pull_request.number }}"


# --- The checks themselves, now that they run from the Factory's composite action ---


def merge_policy_action() -> dict:
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "hook", ["enforce_touches.py", "check_manifest.py", "check_stubs.py"]
)
def test_the_action_invokes_each_base_branch_checker_exactly_once(hook):
    """The three judgements a merge-policy run makes about a head: its write boundary,
    its Factory-managed files, and its workflow stubs."""
    # Run from the resolved Factory checkout, never from the workspace the caller
    # checked out, so a consumer repository cannot supply the script that judges it.
    invocation = f'python3 "${{ARU_SDLC_HOME}}/hooks/{hook}"'
    steps = merge_policy_action()["runs"]["steps"]
    invoking = [step for step in steps if invocation in str(step.get("run", ""))]
    assert len(invoking) == 1, [step.get("name") for step in invoking]
    run = invoking[0]["run"]
    assert "--pr " in run and "--expected-head " in run


def test_the_action_never_reads_or_checks_out_the_head():
    """The safety rule that makes `pull_request_target` acceptable, asserted where the
    work actually happens: head bytes are read through the contents API and hashed,
    and nothing from the pull request is checked out, installed or executed."""
    raw = ACTION_PATH.read_text(encoding="utf-8")
    action = merge_policy_action()
    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    # A composite step that runs nothing but `run:` cannot check anything out.
    assert all("uses" not in step for step in steps), [s.get("uses") for s in steps]
    for forbidden in ("actions/checkout", "git checkout", "git fetch",
                      "github.event.pull_request.head", "pip install", "pytest"):
        assert forbidden not in raw, forbidden
    # The head reaches the checkers as a value to verify against, never as a ref.
    assert "expected-head" in action["inputs"]


def test_the_action_refuses_a_factory_checkout_missing_a_checker():
    """A packaged tag without one of the three scripts would otherwise skip that
    judgement silently; the resolve step fails closed instead."""
    resolve = merge_policy_action()["runs"]["steps"][0]["run"]
    for hook in ("hooks/enforce_touches.py", "hooks/check_manifest.py", "hooks/check_stubs.py"):
        assert hook in resolve
    assert "exit 1" in resolve


def test_both_required_checks_are_declared_for_consumers():
    """A consumer inherits both gates, pinned to the Actions App."""
    rules = {rule["type"]: rule for rule in init_project.ruleset_payload()["rules"]}
    contexts = [
        check["context"]
        for check in rules["required_status_checks"]["parameters"]["required_status_checks"]
    ]
    assert contexts == ["aru-governed-pr", "aru-merge-policy"]
