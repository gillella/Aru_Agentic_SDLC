"""Driver tests and spawned fixtures cannot use live GitHub credentials."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def offline_github(tmp_path, monkeypatch):
    fake = tmp_path / "offline-bin" / "gh"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'test reached unmocked GitHub' >&2\nexit 97\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("ARU_DRIVER_GH", str(fake))
    for name in ("ARU_GITHUB_APP_RUNNER", "ARU_MERGE_APP_RUNNER", "ARU_MERGE_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    original = subprocess.run
    def guarded(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and Path(str(argv[0])).name == "gh":
            pytest.fail("test reached unmocked GitHub")
        return original(args, *rest, **kwargs)
    monkeypatch.setattr(subprocess, "run", guarded)
