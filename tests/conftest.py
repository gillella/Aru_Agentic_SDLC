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
