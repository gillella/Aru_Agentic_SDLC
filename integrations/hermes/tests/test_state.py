from __future__ import annotations

import json
import fcntl
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aru_project_driver import state as state_module
from aru_project_driver.config import DriverError
from aru_project_driver.state import State, key, read_json, write_json


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "private-state")


def test_unreadable_and_nonobject_state_fail_closed(tmp_path):
    path = tmp_path / "state.json"
    assert read_json(path, {"enabled": False}) == {"enabled": False}
    with pytest.raises(DriverError, match="unreadable"):
        read_json(path)
    for content in ("{invalid", "[]", "null"):
        path.write_text(content)
        with pytest.raises(DriverError):
            read_json(path, {"enabled": False})


def test_atomic_write_preserves_previous_receipt_when_publish_fails(tmp_path, monkeypatch):
    path = tmp_path / "private" / "receipt.json"
    write_json(path, {"state": "running"})
    def interrupted_publish(*args):
        raise OSError("synthetic filesystem failure")
    monkeypatch.setattr(state_module.os, "replace", interrupted_publish)
    with pytest.raises(OSError):
        write_json(path, {"state": "exited"})
    assert read_json(path) == {"state": "running"}
    assert not list(path.parent.glob("*.tmp"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_event_dedup_survives_restart_and_is_scoped_to_project(state):
    for repo in ("owner/one", "owner/two"):
        project = state.project(repo)
        project["enabled"] = True
        state.save(repo, project)
    with state.lock():
        assert state.event("owner/one", "delivery-1", "change") is True
    restarted = State(state.root)
    with restarted.lock():
        assert restarted.event("owner/one", "delivery-1", "redelivery") is False
        assert restarted.event("owner/two", "delivery-1", "same delivery, different project") is True
    assert restarted.project("owner/one")["generation"] == 1
    assert restarted.project("owner/two")["generation"] == 1
    assert restarted.project("owner/one")["events"][0]["key"] == key("delivery-1")


def test_stopped_event_has_no_effect_and_recent_event_history_is_bounded(state):
    assert state.event("owner/repo", "delivery-0", "ignored") is False
    assert not state.project_path("owner/repo").exists()
    project = state.project("owner/repo")
    project["enabled"] = True
    state.save("owner/repo", project)
    with state.lock():
        for number in range(260):
            state.event("owner/repo", str(number), "x" * 300)
    project = state.project("owner/repo")
    assert project["generation"] == 260
    assert len(project["events"]) == 256
    assert len(project["events"][-1]["reason"]) <= 120
    project["enabled"] = False
    state.save("owner/repo", project)
    assert state.event("owner/repo", "after-stop", "ignored") is False
    assert state.project("owner/repo")["generation"] == 260


def test_repository_identity_cannot_be_rebound_by_state_file(state):
    write_json(state.project_path("owner/one"), {"repo": "owner/two", "enabled": True})
    with pytest.raises(DriverError, match="identity"):
        state.project("owner/one")
    with pytest.raises(DriverError, match="mismatch"):
        state.save("owner/one", {"repo": "owner/two", "enabled": True})
    write_json(state.project_path("owner/one"), {"repo": "owner/one", "enabled": 1})
    with pytest.raises(DriverError, match="identity"):
        state.project("owner/one")


def test_coordination_lock_excludes_an_independent_process_and_releases_on_error(state):
    script = (
        "import fcntl,sys\n"
        "with open(sys.argv[1], 'a+') as stream:\n"
        " try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        " except BlockingIOError: raise SystemExit(23)\n"
    )
    lock_path = state.root / "coordination.lock"
    def contender():
        return subprocess.run([sys.executable, "-c", script, str(lock_path)],
                              check=False, capture_output=True, timeout=5).returncode
    with pytest.raises(RuntimeError, match="synthetic"):
        with state.lock():
            assert contender() == 23
            with pytest.raises(DriverError, match="another Driver"):
                with State(state.root).lock():
                    pytest.fail("second coordinator entered the lock")
            raise RuntimeError("synthetic activation failure")
    assert contender() == 0


def test_capacity_lock_is_shared_by_key_and_recovered_after_process_exit(state):
    lock_path = state.capacity_path("account-shared-by-models")
    lock_path.parent.mkdir(parents=True)
    script = (
        "import fcntl,sys\n"
        "with open(sys.argv[1], 'a+') as stream:\n"
        " fcntl.flock(stream, fcntl.LOCK_EX)\n"
        " print('locked', flush=True)\n"
        " sys.stdin.read(1)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(lock_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        assert State(state.root).capacity_busy("account-shared-by-models") is True
        assert state.capacity_busy("another-account") is False
    finally:
        process.communicate("x", timeout=5)
    assert process.returncode == 0
    assert state.capacity_busy("account-shared-by-models") is False
    assert lock_path.exists()  # Persisted lock files alone never imply a live worker.


def test_worker_receipts_are_filtered_by_repository_and_malformed_receipts_block(state):
    for worker_id, repo in (("one", "owner/one"), ("two", "owner/two")):
        write_json(state.worker_path(worker_id), {"id": worker_id, "repo": repo, "state": "exited"})
    assert [record["id"] for record in state.workers("owner/one")] == ["one"]
    assert len(state.workers()) == 2
    state.worker_path("broken").write_text(json.dumps([]))
    with pytest.raises(DriverError, match="object"):
        state.workers()


def test_capacity_holder_identifies_current_lock_and_ignores_stale_file_contents(state):
    path = state.capacity_path("shared-account")
    path.parent.mkdir(parents=True)
    path.write_text("old-worker\n")
    assert state.capacity_holder("shared-account") is None
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert state.capacity_holder("shared-account") == "old-worker"
        stream.seek(0)
        stream.truncate()
        stream.write("new-worker\n")
        stream.flush()
        assert State(state.root).capacity_holder("shared-account") == "new-worker"
    assert path.read_text() == "new-worker\n"
    assert state.capacity_holder("shared-account") is None
