#!/usr/bin/env python3
"""test_version_pinning.py — Unit tests for ARU_SDLC_REF version pinning and compatibility checks."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import common  # noqa: E402


class VersionPinningTests(unittest.TestCase):
    def test_parse_semver_major_valid(self):
        self.assertEqual(common.parse_semver_major("v0.1.0"), 0)
        self.assertEqual(common.parse_semver_major("v1.0.0"), 1)
        self.assertEqual(common.parse_semver_major("v2.14.3"), 2)
        self.assertEqual(common.parse_semver_major("10.5.2"), 10)

    def test_parse_semver_major_invalid(self):
        self.assertIsNone(common.parse_semver_major(""))
        self.assertIsNone(common.parse_semver_major("main"))
        self.assertIsNone(common.parse_semver_major(None))

    def test_check_version_compatibility_matching_major(self):
        with patch.dict(os.environ, {"ARU_SDLC_REF": "v1.2.0"}):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.0.0")
                self.assertTrue(res)
                written = "".join(call.args[0] for call in mock_stderr.call_args_list)
                self.assertNotIn("mismatch", written)

    def test_check_version_compatibility_mismatch_major_warns_and_continues(self):
        with patch.dict(os.environ, {"ARU_SDLC_REF": "v2.0.0"}):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.5.0")
                self.assertTrue(res)  # Must return True (never hard-fail)
                written = "".join(call.args[0] for call in mock_stderr.call_args_list)
                self.assertIn("[WARN] Framework version mismatch", written)
                self.assertIn("v2.0.0", written)
                self.assertIn("v1.5.0", written)

    def test_check_version_compatibility_unset_ref(self):
        env_without_ref = dict(os.environ)
        env_without_ref.pop("ARU_SDLC_REF", None)
        with patch.dict(os.environ, env_without_ref, clear=True):
            with patch("sys.stderr.write") as mock_stderr:
                res = common.check_version_compatibility(current_version="v1.0.0")
                self.assertTrue(res)
                mock_stderr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
