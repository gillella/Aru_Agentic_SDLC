"""Tests for the private, locked, atomic JSON primitive extracted in #411."""

import ast
import json
import multiprocessing
import os
import stat
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from secure_json import (  # noqa: E402
    RegistryError,
    _file_lock,
    _private_directory,
    _private_file,
    _read_unlocked,
    _write_unlocked,
    mutate_secure_json,
    read_secure_json,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _hold_lock(path_str, started, release_after):
    """Child process: take the lock, signal, hold it, then drop it."""
    from pathlib import Path as _Path
    sys.path.insert(0, str(SCRIPTS))
    from secure_json import _file_lock as lock
    with lock(_Path(path_str)):
        started.set()
        time.sleep(release_after)


class ModuleBoundaryTests(unittest.TestCase):
    """The whole point of the extraction: storage must not depend on Slack."""

    def test_secure_json_imports_nothing_from_slack(self):
        tree = ast.parse((SCRIPTS / "secure_json.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = sorted(name for name in imported if "slack" in name.lower())
        self.assertEqual(offenders, [], f"secure_json must not import Slack: {offenders}")

    def test_registry_error_is_a_single_shared_class(self):
        """delivery_increments.IncrementError subclasses this; a copy would break it."""
        import delivery_increments
        import slack_projects
        self.assertIs(slack_projects.RegistryError, RegistryError)
        self.assertIs(delivery_increments.RegistryError, RegistryError)
        self.assertTrue(issubclass(delivery_increments.IncrementError, RegistryError))


class PermissionTests(unittest.TestCase):
    def setUp(self):
        import tempfile as tf
        self.root = Path(tf.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_created_directory_is_owner_only(self):
        target = self.root / "nested" / "store.json"
        _private_directory(target.parent)
        mode = stat.S_IMODE(target.parent.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_written_file_is_owner_only(self):
        path = self.root / "store.json"
        _write_unlocked(path, {"a": 1})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_group_or_other_writable_directory_is_refused(self):
        d = self.root / "loose"
        d.mkdir(mode=0o777)
        os.chmod(d, 0o707)
        with self.assertRaises(RegistryError):
            _private_directory(d)

    def test_symlinked_target_is_refused_on_read_and_write(self):
        real = self.root / "real.json"
        real.write_text("{}")
        os.chmod(real, 0o600)
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaises(RegistryError):
            _read_unlocked(link, {})
        with self.assertRaises(RegistryError):
            _write_unlocked(link, {})

    def test_world_readable_existing_file_is_refused(self):
        path = self.root / "loose.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        with self.assertRaises(RegistryError):
            _private_file(path)

    def test_non_regular_file_is_refused(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        os.chmod(fifo, 0o600)
        with self.assertRaises(RegistryError):
            _private_file(fifo)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        import tempfile as tf
        self.root = Path(tf.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_malformed_json_raises_rather_than_returning_the_default(self):
        """A corrupt store must be visible, not silently replaced by an empty one."""
        path = self.root / "bad.json"
        path.write_text("{not json")
        os.chmod(path, 0o600)
        with self.assertRaises(RegistryError):
            _read_unlocked(path, {"safe": True})

    def test_missing_file_returns_a_deep_copy_of_the_default(self):
        default = {"nested": {"v": 1}}
        got = _read_unlocked(self.root / "absent.json", default)
        self.assertEqual(got, default)
        got["nested"]["v"] = 2
        self.assertEqual(default["nested"]["v"], 1)

    def test_missing_file_without_default_raises(self):
        with self.assertRaises(RegistryError):
            _read_unlocked(self.root / "absent.json")

    def test_credential_shaped_payloads_are_refused(self):
        path = self.root / "secret.json"
        for payload in ({"t": "xoxb-1-2-abc"}, {"t": "xapp-1-abc"}, {"h": "Bearer abcdef"}):
            with self.subTest(payload=payload):
                with self.assertRaises(RegistryError):
                    _write_unlocked(path, payload)
        self.assertFalse(path.exists(), "a refused write must leave no file behind")


class AtomicityTests(unittest.TestCase):
    def setUp(self):
        import tempfile as tf
        self.root = Path(tf.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_round_trip(self):
        path = self.root / "store.json"
        _write_unlocked(path, {"a": [1, 2]})
        self.assertEqual(_read_unlocked(path), {"a": [1, 2]})

    def test_mutate_applies_the_updater_exactly_once(self):
        path = self.root / "store.json"
        calls = []

        def bump(current):
            calls.append(current)
            return {"n": (current or {}).get("n", 0) + 1}

        self.assertEqual(mutate_secure_json(path, {"n": 0}, bump), {"n": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(read_secure_json(path), {"n": 1})

    def test_failed_update_leaves_the_previous_value_intact(self):
        path = self.root / "store.json"
        _write_unlocked(path, {"n": 7})

        def explode(_current):
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            mutate_secure_json(path, {"n": 0}, explode)
        self.assertEqual(read_secure_json(path), {"n": 7})

    def test_no_temp_files_survive_a_refused_write(self):
        path = self.root / "store.json"
        _write_unlocked(path, {"ok": 1})
        with self.assertRaises(RegistryError):
            _write_unlocked(path, {"t": "xoxb-leak"})
        strays = [p.name for p in self.root.iterdir() if p.name.startswith(".store.json.")]
        self.assertEqual(strays, [])

    def test_written_bytes_are_canonical_json(self):
        path = self.root / "store.json"
        _write_unlocked(path, {"b": 2, "a": 1})
        raw = path.read_text()
        self.assertEqual(raw, json.dumps({"a": 1, "b": 2}, indent=2, sort_keys=True) + "\n")


class LockTests(unittest.TestCase):
    def setUp(self):
        import tempfile as tf
        self.root = Path(tf.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_lock_file_is_owner_only(self):
        path = self.root / "store.json"
        with _file_lock(path):
            pass
        lock_path = path.with_name("store.json.lock")
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_second_holder_waits_for_the_first(self):
        path = self.root / "store.json"
        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        child = ctx.Process(target=_hold_lock, args=(str(path), started, 1.0))
        child.start()
        self.addCleanup(child.join)
        self.assertTrue(started.wait(timeout=30), "child never acquired the lock")
        begin = time.monotonic()
        with _file_lock(path):
            waited = time.monotonic() - begin
        self.assertGreater(waited, 0.3, "second holder did not block on the first")


if __name__ == "__main__":
    unittest.main()
