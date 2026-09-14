from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def forbid_live_github(monkeypatch):
    """No test may reach the real repository: an unmocked gh call fails the test."""
    real_run = subprocess.run

    def guarded(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and Path(str(argv[0])).name == "gh":
            pytest.fail(f"test reached live GitHub: {' '.join(map(str, argv[:4]))}")
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture(autouse=True)
def isolate_github_app_runner(monkeypatch):
    """Keep operator App-runner configuration out of the focused test suite."""
    monkeypatch.delenv("ARU_GITHUB_APP_RUNNER", raising=False)
    monkeypatch.delenv("ARU_MERGE_APP_RUNNER", raising=False)
    monkeypatch.delenv("ARU_MERGE_APP_ID", raising=False)


@pytest.fixture(autouse=True)
def default_review_posture(monkeypatch):
    """Resolve the review posture without reaching GitHub.

    The suite predates the posture and was written against the permissive rule, so that is
    what it keeps exercising. The strict posture, and every way an unusable declaration
    resolves, are covered directly in tests/test_review_authority.py, and the wiring from
    the merge gate into the posture is asserted there too.
    """
    import review_authority

    monkeypatch.setattr(review_authority, "read_policy_text", lambda **_: '{"authority": "any"}')


@pytest.fixture
def rehash_manifest():
    """Recompute every hash in a scaffold's `.aru/manifest.json` from what is on disk.

    `.aru/verify.sh` verifies managed files first and unconditionally, so a test that
    mutates a managed file to reach a later section would otherwise stop at the first
    one. Rehashing models the documented residual -- an author who edits a managed file
    and regenerates the manifest passes the head's own integrity check -- and keeps each
    of those tests asserting the section it was written for.
    """
    import hashlib
    import json

    def apply(root: Path) -> None:
        path = root / ".aru/manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["files"] = {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in document["files"]
        }
        for relative, entry in document.get("blocks", {}).items():
            lines = (root / relative).read_text(encoding="utf-8").splitlines()
            start = lines.index(entry["begin"])
            stop = lines.index(entry["end"])
            block = "\n".join(lines[start:stop + 1]) + "\n"
            entry["sha256"] = hashlib.sha256(block.encode("utf-8")).hexdigest()
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return apply
