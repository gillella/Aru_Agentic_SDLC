from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def isolate_github_app_runner(monkeypatch):
    """Keep operator App-runner configuration out of the focused test suite."""
    monkeypatch.delenv("ARU_GITHUB_APP_RUNNER", raising=False)
    monkeypatch.delenv("ARU_MERGE_APP_RUNNER", raising=False)
    monkeypatch.delenv("ARU_MERGE_APP_ID", raising=False)
