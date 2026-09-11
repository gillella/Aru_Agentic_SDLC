"""The merge-policy workflow: the check a pull request cannot rewrite.

`aru-governed-pr` runs the workflow file that is in the pull request, so a pull request
that edits it keeps the check name and can go green without running anything. This
workflow runs the copy on the base branch instead. These tests pin the properties that
make that true, and the safety rule that makes `pull_request_target` acceptable here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import init_project

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SOURCES = ["live", "self-hosted-mac", "github-hosted"]


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
    # pull_request_review is here because an approval is not a push: without it the check
    # would never re-evaluate after someone approves.
    assert list(workflow[True]) == ["pull_request_target", "pull_request_review"]
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
def test_merge_policy_publishes_its_verdict_on_the_head(source):
    """The job's own check run lands on the base commit, so the head needs a status."""
    workflow = yaml.safe_load(policy_workflows()[source])
    assert workflow["permissions"]["statuses"] == "write"
    assert workflow["permissions"]["contents"] == "read"
    steps = workflow["jobs"]["merge-policy"]["steps"]
    publish = steps[-1]
    assert "always()" in publish["if"]
    assert "context=aru-merge-policy" in publish["run"]
    assert "statuses/$ARU_HEAD_SHA" in publish["run"]
    enforce = [step for step in steps if "enforce_touches.py" in str(step.get("run", ""))]
    assert len(enforce) == 1 and "--expected-head" in enforce[0]["run"]


@pytest.mark.parametrize("source", WORKFLOW_SOURCES)
def test_merge_policy_refuses_events_it_cannot_trust(source):
    """A fork head, or any event other than pull_request_target, must not be judged here."""
    steps = yaml.safe_load(policy_workflows()[source])["jobs"]["merge-policy"]["steps"]
    guard = steps[0]["run"]
    assert '"$ARU_EVENT_NAME" != "pull_request_target"' in guard
    assert '"$ARU_HEAD_REPOSITORY" != "$ARU_REPOSITORY"' in guard
    assert "exit 1" in guard


def test_merge_policy_has_no_profile_drift():
    """The self-hosted rendering must still reproduce Aru's own live workflow."""
    workflows = policy_workflows()
    assert yaml.safe_load(workflows["self-hosted-mac"]) == yaml.safe_load(workflows["live"])


def test_both_required_checks_are_declared_for_consumers():
    """A consumer inherits both gates, pinned to the Actions App."""
    rules = {rule["type"]: rule for rule in init_project.ruleset_payload()["rules"]}
    contexts = [
        check["context"]
        for check in rules["required_status_checks"]["parameters"]["required_status_checks"]
    ]
    assert contexts == ["aru-governed-pr", "aru-merge-policy"]
