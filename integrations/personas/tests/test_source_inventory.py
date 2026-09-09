"""Adversarial coverage for the static assets admitted by test_surface."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from .source_inventory import STATIC_PERSONA_JSON, assert_static_persona_json

ROOT = Path(__file__).resolve().parents[3]


class SourceInventoryTests(unittest.TestCase):
    def test_surface_guard_refuses_tracked_runtime_files(self):
        guard = runpy.run_path(str(ROOT / "tests/test_surface.py"))[
            "test_one_state_authority_no_tracked_runtime_ledgers"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("jobs.json", "queue.jsonl", "history.db", "state.sqlite",
                         "state.sqlite3", "STATE.JSON"):
                with self.subTest(name=name):
                    path = root / "integrations/personas/examples" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('{"synthetic": true, "executions": [{"status": "running"}]}')
                    with patch.dict(guard.__globals__, ROOT=root, tracked_paths=lambda: [path]):
                        with self.assertRaises(AssertionError):
                            guard()

    def test_exact_eight_static_assets_are_valid(self):
        self.assertEqual(len(STATIC_PERSONA_JSON), 8)
        for name in STATIC_PERSONA_JSON:
            with self.subTest(name=name):
                assert_static_persona_json(ROOT / "integrations/personas" / name, ROOT)

    def test_unknown_runtime_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("integrations/personas", "integrations/personas/examples",
                              "integrations/personas/data", "unrelated"):
                for name in ("new.json", "jobs.json", "queue.jsonl", "binding.json",
                             "history.db", "state.sqlite", "state.sqlite3", "STATE.JSON",
                             "VERIFICATION.json", "observed-2026-09-09.json"):
                    with self.subTest(directory=directory, name=name):
                        path = root / directory / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('{"synthetic": true, "jobs": [{"status": "running"}]}')
                        with self.assertRaises(AssertionError):
                            assert_static_persona_json(path, root)

    def test_allowed_names_cannot_hold_execution_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in STATIC_PERSONA_JSON:
                original = json.loads((ROOT / "integrations/personas" / name).read_text())
                for field in ("executions", "queue", "state", "observations"):
                    with self.subTest(name=name, field=field):
                        raw = dict(original, **{field: [{"head": "b" * 40, "status": "running"}]})
                        path = root / "integrations/personas" / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(raw))
                        with self.assertRaises(AssertionError):
                            assert_static_persona_json(path, root)

    def test_nested_execution_history_cannot_hide_in_synthetic_binding(self):
        name = "examples/synthetic-binding.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "integrations/personas" / name
            path.parent.mkdir(parents=True)
            raw = json.loads((ROOT / "integrations/personas" / name).read_text())
            raw["evidence"]["observations"][0]["executions"] = [{"status": "running"}]
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(AssertionError, "changed static asset"):
                assert_static_persona_json(path, root)

    def test_live_observation_cannot_replace_synthetic_probe(self):
        name = "examples/synthetic-binding.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "integrations/personas" / name
            path.parent.mkdir(parents=True)
            raw = json.loads((ROOT / "integrations/personas" / name).read_text())
            raw["evidence"]["observations"][0]["source"] = "authenticated live probe"
            path.write_text(json.dumps(raw))
            with self.assertRaises(AssertionError):
                assert_static_persona_json(path, root)
