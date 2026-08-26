"""Quota-safe GitHub inventory reads."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common  # noqa: E402


class LocalRepositoryIdentityTests(unittest.TestCase):
    def test_common_remote_url_shapes_need_no_github_call(self):
        remotes = {
            "https://github.com/acme/widgets.git": "acme/widgets",
            "git@github.com:acme/widgets.git": "acme/widgets",
            "ssh://git@github.example/acme/widgets.git": "acme/widgets",
        }
        for remote, expected in remotes.items():
            with self.subTest(remote=remote), patch.object(
                common, "run_cmd", return_value=(0, remote, "")
            ) as run:
                self.assertEqual(common.get_repo_slug(), expected)
                self.assertEqual(
                    run.call_args.args[0], ["git", "remote", "get-url", "origin"]
                )

    def test_missing_or_ambiguous_remote_fails_closed(self):
        for result in (
            (1, "", "missing"),
            (0, "/tmp/local-repo", ""),
            (0, "https://github.com/acme/group/widgets.git", ""),
        ):
            with self.subTest(result=result), patch.object(
                common, "run_cmd", return_value=result
            ):
                self.assertIsNone(common.get_repo_slug())


class RestIssueInventoryTests(unittest.TestCase):
    @staticmethod
    def issue(number=7):
        return {
            "number": number,
            "title": "bounded inventory",
            "labels": [{"name": "status:ready"}],
            "assignees": [{"login": "octocat"}],
            "body": "touches: scripts/common.py",
            "state": "open",
            "updated_at": "2026-08-25T12:00:00Z",
            "user": {"login": "octocat"},
            "author_association": "OWNER",
        }

    def test_open_issues_use_paginated_rest_and_normalize_shape(self):
        with patch.object(common, "get_repo_slug", return_value="acme/widgets"), \
             patch.object(
                 common, "run_cmd", return_value=(0, json.dumps(self.issue()), "")
             ) as run:
            issues = common.query_open_issues()

        self.assertEqual(issues[0]["state"], "OPEN")
        self.assertEqual(issues[0]["updatedAt"], "2026-08-25T12:00:00Z")
        self.assertEqual(issues[0]["author"], {"login": "octocat"})
        self.assertEqual(issues[0]["authorAssociation"], "OWNER")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "api", "--paginate"])
        self.assertIn("issues?state=open&per_page=100", " ".join(command))

    def test_failure_malformed_or_duplicate_snapshot_fails_closed(self):
        duplicate = "\n".join(json.dumps(self.issue()) for _ in range(2))
        for result in (
            (1, "", "rate limited"),
            (0, "not-json", ""),
            (0, duplicate, ""),
        ):
            with self.subTest(result=result), \
                 patch.object(common, "get_repo_slug", return_value="acme/widgets"), \
                 patch.object(common, "run_cmd", return_value=result):
                self.assertIsNone(common.query_open_issues())


if __name__ == "__main__":
    unittest.main()
