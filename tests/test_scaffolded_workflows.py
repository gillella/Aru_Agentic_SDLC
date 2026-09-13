"""Every path a scaffolded workflow executes must be a path the scaffold writes.

`aru-merge-policy` runs the *base branch's* copy of its workflow on
`pull_request_target`. A consumer whose default branch executes a path the
scaffold never created therefore fails every pull request, including the one
that would fix it -- `gillella/AruLifts` wedged exactly this way and needed its
ruleset disabled to recover.

The check reads the **rendered consumer files**, not this repository's own
layout. That distinction is the whole point: Aru keeps its hooks at `hooks/`
while it scaffolds them to `.aru/hooks/`, which is how the mismatch survived.
"""

from __future__ import annotations

import re

import pytest
import yaml

import init_project

PROFILES = ("self-hosted-mac", "github-hosted")

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
def test_every_executed_path_is_one_the_scaffold_writes(tmp_path, profile):
    rendered = init_project.framework_files(profile)
    written = set(rendered)
    workflows = {name: body for name, body in rendered.items()
                 if name.startswith(".github/workflows/")}
    assert workflows, "the scaffold must write at least one workflow"

    checked = 0
    for name, body in workflows.items():
        for path in executed_paths(body):
            checked += 1
            assert path in written, (
                f"{name} executes {path!r}, which the scaffold never writes. "
                f"Scaffolded paths: {sorted(p for p in written if p.endswith(('.py', '.sh')))}"
            )
    assert checked, "no executed path was checked; this guard would pass vacuously"


@pytest.mark.parametrize("profile", PROFILES)
def test_the_touches_hook_is_executed_from_where_it_is_written(tmp_path, profile):
    # The specific mismatch that wedged a consumer, pinned so it cannot return
    # under a different workflow.
    rendered = init_project.framework_files(profile)
    hook = ".aru/hooks/enforce_touches.py"
    assert hook in rendered, "the scaffold writes the hook here"
    executing = {
        name: paths
        for name, body in rendered.items()
        if name.startswith(".github/workflows/")
        and (paths := {p for p in executed_paths(body) if p.endswith("enforce_touches.py")})
    }
    assert executing, "at least one scaffolded workflow must run the touches hook"
    for name, paths in executing.items():
        assert paths == {hook}, f"{name} runs {sorted(paths)}, not {hook}"


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
