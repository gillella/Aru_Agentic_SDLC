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
    restarted = State(state.root)
    assert restarted.event("owner/repo", "0", "old delivery retry") is False
    assert restarted.project("owner/repo")["generation"] == 260
    assert len(restarted.project("owner/repo")["delivery_keys"]) == 260
    project["enabled"] = False
    state.save("owner/repo", project)
    assert state.event("owner/repo", "after-stop", "ignored") is False
    assert state.project("owner/repo")["generation"] == 260


def test_corrupt_delivery_keys_fail_closed(state):
    data = state.project("owner/repo")
    data["delivery_keys"] = ["not-a-hash"]
    write_json(state.project_path("owner/repo"), data)
    with pytest.raises(DriverError, match="delivery keys"):
        state.has_event("owner/repo", "delivery")


def test_failed_project_save_leaves_event_eligible_for_retry(state, monkeypatch):
    data = state.project("owner/repo")
    data["enabled"] = True
    state.save("owner/repo", data)
    save = state.save
    monkeypatch.setattr(state, "save", lambda *a: (_ for _ in ()).throw(OSError("synthetic write failure")))
    with pytest.raises(OSError):
        state.event("owner/repo", "delivery", "event")
    assert not state.has_event("owner/repo", "delivery")
    assert state.project("owner/repo")["generation"] == 0
    monkeypatch.setattr(state, "save", save)
    assert state.event("owner/repo", "delivery", "retry") is True
    assert state.project("owner/repo")["generation"] == 1


def test_event_capacity_preserves_old_keys_across_restart(state, monkeypatch):
    monkeypatch.setattr(state_module, "MAX_EVENT_KEYS", 2)
    data = state.project("owner/repo")
    data["enabled"] = True
    state.save("owner/repo", data)
    assert state.event("owner/repo", "first", "event")
    assert state.event("owner/repo", "second", "event")
    restarted = State(state.root)
    assert not restarted.event("owner/repo", "first", "retry")
    with pytest.raises(DriverError, match="receipt capacity"):
        restarted.event("owner/repo", "third", "event")
    assert restarted.project("owner/repo")["generation"] == 2


def test_repository_identity_cannot_be_rebound_by_state_file(state):
    write_json(state.project_path("owner/one"), {"repo": "owner/two", "enabled": True})
    with pytest.raises(DriverError, match="identity"):
        state.project("owner/one")
    with pytest.raises(DriverError, match="mismatch"):
        state.save("owner/one", {"repo": "owner/two", "enabled": True})
    write_json(state.project_path("owner/one"), {"repo": "owner/one", "enabled": 1})
    with pytest.raises(DriverError, match="identity"):
        state.project("owner/one")


@pytest.mark.parametrize("field,value", [
    ("generation", "1"), ("generation", True), ("generation", -1),
    ("events", {}), ("events", [{}]), ("events", [None]),
    ("events", [{"key": "a" * 24, "reason": "event", "observed_at": "now"}]),
    ("cooldown_until", "later"), ("wake_pending_until", float("nan")),
    ("handled_generation", 1),
])
def test_malformed_project_schema_fails_with_bounded_error(state, field, value):
    data = state.project("owner/one")
    data[field] = value
    write_json(state.project_path("owner/one"), data)
    with pytest.raises(DriverError, match="operational project"):
        state.has_event("owner/one", "delivery")


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
        write_json(state.worker_path(worker_id), {"id": worker_id, "repo": repo, "state": "exited",
                   "agent": "worker-agent", "issue": 1, "capacity_key": "account", "started_at": 1})
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


def test_stop_fence_survives_stale_save_and_only_matching_start_acknowledges(state):
    repo = "owner/repo"
    initial = state.project(repo)
    initial["enabled"] = True
    state.save(repo, initial)
    stale = state.project(repo)
    nonce = state.request_stop(repo)
    state.save(repo, stale)
    assert not State(state.root).project(repo)["enabled"]
    assert State(state.root).stop_nonce(repo) == nonce
    stale.update(acknowledged_stop=nonce, enabled=True)
    state.save(repo, stale)
    assert state.project(repo)["enabled"]
    state.request_stop(repo)
    state.save(repo, stale)
    assert not state.project(repo)["enabled"]


@pytest.mark.parametrize("bad", [{}, {"nonce": "x"}, {"repo": "other/repo", "nonce": "a" * 32, "stopped_at": 1}])
def test_existing_malformed_stop_intent_never_becomes_legacy_absence(state, bad):
    from aru_project_driver.state import write_json

    repo = "owner/repo"
    data = state.project(repo)
    data["enabled"] = True
    state.save(repo, data)
    write_json(state.root / "stops" / f"{key(repo)}.json", bad)
    with pytest.raises(DriverError, match="Stop intent"):
        state.project(repo)


def test_acknowledged_but_missing_stop_intent_blocks_reads_and_admission(state):
    repo = "owner/repo"
    data = state.project(repo)
    nonce = state.request_stop(repo)
    data.update(enabled=True, acknowledged_stop=nonce)
    state.save(repo, data)
    state.require_admission(repo, nonce)
    (state.root / "stops" / f"{key(repo)}.json").unlink()
    with pytest.raises(DriverError, match="Stop intent is missing"):
        state.project(repo)
    with pytest.raises(DriverError, match="Stop intent is missing"):
        state.require_admission(repo, state.stop_nonce(repo))


def test_concurrent_stop_writers_have_independent_atomic_temporary_files(state, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = "owner/repo"
    data = state.project(repo)
    data["enabled"] = True
    state.save(repo, data)
    opened = threading.Barrier(2)
    original = json.dump
    def overlapping_dump(*args, **kwargs):
        # Both writers must own an open temp file before either may replace.
        opened.wait(timeout=3)
        return original(*args, **kwargs)
    monkeypatch.setattr(json, "dump", overlapping_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(state.request_stop, repo)
        second = pool.submit(state.request_stop, repo)
        nonces = {first.result(timeout=5), second.result(timeout=5)}
    assert len(nonces) == 2 and state.stop_nonce(repo) in nonces
    assert not state.project(repo)["enabled"]
    assert list((state.root / "stops").glob(".*.tmp")) == []


def test_stop_preserves_events_and_other_project_bytes(state):
    repo, other = "owner/repo", "owner/other"
    for name in (repo, other):
        data = state.project(name)
        data["enabled"] = True
        state.save(name, data)
        state.event(name, "delivery", "event")
    before = state.project_path(other).read_bytes()
    state.request_stop(repo)
    assert state.has_event(repo, "delivery")
    assert state.project_path(other).read_bytes() == before
    assert state.project(other)["enabled"]
