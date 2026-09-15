"""A scaffolded workflow executes no path of its own: it calls the Factory.

`aru-merge-policy` runs the *base branch's* copy of its workflow on
`pull_request_target`. A consumer whose default branch executes a path the
scaffold never created therefore fails every pull request, including the one
that would fix it -- `gillella/AruLifts` wedged exactly this way and needed its
ruleset disabled to recover.

A thin consumer closes that class of failure by owning no verification logic at
all. Both workflows are stubs: the trust boundary, a checkout, the check name,
the runner, and one `uses:` line pinning this Factory's composite action to a
release tag. So the old invariant -- every executed path is a scaffolded path --
now holds in its strongest form: there are no executed paths, and the guard is
the pair below, which pins that emptiness and pins what the stub calls instead.

The checks read the **rendered consumer files**, not this repository's own
layout. That distinction is the whole point: Aru keeps its hooks at `hooks/`
while a consumer has none, which is how the old mismatch survived.
"""

from __future__ import annotations

import re

import pytest
import yaml

import init_project
import policy

PROFILES = ("self-hosted-mac", "github-hosted")

# path -> the check name its job must keep publishing, and the Factory action it
# must call. A stub that keeps the name while calling anything else is a green
# required check that ran none of the Factory's verification.
STUBS = {
    ".github/workflows/governed-pr.yml": ("governed-pr", "aru-governed-pr"),
    ".github/workflows/merge-policy.yml": ("merge-policy", "aru-merge-policy"),
}

# A path a `run:` block hands to an interpreter, or executes directly.
# The leading character class must admit `.`, or every `.aru/...` path is
# missed and the guard passes for the wrong reason. The extractor's own test
# below exists because this regex is load-bearing.
EXECUTED = re.compile(
    r"(?:^|[\s|&;(])(?:python3?|bash|sh)\s+(?P<path>[A-Za-z0-9_.][A-Za-z0-9_./-]*\.(?:py|sh))"
    r"|(?:^|[\s|&;(])(?P<direct>\./[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh))",
    re.MULTILINE,
)


def executed_paths(workflow_text: str) -> set[str]:
    """Repository-relative paths the workflow's run steps execute."""
    document = yaml.safe_load(workflow_text)
    found: set[str] = set()
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            for match in EXECUTED.finditer(script):
                path = match.group("path") or match.group("direct")
                found.add(path[2:] if path.startswith("./") else path)
    return found


@pytest.mark.parametrize("profile", PROFILES)
def test_a_scaffolded_workflow_executes_no_script_of_its_own(profile):
    rendered = init_project.framework_files(profile)
    workflows = {name: body for name, body in rendered.items()
                 if name.startswith(".github/workflows/")}
    assert set(workflows) == set(STUBS), "the scaffold writes exactly the two stubs"

    for name, body in workflows.items():
        assert executed_paths(body) == set(), (
            f"{name} executes a script; a stub runs the Factory's action, not its own checks"
        )
    # The retired copies cannot come back: a consumer that carries one again has two
    # answers to "what does this check do", and the one in the repository is the one a
    # head can rewrite. Neither written nor invoked.
    for retired in init_project.RETIRED:
        assert retired not in rendered, f"{retired} is retired; the scaffold must not write it"
        for name, body in workflows.items():
            assert retired not in body, f"{name} still references {retired}"


@pytest.mark.parametrize("profile", PROFILES)
def test_each_stub_declares_its_profile_and_calls_one_factory_action(profile):
    rendered = init_project.framework_files(profile)
    runs_on = yaml.safe_load(init_project.profile_spec(profile)["runs_on"])
    release = f"v{policy.version()}"

    for name, (action, check_name) in STUBS.items():
        body = rendered[name]
        assert f"# aru-runner-profile: {profile}" in body, f"{name} must declare its profile"
        (job,) = yaml.safe_load(body)["jobs"].values()
        assert (job["runs-on"], job["name"]) == (runs_on, check_name)
        # Exactly one Aru reference, at this Factory, for this stub's own action,
        # pinned to the declared release. `actions/checkout` is the only other one.
        references = [step["uses"] for step in job["steps"] if "uses" in step]
        assert [r for r in references if not r.startswith("actions/checkout@")] == [
            f"{init_project.FACTORY_REPOSITORY}/.github/actions/{action}@{release}"
        ], f"{name} references {references}"


def test_the_extractor_sees_what_it_claims_to_see():
    # A guard built on a regex is only as good as the regex. If this stops
    # matching, the guard above starts passing for the wrong reason.
    sample = """
jobs:
  j:
    steps:
      - run: bash .aru/verify.sh
      - run: |
          python3 .aru/hooks/enforce_touches.py --pr 1
          ./scripts/thing.sh
      - run: echo "not a path"
      - uses: actions/checkout@v4
"""
    assert executed_paths(sample) == {
        ".aru/verify.sh",
        ".aru/hooks/enforce_touches.py",
        "scripts/thing.sh",
    }
