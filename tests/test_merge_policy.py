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
    assert workflow["permissions"]["contents"] == "read"
    assert "statuses/" not in raw
    steps = workflow["jobs"]["merge-policy"]["steps"]
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


# Aru keeps its own hooks at `hooks/`; a consumer receives them at `.aru/hooks/`.
# The live workflow and the scaffolded one therefore differ in exactly this path
# and nowhere else. Conflating the two is what let `templates/merge-policy.yml`
# execute a path the scaffold never wrote, which wedged gillella/AruLifts.
CONSUMER_HOOKS = ".aru/hooks/"
OWN_HOOKS = "hooks/"


def _normalise_hook_paths(text: str) -> str:
    """Rewrite consumer hook paths to this repository's own layout."""
    return text.replace(CONSUMER_HOOKS, OWN_HOOKS)


# Aru is the Factory, not a scaffolded consumer: it carries no `.aru/manifest.json`,
# so the managed-file step exists only in the rendering a consumer receives. That is
# the second intended difference, and it is compared separately rather than ignored.
MANAGED_FILE_STEP = "check_manifest.py"


def _without_managed_file_step(document: dict) -> dict:
    steps = document["jobs"]["merge-policy"]["steps"]
    document["jobs"]["merge-policy"]["steps"] = [
        step for step in steps if MANAGED_FILE_STEP not in str(step.get("run", ""))
    ]
    return document


@pytest.mark.parametrize("source", ["self-hosted-mac", "github-hosted"])
def test_only_the_consumer_rendering_verifies_managed_files(source):
    """A consumer's copy runs the manifest check; the Factory's own copy has nothing
    to check, and must not execute a path it never wrote."""
    def steps(text):
        return yaml.safe_load(text)["jobs"]["merge-policy"]["steps"]

    workflows = policy_workflows()
    consumer = [s for s in steps(workflows[source]) if MANAGED_FILE_STEP in str(s.get("run", ""))]
    assert len(consumer) == 1
    assert ".aru/hooks/check_manifest.py" in consumer[0]["run"]
    assert "--expected-head" in consumer[0]["run"]
    assert not [s for s in steps(workflows["live"]) if MANAGED_FILE_STEP in str(s.get("run", ""))]


def test_merge_policy_has_no_profile_drift():
    """The self-hosted rendering must still reproduce Aru's own live workflow.

    Compared after normalising the hook directory and removing the managed-file step,
    which are real and intended differences rather than drift; each is asserted on its
    own above. Everything else -- triggers, the trust guard, permissions, runner
    target, timeouts -- must still match exactly.
    """
    workflows = policy_workflows()
    rendered = yaml.safe_load(_normalise_hook_paths(workflows["self-hosted-mac"]))
    assert _without_managed_file_step(rendered) == yaml.safe_load(workflows["live"])


def test_the_hook_directory_is_the_only_difference():
    """Guard the normalisation above: it must not be hiding anything else.

    Without this, widening `_normalise_hook_paths` would silently let real drift
    through the test that exists to catch drift.
    """
    def executable_lines(text: str) -> set[str]:
        # Comment prose legitimately differs between the live workflow and the
        # template; what must not differ is anything the runner executes.
        return {line for line in text.splitlines() if not line.strip().startswith("#")}

    workflows = policy_workflows()
    differences = (executable_lines(workflows["self-hosted-mac"])
                   ^ executable_lines(workflows["live"]))
    assert differences, "if these are identical, the normalisation is now pointless"
    assert all("enforce_touches.py" in line or "manifest" in line for line in differences), (
        f"the renderings differ beyond the hook path and the managed-file step: "
        f"{sorted(differences)}"
    )


def test_both_required_checks_are_declared_for_consumers():
    """A consumer inherits both gates, pinned to the Actions App."""
    rules = {rule["type"]: rule for rule in init_project.ruleset_payload()["rules"]}
    contexts = [
        check["context"]
        for check in rules["required_status_checks"]["parameters"]["required_status_checks"]
    ]
    assert contexts == ["aru-governed-pr", "aru-merge-policy"]
